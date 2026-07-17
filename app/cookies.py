"""Session cookie — set on login/signup/guest, read by every authenticated
route. httponly (JS can't read it, so no XSS token theft) + samesite=lax
(sent on top-level navigation like the marketing->tool redirect, not on
cross-site requests). Works because marketing, the tool, and the API are all
served from the SAME domain via path routing (see Caddyfile) — a cookie set
by one is automatically sent to the others.
"""

from fastapi import Response

from .config import get_settings


def set_session_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.session_cookie_name,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        secure=s.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(key=s.session_cookie_name, path="/")
