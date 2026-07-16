import pytest
from fastapi.testclient import TestClient

from app import deps
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def stub_auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_jwt", lambda token: {"sub": "guest-abc", "is_anonymous": True})
    monkeypatch.setattr(deps, "resolve_org", lambda token, uid: "org-9")


def test_whoami_returns_identity(stub_auth):
    r = client.get("/api/pipeline/whoami", headers={"Authorization": "Bearer guest-jwt"})
    assert r.status_code == 200
    assert r.json() == {"user_id": "guest-abc", "org_id": "org-9", "is_anonymous": True}


def test_whoami_without_bearer_is_401():
    r = client.get("/api/pipeline/whoami")
    assert r.status_code == 401
    assert r.json() == {"error": "No session"}


def test_whoami_unprovisioned_is_409(monkeypatch):
    monkeypatch.setattr(deps, "verify_jwt", lambda token: {"sub": "guest-abc"})
    monkeypatch.setattr(deps, "resolve_org", lambda token, uid: None)
    r = client.get("/api/pipeline/whoami", headers={"Authorization": "Bearer guest-jwt"})
    assert r.status_code == 409
