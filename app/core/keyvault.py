import logging

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


class KeyVaultError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class KeyVaultClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._log = logging.getLogger(f"{__name__}.KeyVaultClient")
        credential = DefaultAzureCredential(additionally_allowed_tenants=["*"])
        self._client = SecretClient(
            vault_url=settings.AZURE_KEYVAULT_URL,
            credential=credential,
        )
        self._log.info("KeyVaultClient initialised | vault=%s", settings.AZURE_KEYVAULT_URL)

    def get_db_secret(self, org_name: str) -> str:
        secret_name = self._secret_name(org_name)
        self._log.info("KeyVault: fetching DB secret | org=%s | secret_name=%s", org_name, secret_name)
        try:
            secret = self._client.get_secret(secret_name)
        except ResourceNotFoundError as exc:
            self._log.error("KeyVault: secret not found | org=%s | secret_name=%s | error=%s", org_name, secret_name, exc)
            raise KeyVaultError(f"DB secret '{secret_name}' not found in Key Vault.") from exc
        except HttpResponseError as exc:
            self._log.error("KeyVault: HTTP error | org=%s | status=%s | error=%s", org_name, exc.status_code, exc)
            raise KeyVaultError(f"Key Vault returned HTTP {exc.status_code} for tenant '{org_name}'.") from exc
        except Exception as exc:
            self._log.error("KeyVault: unexpected error | org=%s | error=%s", org_name, exc)
            raise KeyVaultError(f"Unexpected error fetching DB secret for tenant '{org_name}': {exc}") from exc

        if not secret.value:
            raise KeyVaultError(f"Secret '{secret_name}' exists but value is empty.")

        self._log.info("KeyVault: secret fetched successfully | org=%s", org_name)
        return secret.value

    @staticmethod
    def _secret_name(org_name: str) -> str:
        return "tenant-db-user-password"
