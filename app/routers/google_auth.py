"""Google OAuth — the self-hosted replacement for Supabase's linkIdentity leg.

Flow (Authorization Code):
  GET  /api/auth/google/start     -> redirect the browser to Google's consent
  GET  /api/auth/google/callback  -> Google returns here with ?code&state;
                                     we exchange the code, verify the ID token,
                                     find/create/upgrade the user, mint a token,
                                     and hand it to the SPA via a URL fragment.

State is a short-lived signed JWT (same HS256 secret as sessions) carrying a
nonce + optional guest_id, so there's no server-side session store and the
callback is CSRF-protected. The Google *client secret* is read from env only.
"""

import time
import urllib.parse

import httpx
import jwt
from fastapi import APIRouter, Header, Query
from fastapi.responses import RedirectResponse

from .. import db
from ..auth import AuthError, mint_account_token, new_user_id, normalize_email, verify_token
from ..config import get_settings
from ..provisioning import first_workspace, provision_workspace

router = APIRouter(prefix="/api/auth/google", tags=["auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
_STATE_TTL_SECONDS = 600  # 10 min — a login should complete well within this

# One JWKS client process-wide; PyJWT caches Google's signing keys internally.
_jwks_client = jwt.PyJWKClient(_GOOGLE_JWKS_URL)


def _guest_id(authorization: str) -> str | None:
    """The caller's guest id if they present a valid anonymous token — so a
    Google login upgrades that guest in place instead of orphaning their work."""
    if not authorization.lower().startswith("bearer "):
        return None
    try:
        claims = verify_token(authorization[7:].strip())
    except AuthError:
        return None
    return claims["sub"] if claims.get("is_anonymous") else None


def _sign_state(guest_id: str | None) -> str:
    now = int(time.time())
    return jwt.encode(
        {"nonce": new_user_id(), "guest_id": guest_id, "iat": now, "exp": now + _STATE_TTL_SECONDS},
        get_settings().auth_jwt_secret,
        algorithm="HS256",
    )


def _read_state(state: str) -> str | None:
    """Returns the carried guest_id. Raises on a tampered/expired state."""
    claims = jwt.decode(state, get_settings().auth_jwt_secret, algorithms=["HS256"])
    return claims.get("guest_id")


@router.get("/start")
async def google_start(authorization: str = Header(default="")) -> RedirectResponse:
    s = get_settings()
    if not s.google_enabled:
        raise AuthError(503, "Google sign-in is not configured.")
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": _sign_state(_guest_id(authorization)),
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


def _fail_redirect(reason: str) -> RedirectResponse:
    # Land back on the app with a flag rather than a bare error — the guest
    # session still works, the upgrade just didn't complete (mirrors the TS
    # callback's behaviour).
    base = get_settings().google_post_login_redirect
    sep = "&" if "?" in base else "?"
    return RedirectResponse(f"{base}{sep}link=error&reason={urllib.parse.quote(reason)}")


async def _exchange_code(code: str) -> str:
    """Trade the auth code for Google's ID token (a JWT with the user's email)."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "redirect_uri": s.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if res.status_code >= 400:
        raise ValueError(f"token exchange failed: {res.status_code}")
    id_token = res.json().get("id_token")
    if not id_token:
        raise ValueError("no id_token in Google response")
    return id_token


def _verify_id_token(id_token: str) -> dict:
    """Verify Google's ID token signature/issuer/audience and return claims."""
    s = get_settings()
    signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=s.google_client_id,
        options={"require": ["sub", "email", "exp"]},
    )
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise jwt.InvalidIssuerError("unexpected issuer")
    if not claims.get("email_verified", False):
        raise ValueError("Google account email is not verified")
    return claims


def _upsert_google_user(email: str, guest_id: str | None) -> tuple[str, object, object]:
    """Find, create, or upgrade the account for a verified Google email.
    Returns (user_id, org_id, deal_id)."""
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT id FROM users WHERE lower(email) = %s AND is_anonymous = false LIMIT 1",
            (email,),
        )
        existing = cur.fetchone()

        if existing:
            # Returning Google user — log them into their own workspace.
            user_id = existing["id"]
            org_id, deal_id = first_workspace(cur, user_id)
            if org_id is None:
                org_id, deal_id = provision_workspace(
                    cur, user_id, "Workspace", f"org-{user_id}", "First proposal"
                )
            return str(user_id), org_id, deal_id

        if guest_id:
            # Upgrade the guest in place — keeps their id, org and uploads.
            cur.execute(
                "UPDATE users SET email = %s, is_anonymous = false "
                "WHERE id = %s AND is_anonymous = true RETURNING id",
                (email, guest_id),
            )
            if cur.fetchone():
                cur.execute("UPDATE team_members SET email = %s WHERE user_id = %s", (email, guest_id))
                org_id, deal_id = first_workspace(cur, guest_id)
                if org_id is None:
                    org_id, deal_id = provision_workspace(
                        cur, guest_id, "Workspace", f"org-{guest_id}", "First proposal"
                    )
                return guest_id, org_id, deal_id

        # Fresh account (no guest to upgrade, no prior Google user). No
        # password_hash — this account authenticates only via Google.
        user_id = new_user_id()
        cur.execute(
            "INSERT INTO users (id, email, is_anonymous) VALUES (%s, %s, false)", (user_id, email)
        )
        org_id, deal_id = provision_workspace(
            cur, user_id, "Workspace", f"org-{user_id}", "First proposal"
        )
        return user_id, org_id, deal_id


@router.get("/callback")
async def google_callback(
    code: str = Query(default=""), state: str = Query(default="")
) -> RedirectResponse:
    if not get_settings().google_enabled:
        raise AuthError(503, "Google sign-in is not configured.")
    if not code or not state:
        return _fail_redirect("missing_code")

    try:
        guest_id = _read_state(state)
    except jwt.InvalidTokenError:
        return _fail_redirect("bad_state")

    try:
        id_token = await _exchange_code(code)
        claims = _verify_id_token(id_token)
    except (ValueError, jwt.InvalidTokenError, httpx.HTTPError):
        return _fail_redirect("verify_failed")

    email = normalize_email(claims["email"])
    user_id, _org_id, _deal_id = _upsert_google_user(email, guest_id)
    token = mint_account_token(user_id)

    # Hand the token to the SPA via a fragment — fragments aren't sent to
    # servers or logged in access logs, unlike a query string.
    base = get_settings().google_post_login_redirect
    return RedirectResponse(f"{base}#access_token={urllib.parse.quote(token)}&link=ok")
