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

import secrets
from datetime import datetime, timedelta, timezone

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


# ---------- light share: invite exactly one collaborator ----------

_INVITE_TTL_DAYS = 7
_MAX_MEMBERS = 2  # the free tool's cap: the owner + one invited collaborator


class InviteAccept(BaseModel):
    token: str


@router.post("/invite")
async def create_invite(ctx: GuestContext = Depends(require_guest)) -> dict:
    """Signed-in owner mints a single-use share link for their workspace. Capped
    at one collaborator; re-clicking returns the existing live invite so the link
    is stable."""
    if ctx.is_anonymous:
        raise AuthError(403, "Sign in to invite a collaborator.")
    with db.admin_tx() as cur:
        cur.execute("SELECT count(*) AS n FROM team_members WHERE org_id = %s", (ctx.org_id,))
        if cur.fetchone()["n"] >= _MAX_MEMBERS:
            raise AuthError(409, "This workspace already has a collaborator (limit 2).")

        cur.execute(
            "SELECT token, expires_at FROM invites "
            "WHERE org_id = %s AND accepted_at IS NULL AND expires_at > now() "
            "ORDER BY created_at DESC LIMIT 1",
            (ctx.org_id,),
        )
        existing = cur.fetchone()
        if existing:
            return {"token": existing["token"], "expires_at": existing["expires_at"].isoformat()}

        cur.execute(
            "SELECT id FROM deals WHERE org_id = %s ORDER BY created_at DESC LIMIT 1", (ctx.org_id,)
        )
        deal = cur.fetchone()
        token = secrets.token_urlsafe(24)
        expires = datetime.now(timezone.utc) + timedelta(days=_INVITE_TTL_DAYS)
        cur.execute(
            "INSERT INTO invites (token, org_id, deal_id, created_by, expires_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (token, ctx.org_id, deal["id"] if deal else None, ctx.user_id, expires),
        )
    return {"token": token, "expires_at": expires.isoformat()}


@router.post("/invite/accept")
async def accept_invite(body: InviteAccept, ctx: GuestContext = Depends(require_guest)) -> dict:
    """A signed-in invitee joins the shared workspace. Enforces the 2-member cap
    and single use. Returns the shared org + deal so the tool can open it."""
    if ctx.is_anonymous:
        raise AuthError(403, "Sign in to accept the invite.")
    now = datetime.now(timezone.utc)
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT id, org_id, deal_id, accepted_at, expires_at FROM invites WHERE token = %s",
            (body.token,),
        )
        inv = cur.fetchone()
        if not inv:
            raise AuthError(404, "This invite link is not valid.")
        if inv["expires_at"] < now:
            raise AuthError(410, "This invite link has expired.")

        org_id = inv["org_id"]
        cur.execute(
            "SELECT 1 FROM team_members WHERE org_id = %s AND user_id = %s", (org_id, ctx.user_id)
        )
        already_member = cur.fetchone() is not None

        if not already_member:
            if inv["accepted_at"] is not None:
                raise AuthError(409, "This invite has already been used.")
            cur.execute("SELECT count(*) AS n FROM team_members WHERE org_id = %s", (org_id,))
            if cur.fetchone()["n"] >= _MAX_MEMBERS:
                raise AuthError(409, "This shared workspace is full.")
            cur.execute("SELECT email FROM users WHERE id = %s", (ctx.user_id,))
            u = cur.fetchone()
            cur.execute(
                "INSERT INTO team_members (org_id, user_id, role, email, name) "
                "VALUES (%s, %s, 'collaborator', %s, 'Collaborator')",
                (org_id, ctx.user_id, (u["email"] if u else "") or ""),
            )
            cur.execute(
                "UPDATE invites SET accepted_by = %s, accepted_at = now() WHERE id = %s",
                (ctx.user_id, inv["id"]),
            )

    return {"org_id": str(org_id), "deal_id": str(inv["deal_id"]) if inv["deal_id"] else None}
