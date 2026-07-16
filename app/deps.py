from dataclasses import dataclass

from fastapi import Header

from .auth import AuthError, verify_jwt
from .supabase_rest import resolve_org


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
    claims = verify_jwt(token)
    org_id = resolve_org(token, claims["sub"])
    if not org_id:
        raise AuthError(409, "Session not provisioned")
    return GuestContext(
        token=token,
        user_id=claims["sub"],
        org_id=org_id,
        is_anonymous=bool(claims.get("is_anonymous", False)),
    )
