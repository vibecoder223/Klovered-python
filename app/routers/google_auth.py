"""Google OAuth — the self-hosted replacement for Supabase's linkIdentity leg.

Flow (Authorization Code):
  GET  /api/auth/google/start     -> redirect the browser to Google's consent
  GET  /api/auth/google/callback  -> Google returns here with ?code&state;
                                     we exchange the code, verify the ID token,
                                     find/create the user, mint a token, set
                                     the session cookie, and redirect into
                                     the app already logged in.

State is a short-lived signed JWT (same HS256 secret as sessions) carrying
only a nonce, so there's no server-side session store and the callback is
CSRF-protected. The Google *client secret* is read from env only.

Like email signup, Google login always resolves to either an EXISTING
account or a FRESH one — there is no guest-to-account carryover (only
signed-in accounts persist data; guest work is intentionally left to the
48h purge).
"""

import time
import urllib.parse

import httpx
import jwt
from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from .. import db
from ..auth import AuthError, mint_account_token, new_user_id, normalize_email
from ..config import get_settings
from ..cookies import set_session_cookie
from ..deps import GuestContext, optional_guest
from ..provisioning import first_workspace, provision_workspace

router = APIRouter(prefix="/api/auth/google", tags=["auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
_STATE_TTL_SECONDS = 600  # 10 min — a login should complete well within this

# One JWKS client process-wide; PyJWT caches Google's signing keys internally.
_jwks_client = jwt.PyJWKClient(_GOOGLE_JWKS_URL)


def _sign_state() -> str:
    now = int(time.time())
    return jwt.encode(
        {"nonce": new_user_id(), "iat": now, "exp": now + _STATE_TTL_SECONDS},
        get_settings().auth_jwt_secret,
        algorithm="HS256",
    )


def _verify_state(state: str) -> None:
    """Raises on a tampered/expired state; the nonce itself is never checked
    against anything (single-use is not required — it's expiry + signature
    that make this CSRF-safe, matching the guest/account cookie's own model)."""
    jwt.decode(state, get_settings().auth_jwt_secret, algorithms=["HS256"])


@router.get("/start")
async def google_start() -> RedirectResponse:
    s = get_settings()
    if not s.google_enabled:
        raise AuthError(503, "Google sign-in is not configured.")
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": _sign_state(),
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")


def _fail_redirect(reason: str) -> RedirectResponse:
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


def _resolve_google_user(email: str, guest: GuestContext | None) -> str:
    """Returns the user id for a verified Google email. Resolution order:
    1. an EXISTING account with that email wins (sign in to it);
    2. else, if the caller is an anonymous guest, UPGRADE that guest in place —
       same id, same org, same data — now a permanent Google account;
    3. else, a fresh Google-only account (no password_hash).
    """
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT id FROM users WHERE lower(email) = %s AND is_anonymous = false LIMIT 1",
            (email,),
        )
        existing = cur.fetchone()
        if existing:
            user_id = existing["id"]
            org_id, _ = first_workspace(cur, user_id)
            if org_id is None:
                provision_workspace(cur, user_id, "Workspace", f"org-{user_id}", "First proposal")
            return str(user_id)

        if guest and guest.is_anonymous:
            cur.execute(
                "UPDATE users SET email = %s, is_anonymous = false "
                "WHERE id = %s AND is_anonymous = true RETURNING id",
                (email, guest.user_id),
            )
            row = cur.fetchone()
            if row:  # guest upgraded in place, org + data kept
                return str(row["id"])

        user_id = new_user_id()
        cur.execute(
            "INSERT INTO users (id, email, is_anonymous) VALUES (%s, %s, false)", (user_id, email)
        )
        provision_workspace(cur, user_id, "Workspace", f"org-{user_id}", "First proposal")
        return user_id


@router.get("/callback")
async def google_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    guest: GuestContext | None = Depends(optional_guest),
) -> RedirectResponse:
    if not get_settings().google_enabled:
        raise AuthError(503, "Google sign-in is not configured.")
    if not code or not state:
        return _fail_redirect("missing_code")

    try:
        _verify_state(state)
    except jwt.InvalidTokenError:
        return _fail_redirect("bad_state")

    try:
        id_token = await _exchange_code(code)
        claims = _verify_id_token(id_token)
    except (ValueError, jwt.InvalidTokenError, httpx.HTTPError):
        return _fail_redirect("verify_failed")

    email = normalize_email(claims["email"])
    user_id = _resolve_google_user(email, guest)
    token = mint_account_token(user_id)

    response = RedirectResponse(get_settings().google_post_login_redirect)
    set_session_cookie(response, token, get_settings().auth_account_token_ttl_seconds)
    return response
