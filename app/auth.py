from functools import lru_cache

import jwt
from jwt import PyJWKClient

from .config import get_settings


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


@lru_cache
def _jwk_client() -> PyJWKClient:
    # Cached JWKS fetch + in-process key cache, like the TS getClaims() path.
    return PyJWKClient(get_settings().jwks_url)


def verify_jwt(token: str) -> dict:
    settings = get_settings()
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.supabase_jwt_aud,
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "Session expired")
    except jwt.InvalidTokenError as exc:
        raise AuthError(401, f"Invalid session: {exc}")
    if not claims.get("sub"):
        raise AuthError(401, "No session")
    return claims
