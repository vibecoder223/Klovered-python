"""End-to-end tests against a real Postgres (schema applied). Skipped when no
DATABASE_URL is set; run in CI and against local docker compose."""

from tests.conftest import requires_db

pytestmark = requires_db

SAMPLE = ("rfp.txt", b"Scope of work: managed IT services for 24 months.", "text/plain")


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_guest_provisioning_and_whoami(client, guest):
    g = guest()
    assert g["org_id"] and g["deal_id"]
    r = client.get("/api/pipeline/whoami", headers=_auth(g["access_token"]))
    assert r.status_code == 200
    assert r.json()["org_id"] == g["org_id"]
    assert r.json()["is_anonymous"] is True


def test_whoami_without_token_is_401(client):
    r = client.get("/api/pipeline/whoami")
    assert r.status_code == 401
    assert r.json() == {"error": "No session"}


def test_upload_and_one_rfp_cap(client, guest):
    g = guest()
    files = {"file": SAMPLE}
    r = client.post(
        "/api/pipeline/documents/upload",
        headers=_auth(g["access_token"]),
        data={"deal_id": g["deal_id"]},
        files=files,
    )
    assert r.status_code == 200, r.text
    assert r.json()["document"]["deal_id"] == g["deal_id"]

    # Second upload to the same session is capped.
    r2 = client.post(
        "/api/pipeline/documents/upload",
        headers=_auth(g["access_token"]),
        data={"deal_id": g["deal_id"]},
        files={"file": SAMPLE},
    )
    assert r2.status_code == 403


def test_cross_tenant_isolation(client, guest):
    a = guest()
    b = guest()
    assert a["org_id"] != b["org_id"]

    # B is a different org.
    rb = client.get("/api/pipeline/whoami", headers=_auth(b["access_token"]))
    assert rb.json()["org_id"] == b["org_id"]

    # B cannot upload into A's deal — RLS hides it, so it reads as "not found".
    r = client.post(
        "/api/pipeline/documents/upload",
        headers=_auth(b["access_token"]),
        data={"deal_id": a["deal_id"]},
        files={"file": SAMPLE},
    )
    assert r.status_code == 404


def test_parse_probe(client, guest):
    g = guest()
    r = client.post(
        "/api/pipeline/parse",
        headers=_auth(g["access_token"]),
        files={"file": SAMPLE},
    )
    assert r.status_code == 200
    assert r.json()["block_count"] >= 1
    assert r.json()["parse_ms"] >= 0
