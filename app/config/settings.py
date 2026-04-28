from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Azure App Registration ─────────────────────────────────────────────
    AZURE_CLIENT_ID: str
    AZURE_CLIENT_SECRET: str    # App-only Graph calls (webhooks) only
    AZURE_TENANT_ID: str        # Your own tenant — used in get_access_token_app()

    # ── Central Database ───────────────────────────────────────────────────
    CENTRAL_DB_URL: str

    # ── Tenant DB auth ─────────────────────────────────────────────────────
    TENANT_DB_USER: str

    # ── Tenant DB password ─────────────────────────────────────────────────
    TENANT_DB_PASSWORD: str  # Shared across all tenant DBs on the same server

    # ── Azure Key Vault ────────────────────────────────────────────────────
    # Secret naming convention: db-{org_name}  ← pending team confirmation
    # AZURE_KEYVAULT_URL: str  # Uncomment when switching to Azure Key Vault

    # ── Redis ──────────────────────────────────────────────────────────────
    REDIS_URL: str

    # ── Webhook — optional until webhook team integrates ───────────────────
    WEBHOOK_BASE_URL: str = ""
    WEBHOOK_CLIENT_STATE: str

    # ── Azure OpenAI ───────────────────────────────────────────────────────
    AZURE_OPENAI_ENDPOINT: str
    AZURE_OPENAI_API_KEY: str
    AZURE_OPENAI_DEPLOYMENT_EMBEDDING: str  # e.g. text-embedding-3-small
    AZURE_OPENAI_DEPLOYMENT_LLM: str        # e.g. gpt-4o

    # ── Azure Text Analytics ───────────────────────────────────────────────
    # Used by sentiment team — not Sprint 1. Optional until integrated.
    AZURE_TEXT_ANALYTICS_ENDPOINT: str = ""
    AZURE_TEXT_ANALYTICS_KEY: str = ""

    # ── Sentry ─────────────────────────────────────────────────────────────
    SENTRY_DSN: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
