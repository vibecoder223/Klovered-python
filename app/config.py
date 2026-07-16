from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Read from the same .env.local the Next.js app uses in local dev; env vars
    # win over the file. Extra keys (the app's many NEXT_PUBLIC_* vars) are
    # ignored so this doesn't error on the shared env file.
    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"), extra="ignore", case_sensitive=False
    )

    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_aud: str = "authenticated"
    llm_api_key: str = ""
    mistral_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_model_fast: str = ""
    cron_secret: str = ""

    # NOTE: the app's env uses NEXT_PUBLIC_SUPABASE_URL /
    # NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY. Those names are mapped onto
    # supabase_url / supabase_anon_key in get_settings() below.

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"

    @property
    def postgrest_url(self) -> str:
        return f"{self.supabase_url}/rest/v1"

    @property
    def storage_url(self) -> str:
        return f"{self.supabase_url}/storage/v1"

    @property
    def llm_key(self) -> str:
        return self.llm_api_key or self.mistral_api_key


def _alias_from_env_files(name: str) -> str:
    # Read a NEXT_PUBLIC_* alias from the same dotenv files pydantic uses, since
    # those names don't map onto our snake_case fields. os.environ wins.
    import os

    if os.getenv(name):
        return os.environ[name]
    for path in (".env.local", ".env"):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


@lru_cache
def get_settings() -> Settings:
    # Honor the NEXT_PUBLIC_* aliases without a custom settings source: read them
    # explicitly (env var or dotenv file) and pass as overrides when present.
    overrides = {}
    url = _alias_from_env_files("NEXT_PUBLIC_SUPABASE_URL")
    anon = _alias_from_env_files("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
    if url:
        overrides["supabase_url"] = url
    if anon:
        overrides["supabase_anon_key"] = anon
    return Settings(**overrides)
