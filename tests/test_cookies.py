"""Unit tests for the session cookie helper — no database, always run."""

from fastapi import Response

from app.config import get_settings
from app.cookies import clear_session_cookie, set_session_cookie


def _set_cookie_header(response: Response) -> str:
    values = [v for k, v in response.raw_headers if k == b"set-cookie"]
    assert values, "no Set-Cookie header was written"
    return values[0].decode("latin-1")


def test_set_session_cookie_carries_the_token():
    r = Response()
    set_session_cookie(r, "the-token-value", max_age_seconds=3600)
    header = _set_cookie_header(r)
    assert f"{get_settings().session_cookie_name}=the-token-value" in header


def test_set_session_cookie_is_httponly_and_samesite_lax():
    r = Response()
    set_session_cookie(r, "tok", max_age_seconds=3600)
    header = _set_cookie_header(r).lower()
    assert "httponly" in header
    assert "samesite=lax" in header


def test_set_session_cookie_scopes_to_root_path():
    r = Response()
    set_session_cookie(r, "tok", max_age_seconds=3600)
    header = _set_cookie_header(r)
    assert "Path=/" in header


def test_clear_session_cookie_expires_it():
    r = Response()
    clear_session_cookie(r)
    header = _set_cookie_header(r)
    assert f"{get_settings().session_cookie_name}=" in header
    # Deleting a cookie is done via an immediately-expired Max-Age/Expires.
    assert "Max-Age=0" in header or "expires=Thu, 01 Jan 1970" in header
