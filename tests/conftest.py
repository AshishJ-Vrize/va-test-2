"""
Shared pytest configuration for all teams.

Sets dummy environment variables so pydantic-settings doesn't raise
ValidationError during unit tests. No real Azure/DB connections are made —
all external calls are mocked in individual test files.
"""

import os
import pytest


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """
    Inject dummy values for every required env var before each test.
    Prevents pydantic_settings from raising ValidationError when
    get_settings() is called inside imported modules during tests.
    """
    env = {
        "AZURE_CLIENT_ID": "test-client-id",
        "AZURE_CLIENT_SECRET": "test-client-secret",
        "AZURE_TENANT_ID": "test-tenant-id",
        "CENTRAL_DB_URL": "postgresql+psycopg2://user:pass@localhost/central?sslmode=require",
        "TENANT_DB_USER": "tenant_user",
        "AZURE_KEYVAULT_URL": "https://test-vault.vault.azure.net",
        "REDIS_URL": "rediss://:pass@localhost:6380/0",
        "WEBHOOK_BASE_URL": "https://test.example.com",
        "WEBHOOK_CLIENT_STATE": "test-webhook-secret",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "test-openai-key",
        "AZURE_OPENAI_DEPLOYMENT_EMBEDDING": "text-embedding-3-small",
        "AZURE_OPENAI_DEPLOYMENT_LLM": "gpt-4o",
        "AZURE_TEXT_ANALYTICS_ENDPOINT": "https://test.cognitiveservices.azure.com",
        "AZURE_TEXT_ANALYTICS_KEY": "test-text-analytics-key",
        "SENTRY_DSN": "",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    # Clear the lru_cache so get_settings() picks up the monkeypatched env vars
    from app.config.settings import get_settings
    get_settings.cache_clear()

    yield

    # Clear again after the test so cached values don't leak between tests
    get_settings.cache_clear()
