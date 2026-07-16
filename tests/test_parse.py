import fitz
import pytest
from fastapi.testclient import TestClient

from app import deps
from app.main import app
from app.pipeline.parse import parse_document

client = TestClient(app, raise_server_exceptions=False)


def _make_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    return doc.tobytes()


def test_parse_txt_splits_paragraphs():
    parsed = parse_document(b"Hello world.\n\nSecond para here.", "text/plain", "a.txt")
    assert parsed.page_count == 1
    assert [b.type for b in parsed.blocks] == ["paragraph", "paragraph"]
    assert parsed.blocks[0].text == "Hello world."


def test_parse_pdf_extracts_text():
    pdf = _make_pdf("The quick brown fox jumps over the lazy dog repeatedly today.")
    parsed = parse_document(pdf, "application/pdf", "a.pdf")
    assert parsed.page_count == 1
    assert "quick brown fox" in parsed.raw_text


def test_unsupported_type_raises():
    with pytest.raises(ValueError):
        parse_document(b"x", "image/png", "a.png")


@pytest.fixture
def stub_auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_jwt", lambda token: {"sub": "g", "is_anonymous": True})
    monkeypatch.setattr(deps, "resolve_org", lambda token, uid: "org-1")


def test_parse_endpoint_returns_timing(stub_auth):
    files = {"file": ("a.txt", b"Alpha.\n\nBeta gamma.", "text/plain")}
    r = client.post(
        "/api/pipeline/parse", files=files, headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["block_count"] == 2
    assert body["parse_ms"] >= 0
