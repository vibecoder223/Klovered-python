"""Session bootstrap and real accounts (replaces Supabase Auth + /api/session).

Three entry points:

* ``POST /api/auth/guest``  — anonymous session + a throwaway workspace, so the
  free tool is usable with no account at all.
* ``POST /api/auth/signup`` — create a real account. If the caller presents a
  guest token, that guest row is upgraded IN PLACE (same user id → same org →
  every uploaded doc carries over). Otherwise a fresh account + workspace is
  provisioned. This is what makes "sign up, then land in the tool with your
  work already there" work.
* ``POST /api/auth/login``  — sign in to an existing account.

All three return the same shape, so the client treats a guest and an account
identically apart from `is_anonymous`.
"""

from fastapi import APIRouter, Depends, Header
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
    verify_token,
)
from ..deps import GuestContext, require_guest
from ..provisioning import first_workspace as _first_workspace
from ..provisioning import provision_workspace as _provision_workspace

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    email: str
    password: str


def _guest_id_from_header(authorization: str) -> str | None:
    """The caller's guest id, if they present a valid anonymous token. Anything
    else (no header, an expired token, an already-permanent account) returns
    None so signup just provisions fresh instead of failing."""
    if not authorization.lower().startswith("bearer "):
        return None
    try:
        claims = verify_token(authorization[7:].strip())
    except AuthError:
        return None
    return claims["sub"] if claims.get("is_anonymous") else None


@router.post("/guest")
async def create_guest() -> dict:
    user_id = new_user_id()
    with db.admin_tx() as cur:
        cur.execute("INSERT INTO users (id, is_anonymous) VALUES (%s, true)", (user_id,))
        org_id, deal_id = _provision_workspace(
            cur, user_id, "Guest workspace", f"guest-{user_id}", "Free tool session"
        )
    return {
        "access_token": mint_guest_token(user_id),
        "user_id": user_id,
        "org_id": str(org_id),
        "deal_id": str(deal_id),
        "email": None,
        "is_anonymous": True,
    }


@router.post("/signup")
async def signup(body: Credentials, authorization: str = Header(default="")) -> dict:
    email = validate_email(body.email)
    password = validate_password(body.password)
    password_hash = hash_password(password)
    guest_id = _guest_id_from_header(authorization)

    try:
        with db.admin_tx() as cur:
            if guest_id:
                # Upgrade the guest in place. The WHERE guards against a race
                # (or a replayed token) upgrading the same row twice.
                cur.execute(
                    "UPDATE users SET email = %s, password_hash = %s, is_anonymous = false "
                    "WHERE id = %s AND is_anonymous = true RETURNING id",
                    (email, password_hash, guest_id),
                )
                upgraded = cur.fetchone()
                if upgraded:
                    cur.execute(
                        "UPDATE team_members SET email = %s WHERE user_id = %s", (email, guest_id)
                    )
                    org_id, deal_id = _first_workspace(cur, guest_id)
                    if org_id is None:
                        # Token was valid but the workspace is gone (e.g. the
                        # 48h purge ran). Give them a fresh one rather than
                        # returning an account that resolves no org.
                        org_id, deal_id = _provision_workspace(
                            cur, guest_id, "Workspace", f"org-{guest_id}", "First proposal"
                        )
                    user_id = guest_id
                else:
                    guest_id = None  # fall through to a fresh account

            if not guest_id:
                user_id = new_user_id()
                cur.execute(
                    "INSERT INTO users (id, email, password_hash, is_anonymous) "
                    "VALUES (%s, %s, %s, false)",
                    (user_id, email, password_hash),
                )
                org_id, deal_id = _provision_workspace(
                    cur, user_id, "Workspace", f"org-{user_id}", "First proposal"
                )
    except UniqueViolation:
        raise AuthError(409, "An account with that email already exists. Sign in instead.")

    return {
        "access_token": mint_account_token(user_id),
        "user_id": str(user_id),
        "org_id": str(org_id),
        "deal_id": str(deal_id) if deal_id else None,
        "email": email,
        "is_anonymous": False,
    }


@router.get("/me")
async def me(ctx: GuestContext = Depends(require_guest)) -> dict:
    """Current session. The client uses this to decide whether to show a
    "sign in" CTA or the account badge."""
    with db.user_tx(ctx.user_id) as cur:
        cur.execute("SELECT email, is_anonymous FROM users WHERE id = %s", (ctx.user_id,))
        row = cur.fetchone()
    return {
        "user_id": ctx.user_id,
        "org_id": ctx.org_id,
        "email": (row["email"] or None) if row else None,
        "is_anonymous": bool(row["is_anonymous"]) if row else ctx.is_anonymous,
    }


@router.post("/login")
async def login(body: Credentials) -> dict:
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

    return {
        "access_token": mint_account_token(str(user_id)),
        "user_id": str(user_id),
        "org_id": str(org_id),
        "deal_id": str(deal_id) if deal_id else None,
        "email": email,
        "is_anonymous": False,
    }
