from __future__ import annotations

import logging

from openai import AsyncAzureOpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config.settings import get_settings

log = logging.getLogger(__name__)

# text-embedding-3-small outputs exactly 1536 floats per input by default.
# This MUST match Vector(1536) declared in app/db/tenant/models.py → Chunk.embedding.
EMBEDDING_DIM = 1536

# Azure OpenAI enforces a maximum of 16 texts per embeddings API request.
_BATCH_SIZE = 16

# Module-level singleton — avoids creating a new HTTP client on every call.
# One instance per worker process is sufficient and keeps connection pools alive.
_client: AsyncAzureOpenAI | None = None


def _get_client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncAzureOpenAI(
            api_key=s.AZURE_OPENAI_API_KEY,
            azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
            api_version="2024-02-01",
        )
    return _client


async def embed_single(text: str) -> list[float]:
    """
    Return a single 1536-dim embedding vector for the given text.
    Convenience wrapper around embed_batch for callers that process one text at a time.
    """
    return (await embed_batch([text]))[0]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Return one 1536-dim embedding vector for each input text, in input order.

    Azure OpenAI limits each embeddings request to 16 inputs. Splits into
    sub-batches of ≤ 16, calls _embed_sub_batch for each, and concatenates.

    _embed_sub_batch retries on HTTP 429 up to 5 times with exponential back-off
    (2–60 s). Any other exception propagates immediately.

    Celery callers: use asyncio.run(embed_batch(...)) or an async Celery task.
    """
    if not texts:
        return []

    results: list[list[float]] = []
    for batch_start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[batch_start : batch_start + _BATCH_SIZE]
        vectors = await _embed_sub_batch(batch)
        results.extend(vectors)
    return results


@retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def _embed_sub_batch(texts: list[str]) -> list[list[float]]:
    """
    Call the Azure OpenAI embeddings API for a single sub-batch (≤ 16 texts).
    Not meant to be called directly — use embed_batch instead.
    """
    s = get_settings()
    client = _get_client()

    response = await client.embeddings.create(
        input=texts,
        model=s.AZURE_OPENAI_DEPLOYMENT_EMBEDDING,
    )

    ordered = sorted(response.data, key=lambda item: item.index)

    vectors: list[list[float]] = []
    for item in ordered:
        vector = item.embedding
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(
                f"Expected {EMBEDDING_DIM}-dim embedding, got {len(vector)}. "
                f"Verify AZURE_OPENAI_DEPLOYMENT_EMBEDDING is text-embedding-3-small."
            )
        vectors.append(vector)

    return vectors
