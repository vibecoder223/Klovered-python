from dataclasses import dataclass

from fastapi import Cookie, Header

from . import db
from .auth import AuthError, verify_token
from .config import get_settings


@dataclass
class GuestContext:
    token: str
    user_id: str
    org_id: str
    is_anonymous: bool


def _extract_token(authorization: str, session_cookie: str | None) -> str:
    """Authorization header wins (API clients, curl, tests); the session
    cookie is the fallback (browser requests from marketing/the tool, which
    never set the header — they rely on the cookie set at login/signup)."""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if session_cookie:
        return session_cookie
    raise AuthError(401, "No session")


async def require_guest(
    authorization: str = Header(default=""),
    session_cookie: str | None = Cookie(default=None, alias=get_settings().session_cookie_name),
) -> GuestContext:
    token = _extract_token(authorization, session_cookie)
    claims = verify_token(token)
    user_id = claims["sub"]

    # Resolve org on the RLS-enforced connection: the caller can only ever see
    # their own membership row.
    with db.user_tx(user_id) as cur:
        cur.execute(
            "SELECT org_id FROM team_members WHERE user_id = %s LIMIT 1", (user_id,)
        )
        row = cur.fetchone()

    if not row:
        raise AuthError(409, "Session not provisioned")
    return GuestContext(
        token=token,
        user_id=user_id,
        org_id=str(row["org_id"]),
        is_anonymous=bool(claims.get("is_anonymous", False)),
    )
