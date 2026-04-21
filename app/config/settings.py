from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Azure App Registration ─────────────────────────────────────────────
    # Single multi-tenant registration used for all tenants.
    AZURE_CLIENT_ID: str
    AZURE_CLIENT_SECRET: str    # App-only Graph calls (webhooks) only
    AZURE_TENANT_ID: str        # Your own tenant — used in get_access_token_app()

    # ── Central Database ───────────────────────────────────────────────────
    # The one shared DB that stores tenant registry, billing, pricing.
    CENTRAL_DB_URL: str

    # ── Tenant DB auth ─────────────────────────────────────────────────────
    # Username is shared across all tenant DBs.
    # Password is fetched per-tenant from Key Vault at runtime.
    TENANT_DB_USER: str

    # ── Azure Key Vault ────────────────────────────────────────────────────
    # Where per-tenant DB passwords are stored.
    # Secret naming convention: db-{org_name}  ← pending team confirmation
    AZURE_KEYVAULT_URL: str

    # ── Redis ──────────────────────────────────────────────────────────────
    # Shared by: JWKS cache, Celery broker, MSAL token cache.
    REDIS_URL: str

    # ── Webhook ────────────────────────────────────────────────────────────
    WEBHOOK_BASE_URL: str       # Public HTTPS URL this backend is reachable at
    WEBHOOK_CLIENT_STATE: str   # Validated against incoming Graph notifications

    # ── Azure OpenAI ───────────────────────────────────────────────────────
    AZURE_OPENAI_ENDPOINT: str
    AZURE_OPENAI_API_KEY: str
    AZURE_OPENAI_DEPLOYMENT_EMBEDDING: str  # e.g. text-embedding-3-small
    AZURE_OPENAI_DEPLOYMENT_LLM: str        # e.g. gpt-4o

    # ── Azure Text Analytics ───────────────────────────────────────────────
    AZURE_TEXT_ANALYTICS_ENDPOINT: str
    AZURE_TEXT_ANALYTICS_KEY: str

    # ── Sentry ─────────────────────────────────────────────────────────────
    # Optional. Set to empty string to disable.
    SENTRY_DSN: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,    # ENV_VAR names must match exactly
        extra="ignore",         # Ignore unknown vars in .env — don't crash
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
