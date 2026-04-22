from __future__ import annotations

import logging

from openai import AzureOpenAI, RateLimitError
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
_client: AzureOpenAI | None = None


def _get_client() -> AzureOpenAI:
    """
    Return the shared AzureOpenAI client, creating it on first call.

    Uses a module-level singleton so the underlying HTTP connection pool
    is reused across all embed_batch calls within the same worker process.
    """
    global _client
    if _client is None:
        s = get_settings()
        _client = AzureOpenAI(
            api_key=s.AZURE_OPENAI_API_KEY,
            azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
            api_version="2024-02-01",
        )
    return _client


def embed_single(text: str) -> list[float]:
    """
    Return a single 1536-dim embedding vector for the given text.

    Convenience wrapper around embed_batch for callers that process one
    text at a time (e.g. query embedding at search time).
    """
    return embed_batch([text])[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Return one 1536-dim embedding vector for each input text, in input order.

    How it works
    ------------
    Azure OpenAI limits each embeddings request to 16 inputs.  This function
    splits the full list into sub-batches of ≤ 16, calls _embed_sub_batch for
    each, and concatenates the results before returning.

    Retry behaviour
    ---------------
    _embed_sub_batch is decorated with @retry and will automatically retry on
    HTTP 429 (RateLimitError) up to 5 times with exponential back-off (2–60 s).
    Any other exception (e.g. 400 Bad Request, network error) is raised immediately.

    Parameters
    ----------
    texts : List of non-empty strings to embed.  Empty list returns [].

    Returns
    -------
    List of float lists, same length as `texts`, same order.
    Each inner list contains exactly EMBEDDING_DIM (1536) floats.
    """
    if not texts:
        return []

    results: list[list[float]] = []
    for batch_start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[batch_start : batch_start + _BATCH_SIZE]
        vectors = _embed_sub_batch(batch)
        results.extend(vectors)
    return results


@retry(
    # Only retry on rate-limit errors (HTTP 429).  Other errors are bugs or
    # config issues that won't resolve on retry, so let them propagate immediately.
    retry=retry_if_exception_type(RateLimitError),
    # Exponential back-off starting at 2 s, doubling each attempt, capped at 60 s.
    wait=wait_exponential(multiplier=1, min=2, max=60),
    # Give up after 5 attempts (~2 min total wait at max back-off).
    stop=stop_after_attempt(5),
    # Re-raise the original RateLimitError if all retries are exhausted.
    reraise=True,
)
def _embed_sub_batch(texts: list[str]) -> list[list[float]]:
    """
    Call the Azure OpenAI embeddings API for a single sub-batch (≤ 16 texts).

    Validates that every returned vector has exactly EMBEDDING_DIM dimensions.
    Raises ValueError immediately (no retry) if the dimension check fails —
    this indicates a misconfigured deployment name, not a transient error.

    This function is not meant to be called directly; use embed_batch instead.
    """
    s = get_settings()
    client = _get_client()

    response = client.embeddings.create(
        input=texts,
        model=s.AZURE_OPENAI_DEPLOYMENT_EMBEDDING,
    )

    # Azure guarantees response items are in input order, but sort by index
    # defensively in case the API behaviour changes across versions.
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
