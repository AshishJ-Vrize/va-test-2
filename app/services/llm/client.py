"""AzureOpenAIClient — thin async wrapper over AsyncAzureOpenAI.

Exposes two methods used across the chat layer:
  - complete_text  → plain string answer (for SEARCH/HYBRID/COMPARE/META/STRUCTURED_LLM)
  - complete_json  → parsed dict (for ROUTER and any structured-output call)

Singleton via lru_cache; safe under asyncio because AsyncAzureOpenAI is
designed to be shared. Embeddings live in `app/services/ingestion/embedder.py`
to avoid duplicating that code path — query-time embedding for the chat
layer reuses the same module.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from openai import AsyncAzureOpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config.settings import get_settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm_client() -> "AzureOpenAIClient":
    """Process-level cached client. Use this in handlers — never construct directly."""
    return AzureOpenAIClient()


class AzureOpenAIClient:
    """Thin async wrapper over AsyncAzureOpenAI.

    The class implements the LLMClient Protocol defined in
    `app/services/chat/interfaces.py`. Tests substitute a fake at injection
    points; production wiring uses `get_llm_client()`.
    """

    def __init__(self) -> None:
        s = get_settings()
        self._raw = AsyncAzureOpenAI(
            api_key=s.AZURE_OPENAI_API_KEY,
            azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
            api_version="2024-02-01",
        )

    async def complete_text(
        self,
        deployment: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 600,
        temperature: float = 0.3,
    ) -> str:
        """Plain-text completion. Returns trimmed content (or empty string)."""
        resp = await self._call_with_backoff(
            model=deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()

    async def complete_json(
        self,
        deployment: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 400,
        temperature: float = 0.0,
    ) -> dict:
        """JSON-mode completion. Returns the parsed dict, or {} on parse failure."""
        resp = await self._call_with_backoff(
            model=deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("AzureOpenAIClient.complete_json: invalid JSON | error=%s | raw=%r",
                        exc, raw[:200])
            return {}

    @retry(
        retry=retry_if_exception_type(RateLimitError),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _call_with_backoff(self, **kwargs):
        return await self._raw.chat.completions.create(**kwargs)
