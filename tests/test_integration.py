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


# ---------- session cookie: the marketing<->tool hand-off ----------


def _unique_email() -> str:
    import uuid

    return f"cookie-test-{uuid.uuid4().hex[:12]}@example.com"


def test_signup_sets_cookie_that_authenticates_without_a_header(client):
    email = _unique_email()
    r = client.post("/api/auth/signup", json={"email": email, "password": "correcthorse123"})
    assert r.status_code == 200, r.text
    assert client.cookies.get("klovered_session")

    # No Authorization header at all — only the cookie the client now holds.
    r2 = client.get("/api/pipeline/whoami")
    assert r2.status_code == 200
    assert r2.json()["org_id"] == r.json()["org_id"]
    assert r2.json()["is_anonymous"] is False


def test_me_returns_org_and_deal(client, guest):
    g = guest()
    r = client.get("/api/auth/me", headers=_auth(g["access_token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org_id"] == g["org_id"]
    assert body["deal_id"] == g["deal_id"]  # tool resumes the workspace from this
    assert body["is_anonymous"] is True


def test_login_sets_cookie(client):
    email = _unique_email()
    client.post("/api/auth/signup", json={"email": email, "password": "correcthorse123"})
    client.cookies.clear()

    r = client.post("/api/auth/login", json={"email": email, "password": "correcthorse123"})
    assert r.status_code == 200, r.text
    assert client.cookies.get("klovered_session")


def test_signup_upgrades_guest_in_place(client, guest):
    """Journey C (restored): signing up while an anonymous guest session is
    present UPGRADES that guest in place — same user id, same org, so any work
    the guest already did carries into the account instead of being discarded."""
    g = guest()
    email = _unique_email()

    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "correcthorse123"},
        headers=_auth(g["access_token"]),  # guest token present -> upgrade it
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_anonymous"] is False
    assert body["email"] == email
    assert body["user_id"] == g["user_id"]  # same identity, kept
    assert body["org_id"] == g["org_id"]    # same workspace + data, kept


def test_signup_without_guest_creates_fresh_account(client):
    """No guest session -> a brand-new account + workspace."""
    email = _unique_email()
    r = client.post("/api/auth/signup", json={"email": email, "password": "correcthorse123"})
    assert r.status_code == 200, r.text
    assert r.json()["is_anonymous"] is False
    assert r.json()["org_id"]


def test_signup_duplicate_email_conflicts(client, guest):
    """An email already registered -> 409, even when a guest is upgrading (the
    UNIQUE(lower(email)) index rejects the UPDATE)."""
    email = _unique_email()
    client.post("/api/auth/signup", json={"email": email, "password": "correcthorse123"})
    client.cookies.clear()

    g = guest()
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "correcthorse123"},
        headers=_auth(g["access_token"]),
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["error"]


# ---------- answers read + document status/delete ----------


def test_deal_answers_empty_for_fresh_guest(client, guest):
    g = guest()
    r = client.get(f"/api/pipeline/deals/{g['deal_id']}/answers", headers=_auth(g["access_token"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"questions": [], "documents": []}


def test_deal_answers_isolation(client, guest):
    a = guest()
    b = guest()
    # B asks for A's deal — RLS hides it, so it reads as empty, never A's data.
    r = client.get(f"/api/pipeline/deals/{a['deal_id']}/answers", headers=_auth(b["access_token"]))
    assert r.status_code == 200
    assert r.json() == {"questions": [], "documents": []}


def test_document_status_and_delete_frees_cap(client, guest):
    g = guest()
    up = client.post(
        "/api/pipeline/documents/upload",
        headers=_auth(g["access_token"]),
        data={"deal_id": g["deal_id"]},
        files={"file": SAMPLE},
    )
    assert up.status_code == 200, up.text
    doc_id = up.json()["document"]["id"]

    st = client.get(f"/api/pipeline/documents/{doc_id}", headers=_auth(g["access_token"]))
    assert st.status_code == 200
    assert st.json()["processing_status"] == "uploaded"

    # Now the deal is at its one-RFP cap; a second upload is refused.
    cap = client.post(
        "/api/pipeline/documents/upload",
        headers=_auth(g["access_token"]),
        data={"deal_id": g["deal_id"]},
        files={"file": SAMPLE},
    )
    assert cap.status_code == 403

    dl = client.delete(f"/api/pipeline/documents/{doc_id}", headers=_auth(g["access_token"]))
    assert dl.status_code == 200

    # After delete the doc is gone and the cap is freed.
    assert client.get(f"/api/pipeline/documents/{doc_id}", headers=_auth(g["access_token"])).status_code == 404
    again = client.post(
        "/api/pipeline/documents/upload",
        headers=_auth(g["access_token"]),
        data={"deal_id": g["deal_id"]},
        files={"file": SAMPLE},
    )
    assert again.status_code == 200, again.text


def test_document_status_unknown_is_404(client, guest):
    g = guest()
    import uuid

    r = client.get(f"/api/pipeline/documents/{uuid.uuid4()}", headers=_auth(g["access_token"]))
    assert r.status_code == 404


def test_logout_clears_the_cookie(client):
    email = _unique_email()
    client.post("/api/auth/signup", json={"email": email, "password": "correcthorse123"})
    assert client.cookies.get("klovered_session")

    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert client.cookies.get("klovered_session") is None

    r2 = client.get("/api/pipeline/whoami")
    assert r2.status_code == 401
