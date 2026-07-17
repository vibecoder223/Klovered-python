"""Unit tests for Google OAuth's stateless CSRF-state handling and the
disabled-by-default guard — no database, no network, always run."""

import time

import jwt
import pytest

from app.config import get_settings
from app.routers import google_auth


def _secret() -> str:
    return get_settings().auth_jwt_secret


# ---------- signed state (CSRF) ----------
def test_state_roundtrips_guest_id():
    state = google_auth._sign_state("guest-123")
    assert google_auth._read_state(state) == "guest-123"


def test_state_roundtrips_none_guest():
    state = google_auth._sign_state(None)
    assert google_auth._read_state(state) is None


def test_state_is_unique_per_call():
    # A fresh nonce each time — two states for the same guest must differ, so a
    # captured state can't be trivially recognized/replayed as a constant.
    assert google_auth._sign_state("g") != google_auth._sign_state("g")


def test_tampered_state_is_rejected():
    state = google_auth._sign_state("guest-123")
    with pytest.raises(jwt.InvalidTokenError):
        google_auth._read_state(state + "x")


def test_expired_state_is_rejected():
    # Hand-mint a state dated past its TTL and confirm it won't verify.
    now = int(time.time())
    stale = jwt.encode(
        {"nonce": "n", "guest_id": "g", "iat": now - 3600, "exp": now - 60},
        _secret(),
        algorithm="HS256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        google_auth._read_state(stale)


def test_state_signed_with_other_secret_is_rejected():
    forged = jwt.encode(
        {"nonce": "n", "guest_id": "g", "iat": int(time.time()), "exp": int(time.time()) + 600},
        "not-the-real-secret",
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        google_auth._read_state(forged)


# ---------- guest-token extraction ----------
def test_guest_id_none_without_bearer():
    assert google_auth._guest_id("") is None
    assert google_auth._guest_id("Basic abc") is None


def test_guest_id_none_for_account_token():
    # An already-permanent account token carries is_anonymous=False, so there's
    # no guest to upgrade — must return None, not the account's id.
    from app.auth import mint_account_token, new_user_id

    tok = mint_account_token(new_user_id())
    assert google_auth._guest_id(f"Bearer {tok}") is None


def test_guest_id_extracts_from_guest_token():
    from app.auth import mint_guest_token, new_user_id

    uid = new_user_id()
    assert google_auth._guest_id(f"Bearer {mint_guest_token(uid)}") == uid


# ---------- disabled-by-default guard ----------
def test_google_disabled_when_unconfigured(client):
    # No GOOGLE_CLIENT_ID/SECRET in the test env -> both legs refuse with 503
    # rather than redirecting into a broken Google flow.
    assert get_settings().google_enabled is False
    r = client.get("/api/auth/google/start", follow_redirects=False)
    assert r.status_code == 503
    r = client.get("/api/auth/google/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 503
