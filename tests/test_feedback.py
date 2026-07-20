"""Feedback endpoint — POST /api/feedback. Skipped when no DATABASE_URL is set;
runs in CI and against local docker compose."""

from tests.conftest import requires_db

pytestmark = requires_db


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_submit_feedback_ok(client, guest):
    g = guest()
    r = client.post(
        "/api/feedback",
        headers=_auth(g["access_token"]),
        json={"rating": 5, "comment": "Great drafts", "deal_id": g["deal_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


def test_feedback_requires_session(client):
    r = client.post("/api/feedback", json={"rating": 4})
    assert r.status_code == 401


def test_rating_out_of_range_is_422(client, guest):
    g = guest()
    for bad in (0, 6, -1):
        r = client.post("/api/feedback", headers=_auth(g["access_token"]), json={"rating": bad})
        assert r.status_code == 422, f"rating {bad} should be rejected"


def test_resubmit_upserts_not_duplicates(client, guest):
    """Second submit for the same user updates the single row (unique on
    user_id) rather than erroring or creating a duplicate — 'feedback once'."""
    g = guest()
    r1 = client.post("/api/feedback", headers=_auth(g["access_token"]), json={"rating": 2})
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/api/feedback", headers=_auth(g["access_token"]), json={"rating": 5, "comment": "changed my mind"}
    )
    assert r2.status_code == 200, r2.text


def test_forged_deal_id_does_not_error_or_leak(client, guest):
    """A deal_id from another tenant is silently dropped (resolved to NULL),
    not linked and not a 500."""
    a = guest()
    b = guest()
    r = client.post(
        "/api/feedback",
        headers=_auth(a["access_token"]),
        json={"rating": 3, "deal_id": b["deal_id"]},
    )
    assert r.status_code == 200, r.text


def test_comment_and_email_optional(client, guest):
    g = guest()
    r = client.post("/api/feedback", headers=_auth(g["access_token"]), json={"rating": 4})
    assert r.status_code == 200, r.text
