from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config. Reads env vars (and .env.local / .env in local dev). No
    Supabase: the platform runs on self-hosted Postgres + local/object storage."""

    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"), extra="ignore", case_sensitive=False
    )

    # Postgres — two roles, mirroring the isolation split:
    #  database_url       -> app_user (NOBYPASSRLS): the request path, RLS enforced
    #  admin_database_url -> superuser/worker (BYPASSRLS): provisioning + workers
    database_url: str = "postgresql://app_user:app_pw@localhost:5432/klovered"
    admin_database_url: str = "postgresql://klovered:klovered_pw@localhost:5432/klovered"

    # Self-issued guest auth (replaces Supabase Auth). One shared HS256 secret;
    # only this service verifies tokens.
    auth_jwt_secret: str = "dev-secret-change-me"
    auth_token_ttl_seconds: int = 60 * 60 * 24  # 24h guest session
    # Real accounts get a longer session than throwaway guests — their data
    # isn't on the 48h purge clock, so there's no reason to log them out daily.
    auth_account_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30d

    # Local-disk storage (replaces Supabase Storage / Spaces). Swap for an S3
    # adapter later without touching callers.
    storage_dir: str = "./data/uploads"
    max_upload_bytes: int = 20 * 1024 * 1024  # 20 MB cap

    # Concurrency guard: how many uploads may process at once (protects RAM).
    max_concurrent_uploads: int = 2

    # LLM (Mistral only — generation, embeddings, OCR). Key resolves
    # llm_api_key then mistral_api_key.
    llm_api_key: str = ""
    mistral_api_key: str = ""
    llm_base_url: str = "https://api.mistral.ai/v1"
    llm_model: str = "mistral-large-latest"
    llm_model_fast: str = "mistral-small-latest"

    cron_secret: str = ""

    # Google OAuth (replaces Supabase's linkIdentity Google leg). The client id
    # is public; the secret must live only in .env, never in the repo. Google
    # rejects raw-IP redirect URIs — google_redirect_uri must be localhost (for
    # dev) or a real domain, and must be registered on the OAuth client.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    # Where the browser lands after a successful Google login; the minted token
    # is appended as a #access_token fragment for the SPA to read.
    google_post_login_redirect: str = "http://localhost:3100/"

    @property
    def llm_key(self) -> str:
        return self.llm_api_key or self.mistral_api_key

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
