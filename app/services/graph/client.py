# Scope A — GraphClient (HTTP wrapper), TokenExpiredError, get_access_token_app()
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 13 (client.py exports)

import asyncio
import logging
import random
from typing import Any

import httpx
import msal

from app.config.settings import get_settings
from app.services.graph.exceptions import GraphClientError, TokenExpiredError
from app.services.graph.meetings import MeetingsMixin
from app.services.graph.transcripts import TranscriptsMixin

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"  # verified — CONTEXT.md Section 16
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

MAX_RETRIES = 3  # applies to: network errors, timeouts, 429, 5xx

# Re-export exceptions so callers can import from either location:
#   from app.services.graph.client import GraphClientError
#   from app.services.graph.exceptions import GraphClientError
__all__ = ["GraphClient", "GraphClientError", "TokenExpiredError", "get_access_token_app"]


# ── Graph HTTP client ─────────────────────────────────────────────────────────

class GraphClient(MeetingsMixin, TranscriptsMixin):
    """
    Async HTTP wrapper around the Microsoft Graph REST API with retry and error handling.

    Retry policy (handled inside _request):
      - Network error / timeout : retry up to MAX_RETRIES with exponential backoff + jitter
      - 429 Too Many Requests   : retry up to MAX_RETRIES respecting Retry-After header
      - 5xx Server Error        : retry up to MAX_RETRIES with exponential backoff + jitter
      - 401 Unauthorized        : raise TokenExpiredError immediately — no retry
      - 400 Bad Request         : raise immediately, log full request+response — this is our bug
      - 403 Forbidden           : raise immediately with graph error code + likely cause hint
      - 404 Not Found           : raise immediately with graph error code + likely cause hint

    Extended outage behaviour:
      After MAX_RETRIES are exhausted, GraphClientError is raised and propagates to:
        - Route handlers  → return HTTP 503 to frontend
        - Celery tasks    → Celery reschedules with long delay (5–60 min, up to 10x)

    ⚠ Do NOT use /me/ paths with an app-only token — Graph returns 400.
      Pass user_id to _base_path() when using app tokens. CONTEXT.md Section 18.
    """

    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._log = logging.getLogger(f"{__name__}.GraphClient")
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ── Public HTTP methods ───────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """
        GET {GRAPH_BASE}{path}

        Args:
            path:    URL path relative to GRAPH_BASE (e.g. '/me/onlineMeetings').
            params:  OData query parameters (e.g. {'$top': 20, '$filter': '...'}).
            timeout: Request timeout in seconds. Default 30s.
        """
        return await self._request("GET", path, params=params, timeout=timeout)

    async def post(self, path: str, body: dict, timeout: float = 30.0) -> dict:
        """POST {GRAPH_BASE}{path} with JSON body."""
        return await self._request("POST", path, json=body, timeout=timeout)

    async def patch(self, path: str, body: dict, timeout: float = 30.0) -> dict:
        """PATCH {GRAPH_BASE}{path} with JSON body."""
        return await self._request("PATCH", path, json=body, timeout=timeout)

    async def delete(self, path: str, timeout: float = 30.0) -> None:
        """DELETE {GRAPH_BASE}{path}. Returns None on success (Graph sends 204)."""
        await self._request("DELETE", path, timeout=timeout, expect_json=False)

    async def get_text(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> str:
        """
        GET {GRAPH_BASE}{path} returning the raw response body as a string.

        Used for non-JSON endpoints:
          GET .../transcripts/{id}/content?$format=text/vtt → raw VTT string

        VTT → structured conversion is NOT done here.
        That belongs to services/ingestion/vtt_parser.py.
        """
        return await self._request("GET", path, params=params, timeout=timeout, return_text=True)

    # ── Path helper ───────────────────────────────────────────────────────────

    def _base_path(self, user_id: str | None = None) -> str:
        """
        Returns the correct base path for online meeting endpoints.

        user_id=None → /me/onlineMeetings           (delegated token)
        user_id=str  → /users/{id}/onlineMeetings   (app token)

        ⚠ Never call /me/ with an app token — Graph returns 400.
        """
        if user_id:
            return f"/users/{user_id}/onlineMeetings"
        return "/me/onlineMeetings"

    # ── Internal request handler ──────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        timeout: float = 30.0,
        expect_json: bool = True,
        return_text: bool = False,
        **kwargs: Any,
    ) -> dict | str | None:
        """
        Central async HTTP request handler. Implements all retry and error handling.

        Retry targets: network errors, timeouts, 429, 5xx — up to MAX_RETRIES.
        No-retry targets: 401, 400, 403, 404 — fail immediately with full context.

        Raises:
            TokenExpiredError:  401 — delegated token expired, frontend must re-auth.
            GraphClientError:   All other failures including exhausted retries.
        """
        url = f"{GRAPH_BASE}{path}"
        attempt = 0

        async with httpx.AsyncClient() as client:
            while attempt < MAX_RETRIES:
                attempt += 1
                self._log.debug(
                    "Graph request | method=%s | url=%s | attempt=%d/%d",
                    method, url, attempt, MAX_RETRIES,
                )

                # ── Send the request ──────────────────────────────────────────────
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers,
                        timeout=timeout,
                        **kwargs,
                    )
                except httpx.TimeoutException as exc:
                    wait = self._backoff_wait(attempt)
                    self._log.warning(
                        "Graph timeout | method=%s | url=%s | attempt=%d/%d | "
                        "timeout=%ss | retrying_in=%.1fs",
                        method, url, attempt, MAX_RETRIES, timeout, wait,
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(wait)
                        continue
                    self._log.error(
                        "Graph timeout — retries exhausted | method=%s | url=%s", method, url
                    )
                    raise GraphClientError(
                        f"Graph API timed out after {timeout}s on {method} {path}. "
                        f"Tried {MAX_RETRIES} times. "
                        "If this persists, Microsoft Graph may be experiencing an outage.",
                        status_code=None,
                    ) from exc

                except httpx.RequestError as exc:
                    wait = self._backoff_wait(attempt)
                    self._log.warning(
                        "Graph network error | method=%s | url=%s | attempt=%d/%d | "
                        "error=%s | retrying_in=%.1fs",
                        method, url, attempt, MAX_RETRIES, exc, wait,
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(wait)
                        continue
                    self._log.error(
                        "Graph network error — retries exhausted | method=%s | url=%s | error=%s",
                        method, url, exc,
                    )
                    raise GraphClientError(
                        f"Network error reaching Graph API on {method} {path}: {exc}. "
                        f"Tried {MAX_RETRIES} times. Check connectivity.",
                        status_code=None,
                    ) from exc

                # ── Handle response status codes ──────────────────────────────────

                # 429 — rate limit: respect Retry-After, then retry
                if response.status_code == 429:
                    retry_after = self._parse_retry_after(response, fallback=self._backoff_wait(attempt))
                    self._log.warning(
                        "Graph rate limit (429) | method=%s | url=%s | attempt=%d/%d | "
                        "retry_after=%.1fs",
                        method, url, attempt, MAX_RETRIES, retry_after,
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(retry_after)
                        continue
                    self._log.error(
                        "Graph rate limit — retries exhausted | method=%s | url=%s", method, url
                    )
                    raise GraphClientError(
                        f"Graph API rate limit hit on {method} {path}. "
                        f"Tried {MAX_RETRIES} times. Reduce request frequency.",
                        status_code=429,
                    )

                # 5xx — server error: retry with backoff
                if response.status_code >= 500:
                    error_body = self._safe_json(response)
                    graph_code = error_body.get("error", {}).get("code", "unknown") if error_body else "unknown"
                    graph_message = error_body.get("error", {}).get("message", response.text) if error_body else response.text
                    wait = self._backoff_wait(attempt)
                    self._log.warning(
                        "Graph server error (5xx) | method=%s | url=%s | status=%s | "
                        "graph_code=%s | attempt=%d/%d | retrying_in=%.1fs",
                        method, url, response.status_code, graph_code, attempt, MAX_RETRIES, wait,
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(wait)
                        continue
                    self._log.error(
                        "Graph server error — retries exhausted | method=%s | url=%s | "
                        "status=%s | graph_code=%s | graph_message=%s",
                        method, url, response.status_code, graph_code, graph_message,
                    )
                    raise GraphClientError(
                        f"Graph API server error {response.status_code} on {method} {path}. "
                        f"graph_code={graph_code!r} | graph_message={graph_message!r}. "
                        f"Tried {MAX_RETRIES} times. Microsoft Graph may be experiencing an outage.",
                        status_code=response.status_code,
                    )

                # 401 — token expired: no retry, frontend must re-authenticate
                if response.status_code == 401:
                    self._log.warning(
                        "Graph 401 — token expired | method=%s | url=%s", method, url
                    )
                    raise TokenExpiredError(
                        f"Graph API returned 401 on {method} {path}. "
                        "The delegated access token has expired. "
                        "The frontend must re-authenticate via MSAL and retry."
                    )

                # 400 — bad request: this is our bug, log everything for debugging
                if response.status_code == 400:
                    error_body = self._safe_json(response)
                    graph_code = error_body.get("error", {}).get("code", "unknown") if error_body else "unknown"
                    graph_message = error_body.get("error", {}).get("message", response.text) if error_body else response.text
                    request_params = kwargs.get("params")
                    request_body = kwargs.get("json")
                    self._log.error(
                        "Graph 400 Bad Request — THIS IS A CODE BUG | "
                        "method=%s | url=%s | params=%s | body=%s | "
                        "graph_code=%s | graph_message=%s",
                        method, url, request_params, request_body,
                        graph_code, graph_message,
                    )
                    raise GraphClientError(
                        f"Graph API rejected our request (400) on {method} {path}. "
                        f"graph_code={graph_code!r} | graph_message={graph_message!r} | "
                        f"params={request_params!r} | body={request_body!r}. "
                        "This is a code bug — check the request parameters.",
                        status_code=400,
                    )

                # 403 / 404 — no retry, surface graph_code and likely cause
                if response.status_code in (403, 404):
                    error_body = self._safe_json(response)
                    graph_code = error_body.get("error", {}).get("code", "unknown") if error_body else "unknown"
                    graph_message = error_body.get("error", {}).get("message", response.text) if error_body else response.text
                    likely_cause = self._likely_cause(url, response.status_code, graph_code)
                    self._log.error(
                        "Graph %s | method=%s | url=%s | graph_code=%s | "
                        "graph_message=%s | likely_cause=%s",
                        response.status_code, method, url, graph_code,
                        graph_message, likely_cause,
                    )
                    raise GraphClientError(
                        f"Graph API {response.status_code} on {method} {path}. "
                        f"graph_code={graph_code!r} | graph_message={graph_message!r} | "
                        f"likely_cause={likely_cause!r}",
                        status_code=response.status_code,
                    )

                # Other non-2xx (e.g. 405, 409, 410) — no retry
                if not response.is_success:
                    error_body = self._safe_json(response)
                    graph_code = error_body.get("error", {}).get("code", "unknown") if error_body else "unknown"
                    graph_message = error_body.get("error", {}).get("message", response.text) if error_body else response.text
                    self._log.error(
                        "Graph unexpected error | method=%s | url=%s | status=%s | "
                        "graph_code=%s | graph_message=%s",
                        method, url, response.status_code, graph_code, graph_message,
                    )
                    raise GraphClientError(
                        f"Graph API error {response.status_code} on {method} {path}. "
                        f"graph_code={graph_code!r} | graph_message={graph_message!r}",
                        status_code=response.status_code,
                    )

                # ── Success ───────────────────────────────────────────────────────
                self._log.debug(
                    "Graph request success | method=%s | url=%s | status=%s",
                    method, url, response.status_code,
                )

                if return_text:
                    return response.text
                if not expect_json:
                    return None
                return response.json()

        # Should never reach here — loop always returns or raises
        raise GraphClientError(  # pragma: no cover
            f"Unexpected exit from retry loop on {method} {path}.",
            status_code=None,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _backoff_wait(attempt: int) -> float:
        """
        Exponential backoff with jitter: 2^(attempt-1) + random(0, 1).
        attempt=1 → ~1s, attempt=2 → ~2s, attempt=3 → ~4s.
        Jitter prevents thundering herd when many tenants retry simultaneously.
        """
        return (2 ** (attempt - 1)) + random.uniform(0, 1)

    @staticmethod
    def _parse_retry_after(response: httpx.Response, fallback: float) -> float:
        """
        Reads the Retry-After header from a 429 response.
        Returns the header value (seconds) if present and valid, else fallback.
        """
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _likely_cause(url: str, status_code: int, graph_code: str) -> str:
        """
        Returns an internal hint about the most probable cause of a 403 or 404.
        This is logged for the engineering team — never shown to end users.
        """
        if status_code == 403:
            if graph_code == "Authorization_RequestDenied":
                if "transcripts" in url:
                    return (
                        "Missing OnlineMeetingTranscript.Read.All permission, OR "
                        "transcription policy not enabled in the customer's M365 tenant admin."
                    )
                if "recordings" in url:
                    return "Missing OnlineMeetingRecording.Read.All permission in app registration."
                if "onlineMeetings" in url:
                    return "Missing OnlineMeetings.Read or OnlineMeetings.Read.All permission."
                return "Missing a required Graph permission — check app registration scopes."
            if graph_code in ("AccessDenied", "Forbidden"):
                return (
                    "Admin consent has likely not been granted in this customer's Azure tenant. "
                    "Customer IT admin must grant consent at: "
                    "https://entra.microsoft.com → Enterprise Apps → [AppName] → Permissions."
                )
            return (
                f"Graph denied access (code={graph_code!r}). "
                "Check app registration permissions and customer admin consent status."
            )

        if status_code == 404:
            if "transcripts" in url and "content" in url:
                return (
                    "Transcript content not available. The transcript may still be processing — "
                    "Teams typically takes 5–10 minutes after a meeting ends. Retry later."
                )
            if "transcripts" in url:
                return (
                    "Transcript not found. Meeting may not have transcription enabled, "
                    "or the transcript ID is incorrect."
                )
            if "recordings" in url:
                return (
                    "No recording found for this meeting. "
                    "The meeting may not have been recorded, or the recording was deleted."
                )
            if "onlineMeetings" in url:
                return (
                    "Meeting not found. It may have been deleted from Graph, "
                    "the meeting ID may be incorrect, or the organiser's account was removed."
                )
            if "/users/" in url:
                return (
                    "User not found in this tenant's Entra directory. "
                    "The account may have been deleted or the ID/UPN is incorrect."
                )
            return "Resource not found. Verify the ID and that the resource has not been deleted."

        return "No additional context available."

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict | None:
        try:
            return response.json()
        except Exception:
            return None


# ── App-only token (client credentials) ──────────────────────────────────────
# One MSAL ConfidentialClientApplication is cached per customer tenant.
# MSAL caches the access token internally — tokens are valid ~1 hour.
# Reusing the same app instance means MSAL returns the cached token without
# hitting AAD on every call. Creating a new instance per call throws away the cache.
_msal_app_cache: dict[str, msal.ConfidentialClientApplication] = {}


def get_access_token_app(ms_tenant_id: str) -> str:
    """
    Obtains an app-only access token for the given customer tenant using the
    client credentials flow. MSAL is synchronous — wrap with asyncio.to_thread()
    when calling from async code.

    401 handling for app tokens: MSAL handles token refresh internally via
    acquire_token_for_client(). If Graph returns 401 with an app token, it
    almost always indicates missing admin consent, not token expiry. The
    webhook service caller is responsible for surfacing this to the platform admin.

    Args:
        ms_tenant_id: The CUSTOMER's Azure tenant ID (tenants.ms_tenant_id).
                      NOT settings.AZURE_TENANT_ID (the platform's own tenant).
                      Each customer needs a different authority URL.

    Returns:
        Access token string scoped to https://graph.microsoft.com/.default

    Raises:
        GraphClientError: MSAL failed to acquire a token. Likely causes:
                          missing admin consent, bad client secret, or network issue.
    """
    settings = get_settings()

    if ms_tenant_id not in _msal_app_cache:
        authority = f"https://login.microsoftonline.com/{ms_tenant_id}"
        logger.info(
            "MSAL: creating new ConfidentialClientApplication | "
            "tenant=%s | authority=%s", ms_tenant_id, authority,
        )
        _msal_app_cache[ms_tenant_id] = msal.ConfidentialClientApplication(
            client_id=settings.AZURE_CLIENT_ID,
            client_credential=settings.AZURE_CLIENT_SECRET,
            authority=authority,
        )

    app = _msal_app_cache[ms_tenant_id]
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        error = result.get("error", "unknown_error")
        description = result.get("error_description", "No description provided.")
        logger.error(
            "MSAL: token acquisition failed | tenant=%s | error=%s | description=%s",
            ms_tenant_id, error, description,
        )
        raise GraphClientError(
            f"Failed to acquire app-only token for tenant '{ms_tenant_id}'. "
            f"MSAL error={error!r} | description={description!r}. "
            "Verify: (1) admin consent for CallRecords.Read.All is granted in this "
            "customer's tenant, (2) AZURE_CLIENT_SECRET is valid and not expired.",
        )

    logger.info("MSAL: token acquired | tenant=%s", ms_tenant_id)
    return result["access_token"]
