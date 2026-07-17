import os

import pytest
from fastapi.testclient import TestClient

HAS_DB = bool(os.getenv("DATABASE_URL") and os.getenv("ADMIN_DATABASE_URL"))
requires_db = pytest.mark.skipif(not HAS_DB, reason="no database configured")


@pytest.fixture(scope="session")
def client():
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean_cookies(client):
    """The client fixture is session-scoped, so its cookie jar persists across
    every test. Clear it before each test so a session cookie set by one test
    can't silently authenticate another test's 'no token' request (e.g. the
    whoami-without-a-token 401 assertion). Tests that need a cookie set it
    within their own body, so clearing between tests is safe."""
    client.cookies.clear()
    yield


@pytest.fixture
def guest(client):
    """Provision a fresh guest and return its token + ids."""

    def _make():
        r = client.post("/api/auth/guest")
        assert r.status_code == 200, r.text
        return r.json()

    return _make
