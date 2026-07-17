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


# ---------- guest-upgrade resolver (no DB — admin_tx is faked) ----------
class _FakeCur:
    """Records executed SQL and hands back queued fetchone() results in order."""

    def __init__(self, fetch_results):
        self._results = list(fetch_results)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._results.pop(0) if self._results else None


class _FakeTx:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc):
        return False


def _guest_ctx(user_id, is_anonymous=True):
    from app.deps import GuestContext

    return GuestContext(token="t", user_id=user_id, org_id="o", is_anonymous=is_anonymous)


def test_resolve_prefers_existing_account_over_guest(monkeypatch):
    from app.routers import google_auth as g

    cur = _FakeCur([{"id": "acct-1"}])  # SELECT existing account -> found
    monkeypatch.setattr(g.db, "admin_tx", lambda: _FakeTx(cur))
    monkeypatch.setattr(g, "first_workspace", lambda c, uid: ("org-1", None))
    assert g._resolve_google_user("known@ex.com", _guest_ctx("guest-9")) == "acct-1"


def test_resolve_upgrades_guest_when_email_new(monkeypatch):
    from app.routers import google_auth as g

    cur = _FakeCur([None, {"id": "guest-9"}])  # no existing; UPDATE ... RETURNING id
    monkeypatch.setattr(g.db, "admin_tx", lambda: _FakeTx(cur))
    assert g._resolve_google_user("new@ex.com", _guest_ctx("guest-9")) == "guest-9"
    assert any("UPDATE users" in sql for sql, _ in cur.executed)


def test_resolve_creates_fresh_when_no_guest(monkeypatch):
    from app.routers import google_auth as g

    cur = _FakeCur([None])  # no existing account, no guest -> fresh insert
    monkeypatch.setattr(g.db, "admin_tx", lambda: _FakeTx(cur))
    monkeypatch.setattr(g, "provision_workspace", lambda *a, **k: ("org-z", "deal-z"))
    monkeypatch.setattr(g, "new_user_id", lambda: "fresh-1")
    assert g._resolve_google_user("solo@ex.com", None) == "fresh-1"
    assert any("INSERT INTO users" in sql for sql, _ in cur.executed)
