import httpx
import respx

from app.config import get_settings
from app.supabase_rest import resolve_org


@respx.mock
def test_user_client_sends_anon_apikey_and_user_bearer(monkeypatch):
    monkeypatch.setenv("NEXT_PUBLIC_SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "anon-key")
    get_settings.cache_clear()

    route = respx.get("https://proj.supabase.co/rest/v1/team_members").mock(
        return_value=httpx.Response(200, json=[{"org_id": "org-9"}])
    )
    org = resolve_org("guest-jwt", "guest-abc")

    assert org == "org-9"
    sent = route.calls.last.request
    assert sent.headers["apikey"] == "anon-key"
    assert sent.headers["authorization"] == "Bearer guest-jwt"


@respx.mock
def test_resolve_org_returns_none_when_no_membership(monkeypatch):
    monkeypatch.setenv("NEXT_PUBLIC_SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "anon-key")
    get_settings.cache_clear()

    respx.get("https://proj.supabase.co/rest/v1/team_members").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert resolve_org("guest-jwt", "nobody") is None
