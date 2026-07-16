from dataclasses import dataclass

from fastapi import Header

from . import db
from .auth import AuthError, verify_token


@dataclass
class GuestContext:
    token: str
    user_id: str
    org_id: str
    is_anonymous: bool


async def require_guest(authorization: str = Header(default="")) -> GuestContext:
    if not authorization.lower().startswith("bearer "):
        raise AuthError(401, "No session")
    token = authorization[7:].strip()
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
