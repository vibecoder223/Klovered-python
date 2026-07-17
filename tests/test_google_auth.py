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
def test_state_verifies():
    state = google_auth._sign_state()
    google_auth._verify_state(state)  # must not raise


def test_state_is_unique_per_call():
    # A fresh nonce each time — two states must differ, so a captured state
    # can't be trivially recognized/replayed as a constant.
    assert google_auth._sign_state() != google_auth._sign_state()


def test_tampered_state_is_rejected():
    state = google_auth._sign_state()
    with pytest.raises(jwt.InvalidTokenError):
        google_auth._verify_state(state + "x")


def test_expired_state_is_rejected():
    # Hand-mint a state dated past its TTL and confirm it won't verify.
    now = int(time.time())
    stale = jwt.encode(
        {"nonce": "n", "iat": now - 3600, "exp": now - 60}, _secret(), algorithm="HS256"
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        google_auth._verify_state(stale)


def test_state_signed_with_other_secret_is_rejected():
    forged = jwt.encode(
        {"nonce": "n", "iat": int(time.time()), "exp": int(time.time()) + 600},
        "not-the-real-secret",
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        google_auth._verify_state(forged)


# ---------- disabled-by-default guard ----------
def test_google_disabled_when_unconfigured(client):
    # No GOOGLE_CLIENT_ID/SECRET in the test env -> both legs refuse with 503
    # rather than redirecting into a broken Google flow.
    assert get_settings().google_enabled is False
    r = client.get("/api/auth/google/start", follow_redirects=False)
    assert r.status_code == 503
    r = client.get("/api/auth/google/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 503
