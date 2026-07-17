"""Self-issued auth — guest sessions AND real accounts (replaces Supabase Auth).

Only this service mints and verifies tokens, so a single HS256 secret is enough
— no JWKS round-trip. Guests and account holders are both a uuid carried as
`sub`; `is_anonymous` in the claims is what separates them, and that's what
callers check — never which mint function produced the token.
"""

import re
import time
import uuid

import bcrypt
import jwt

from .config import get_settings

# Deliberately permissive: real address validity is proven by delivery, not by
# a regex. This only rejects obvious junk before it reaches the database.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MIN_PASSWORD_LENGTH = 8
# bcrypt silently truncates at 72 bytes; reject longer input rather than let a
# user believe the tail of their passphrase is protecting anything.
MAX_PASSWORD_BYTES = 72


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def new_user_id() -> str:
    return str(uuid.uuid4())


# ---------- email / password ----------


def normalize_email(email: str) -> str:
    """Lowercase + strip. Must match the `lower(email)` unique index in
    schema.sql, or two rows differing only by case would both insert."""
    return email.strip().lower()


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise AuthError(400, "Enter a valid email address.")
    return email


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise AuthError(400, "Password is too long (max 72 bytes).")
    return password


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-time check. A user row with no password_hash (a guest, or an
    account created via a future OAuth-only path) can never match."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash in the column — treat as a failed login rather
        # than a 500, so one bad row can't hand out an error oracle.
        return False


# ---------- tokens ----------


def _mint(user_id: str, is_anonymous: bool, ttl: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "is_anonymous": is_anonymous, "iat": now, "exp": now + ttl},
        get_settings().auth_jwt_secret,
        algorithm="HS256",
    )


def mint_guest_token(user_id: str) -> str:
    return _mint(user_id, True, get_settings().auth_token_ttl_seconds)


def mint_account_token(user_id: str) -> str:
    return _mint(user_id, False, get_settings().auth_account_token_ttl_seconds)


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
