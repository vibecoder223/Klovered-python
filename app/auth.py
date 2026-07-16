"""Self-issued guest auth (replaces Supabase Auth).

Only this service mints and verifies tokens, so a single HS256 secret is enough
— no JWKS round-trip. A guest is a random uuid; the token carries it as `sub`.
"""

import time
import uuid

import jwt

from .config import get_settings


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def new_user_id() -> str:
    return str(uuid.uuid4())


def mint_guest_token(user_id: str) -> str:
    s = get_settings()
    now = int(time.time())
    payload = {
        "sub": user_id,
        "is_anonymous": True,
        "iat": now,
        "exp": now + s.auth_token_ttl_seconds,
    }
    return jwt.encode(payload, s.auth_jwt_secret, algorithm="HS256")


def verify_token(token: str) -> dict:
    s = get_settings()
    try:
        claims = jwt.decode(
            token,
            s.auth_jwt_secret,
            algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "Session expired")
    except jwt.InvalidTokenError as exc:
        raise AuthError(401, f"Invalid session: {exc}")
    if not claims.get("sub"):
        raise AuthError(401, "No session")
    return claims
