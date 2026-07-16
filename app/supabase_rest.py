import httpx

from .config import get_settings


class SupabaseRest:
    """Thin PostgREST client. On the user path, `apikey` is the anon key and the
    Authorization bearer is the guest JWT, so Postgres RLS scopes every row to
    the guest's org. On the service path, both are the service-role key and RLS
    is bypassed (trusted worker code only)."""

    def __init__(self, bearer: str, *, is_service_role: bool = False):
        settings = get_settings()
        self._base = settings.postgrest_url
        apikey = (
            settings.supabase_service_role_key if is_service_role else settings.supabase_anon_key
        )
        self._headers = {
            "apikey": apikey,
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        }

    def get(self, table: str, params: dict) -> list[dict]:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{self._base}/{table}", headers=self._headers, params=params)
            resp.raise_for_status()
            return resp.json()

    def insert(self, table: str, rows: list[dict], *, prefer: str = "return=representation") -> list[dict]:
        headers = {**self._headers, "Content-Type": "application/json", "Prefer": prefer}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{self._base}/{table}", headers=headers, json=rows)
            resp.raise_for_status()
            return resp.json() if resp.content else []


class StorageClient:
    """Supabase Storage REST client. Uploads use the service-role key to sidestep
    storage.objects RLS edge cases, mirroring tryCreateAdminClient() in the TS
    upload route."""

    def __init__(self):
        settings = get_settings()
        self._base = settings.storage_url
        self._key = settings.supabase_service_role_key

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": content_type or "application/octet-stream",
            "x-upsert": "false",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{self._base}/object/{bucket}/{path}", headers=headers, content=data
            )
            resp.raise_for_status()

    def remove(self, bucket: str, path: str) -> None:
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}"}
        with httpx.Client(timeout=30.0) as client:
            client.request(
                "DELETE", f"{self._base}/object/{bucket}/{path}", headers=headers
            )


def user_client(token: str) -> SupabaseRest:
    return SupabaseRest(token)


def service_client() -> SupabaseRest:
    return SupabaseRest(get_settings().supabase_service_role_key, is_service_role=True)


def resolve_org(token: str, user_id: str) -> str | None:
    rows = user_client(token).get(
        "team_members",
        {"select": "org_id", "user_id": f"eq.{user_id}", "limit": "1"},
    )
    return rows[0]["org_id"] if rows else None
