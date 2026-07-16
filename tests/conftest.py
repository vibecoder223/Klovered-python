import os

import pytest
from fastapi.testclient import TestClient

HAS_DB = bool(os.getenv("DATABASE_URL") and os.getenv("ADMIN_DATABASE_URL"))
requires_db = pytest.mark.skipif(not HAS_DB, reason="no database configured")


@pytest.fixture(scope="session")
def client():
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def guest(client):
    """Provision a fresh guest and return its token + ids."""

    def _make():
        r = client.post("/api/auth/guest")
        assert r.status_code == 200, r.text
        return r.json()

    return _make
