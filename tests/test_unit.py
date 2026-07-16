"""Pure unit tests — no database, always run."""

import time

import fitz
import pytest

from app import auth
from app.auth import AuthError, mint_guest_token, verify_token
from app.pipeline.parse import parse_document


# ---------- auth ----------
def test_token_roundtrip():
    uid = auth.new_user_id()
    claims = verify_token(mint_guest_token(uid))
    assert claims["sub"] == uid
    assert claims["is_anonymous"] is True


def test_tampered_token_rejected():
    tok = mint_guest_token(auth.new_user_id())
    with pytest.raises(AuthError):
        verify_token(tok + "x")


def test_expired_token_rejected(monkeypatch):
    uid = auth.new_user_id()
    # Mint a token dated before the TTL window so it's already expired.
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 3 * 24 * 3600)
    tok = mint_guest_token(uid)
    monkeypatch.setattr(time, "time", real)
    with pytest.raises(AuthError) as e:
        verify_token(tok)
    assert e.value.status == 401


# ---------- parse ----------
def _pdf(text: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text, fontsize=12)
    return doc.tobytes()


def test_parse_txt():
    parsed = parse_document(b"Alpha.\n\nBeta gamma.", "text/plain", "a.txt")
    assert parsed.page_count == 1
    assert [b.type for b in parsed.blocks] == ["paragraph", "paragraph"]


def test_parse_pdf():
    parsed = parse_document(_pdf("The quick brown fox jumps today."), "application/pdf", "a.pdf")
    assert "quick brown fox" in parsed.raw_text


def test_parse_unsupported():
    with pytest.raises(ValueError):
        parse_document(b"x", "image/png", "a.png")
