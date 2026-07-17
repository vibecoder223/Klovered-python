"""Session bootstrap and real accounts (replaces Supabase Auth + /api/session).

Three entry points, each sets the shared session cookie (see app/cookies.py)
so the marketing site's /login and /signup pages and the tool at /app — same
domain, different paths — see the same logged-in state with no token in the
URL:

* ``POST /api/auth/guest``  — anonymous session + a throwaway workspace, so
  the tool is usable with no account. Guest data is never kept.
* ``POST /api/auth/signup`` — create a real account. Always starts CLEAN — a
  guest's in-progress work does NOT carry over. Only signed-in accounts have
  persistent data; that's the whole point of requiring an account at all.
* ``POST /api/auth/login``  — sign in to an existing account.

All three return the same shape, so the client treats a guest and an account
identically apart from `is_anonymous`. `access_token` is still returned (not
just the cookie) for non-browser callers — tests, curl, future native clients.
"""

from fastapi import APIRouter, Depends, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from .. import db
from ..auth import (
    AuthError,
    hash_password,
    mint_account_token,
    mint_guest_token,
    new_user_id,
    normalize_email,
    validate_email,
    validate_password,
    verify_password,
)
from ..config import get_settings
from ..cookies import clear_session_cookie, set_session_cookie
from ..deps import GuestContext, optional_guest, require_guest
from ..provisioning import first_workspace as _first_workspace
from ..provisioning import provision_workspace as _provision_workspace

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str
    password: str


@router.post("/guest")
async def create_guest(response: Response) -> dict:
    user_id = new_user_id()
    with db.admin_tx() as cur:
        cur.execute("INSERT INTO users (id, is_anonymous) VALUES (%s, true)", (user_id,))
        org_id, deal_id = _provision_workspace(
            cur, user_id, "Guest workspace", f"guest-{user_id}", "Free tool session"
        )
    token = mint_guest_token(user_id)
    set_session_cookie(response, token, get_settings().auth_token_ttl_seconds)
    return {
        "access_token": token,
        "user_id": user_id,
        "org_id": str(org_id),
        "deal_id": str(deal_id),
        "email": None,
        "is_anonymous": True,
    }


def _fresh_account(cur, email: str, password_hash: str) -> tuple:
    """Insert a brand-new account + workspace. Shared by the no-guest path and
    the purged-guest fallback so the two can't drift."""
    user_id = new_user_id()
    cur.execute(
        "INSERT INTO users (id, email, password_hash, is_anonymous) VALUES (%s, %s, %s, false)",
        (user_id, email, password_hash),
    )
    org_id, deal_id = _provision_workspace(cur, user_id, "Workspace", f"org-{user_id}", "First proposal")
    return user_id, org_id, deal_id


@router.post("/signup")
async def signup(
    body: Credentials,
    response: Response,
    guest: GuestContext | None = Depends(optional_guest),
) -> dict:
    """Create a real account. If the caller is currently an anonymous guest, we
    UPGRADE that guest in place — same user id, same org, same uploaded work —
    so nothing they did as a guest is lost. With no guest session, a fresh
    account + workspace is created."""
    email = validate_email(body.email)
    password = validate_password(body.password)
    password_hash = hash_password(password)

    try:
        with db.admin_tx() as cur:
            if guest and guest.is_anonymous:
                cur.execute(
                    "UPDATE users SET email = %s, password_hash = %s, is_anonymous = false "
                    "WHERE id = %s AND is_anonymous = true RETURNING id",
                    (email, password_hash, guest.user_id),
                )
                row = cur.fetchone()
                if row:  # guest still exists -> upgraded in place, keep their org + data
                    user_id = row["id"]
                    org_id, deal_id = _first_workspace(cur, user_id)
                    if org_id is None:
                        org_id, deal_id = _provision_workspace(
                            cur, user_id, "Workspace", f"org-{user_id}", "First proposal"
                        )
                else:  # guest row was purged mid-flight -> fall through to fresh
                    user_id, org_id, deal_id = _fresh_account(cur, email, password_hash)
            else:
                user_id, org_id, deal_id = _fresh_account(cur, email, password_hash)
    except UniqueViolation:
        raise AuthError(409, "An account with that email already exists. Sign in instead.")

    token = mint_account_token(str(user_id))
    set_session_cookie(response, token, get_settings().auth_account_token_ttl_seconds)
    return {
        "access_token": token,
        "user_id": str(user_id),
        "org_id": str(org_id),
        "deal_id": str(deal_id) if deal_id else None,
        "email": email,
        "is_anonymous": False,
    }


@router.get("/me")
async def me(ctx: GuestContext = Depends(require_guest)) -> dict:
    """Current session. The client uses this to decide whether to show a
    "sign in" CTA or the account badge, and to resume the workspace (org + most
    recent deal) without a separate call."""
    with db.user_tx(ctx.user_id) as cur:
        cur.execute("SELECT email, is_anonymous FROM users WHERE id = %s", (ctx.user_id,))
        row = cur.fetchone()
        cur.execute(
            "SELECT id FROM deals WHERE org_id = %s ORDER BY created_at DESC LIMIT 1",
            (ctx.org_id,),
        )
        deal = cur.fetchone()
    return {
        "user_id": ctx.user_id,
        "org_id": ctx.org_id,
        "deal_id": str(deal["id"]) if deal else None,
        "email": (row["email"] or None) if row else None,
        "is_anonymous": bool(row["is_anonymous"]) if row else ctx.is_anonymous,
    }


@router.post("/login")
async def login(body: Credentials, response: Response) -> dict:
    email = normalize_email(body.email)
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT id, password_hash FROM users "
            "WHERE lower(email) = %s AND is_anonymous = false LIMIT 1",
            (email,),
        )
        row = cur.fetchone()

        # One message for both "no such user" and "wrong password" — a
        # distinct error would let anyone enumerate registered emails.
        if not row or not verify_password(body.password, row["password_hash"]):
            raise AuthError(401, "Incorrect email or password.")

        user_id = row["id"]
        org_id, deal_id = _first_workspace(cur, user_id)
        if org_id is None:
            org_id, deal_id = _provision_workspace(
                cur, user_id, "Workspace", f"org-{user_id}", "First proposal"
            )

    token = mint_account_token(str(user_id))
    set_session_cookie(response, token, get_settings().auth_account_token_ttl_seconds)
    return {
        "access_token": token,
        "user_id": str(user_id),
        "org_id": str(org_id),
        "deal_id": str(deal_id) if deal_id else None,
        "email": email,
        "is_anonymous": False,
    }


@router.post("/logout")
async def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}
