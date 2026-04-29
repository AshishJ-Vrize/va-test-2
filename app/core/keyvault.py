# Scope A — Azure Key Vault client, get_db_secret(org_name)
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 10 (tenant DB routing, credential storage)

import logging

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


# ── Custom exception ──────────────────────────────────────────────────────────

class KeyVaultError(Exception):
    """
    Raised when a Key Vault operation fails.
    Callers (db/manager.py) catch this to surface a clear 503 rather than
    letting an Azure SDK exception bubble up uncaught.
    """
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ── Key Vault client ──────────────────────────────────────────────────────────

class KeyVaultClient:
    """
    Thin wrapper around the Azure Key Vault Secrets SDK.

    Authentication uses DefaultAzureCredential, which resolves automatically:
      - Production (Azure Container Apps): managed identity — no secrets needed
      - Local dev: Azure CLI login (run 'az login' once)
    No credential config changes between environments.

    Lifecycle: instantiated once in FastAPI lifespan, stored in app.state.
    The _client underneath is thread-safe per Azure SDK documentation.

    Secret naming convention: db-{org_name}
    ⚠ If provisioning team uses a different convention, change _secret_name() only.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._log = logging.getLogger(f"{__name__}.KeyVaultClient")

        credential = DefaultAzureCredential(additionally_allowed_tenants=["*"])
        self._client = SecretClient(
            vault_url=settings.AZURE_KEYVAULT_URL,
            credential=credential,
        )
        self._log.info(
            "KeyVaultClient initialised | vault=%s", settings.AZURE_KEYVAULT_URL
        )

    def get_db_secret(self, org_name: str) -> str:
        """
        Fetches the DB password for the tenant identified by org_name.

        The secret is stored in Key Vault under the name 'db-{org_name}'.
        This method performs a live Key Vault API call every time it is called.
        Caching (so this is called once per tenant per process lifetime) is
        handled by db/manager.py — not here.

        Args:
            org_name: tenants.org_name from the central DB (e.g. 'acme').

        Returns:
            The secret value string (DB password).

        Raises:
            KeyVaultError: if the secret does not exist or the call fails.
        """
        secret_name = self._secret_name(org_name)
        self._log.info(
            "KeyVault: fetching DB secret | org=%s | secret_name=%s",
            org_name, secret_name,
        )

        try:
            secret = self._client.get_secret(secret_name)
        except ResourceNotFoundError as exc:
            self._log.error(
                "KeyVault: secret not found | org=%s | secret_name=%s | error=%s",
                org_name, secret_name, exc,
            )
            raise KeyVaultError(
                f"DB secret '{secret_name}' not found in Key Vault. "
                f"Tenant '{org_name}' may not be fully provisioned yet. "
                "Contact the platform administrator."
            ) from exc
        except HttpResponseError as exc:
            self._log.error(
                "KeyVault: HTTP error | org=%s | secret_name=%s | status=%s | error=%s",
                org_name, secret_name, exc.status_code, exc,
            )
            raise KeyVaultError(
                f"Key Vault returned HTTP {exc.status_code} when fetching "
                f"secret '{secret_name}' for tenant '{org_name}': {exc.message}"
            ) from exc
        except Exception as exc:
            self._log.error(
                "KeyVault: unexpected error | org=%s | secret_name=%s | error=%s",
                org_name, secret_name, exc,
            )
            raise KeyVaultError(
                f"Unexpected error fetching DB secret for tenant '{org_name}': {exc}"
            ) from exc

        if not secret.value:
            self._log.error(
                "KeyVault: secret exists but value is empty | org=%s | secret_name=%s",
                org_name, secret_name,
            )
            raise KeyVaultError(
                f"Secret '{secret_name}' exists in Key Vault but its value is empty. "
                f"Re-provision the secret for tenant '{org_name}'."
            )

        self._log.info(
            "KeyVault: secret fetched successfully | org=%s | secret_name=%s",
            org_name, secret_name,
        )
        return secret.value

    @staticmethod
    def _secret_name(org_name: str) -> str:
        # Naming convention: db-{org_name}
        # ⚠ Pending provisioning team confirmation (CONTEXT.md Section 17, Q2).
        # If the convention changes, update only this method.
        return f"db-{org_name}"
