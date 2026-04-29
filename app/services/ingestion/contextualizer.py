"""
Contextual embedding enrichment for RAG retrieval quality.

Based on Anthropic's Contextual Retrieval research: prepending a short
meeting-context description to each chunk before embedding reduces retrieval
failures by 35-49% compared to embedding raw chunk text.

Two layers:
1. build_contextual_text()   — free, no LLM.  Prepends meeting metadata
   (subject, date, speakers, speaker label) to the chunk text.
2. contextualize_chunks()    — one batched LLM call per meeting.  Generates
   a per-chunk sentence describing the current discussion topic, then
   combines it with the free-layer context.  Costs ~$0.015/meeting using
   the mini model (gpt-4o-mini) or ~$0.10/meeting with gpt-4o.

The returned strings are passed to embed_batch() and stored as
chunks.contextual_text.  The raw chunks.text column is never modified.
"""
from __future__ import annotations

import json
import logging

from openai import AsyncAzureOpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config.settings import get_settings
from app.services.ingestion.chunker import Chunk

log = logging.getLogger(__name__)

_client: AsyncAzureOpenAI | None = None

_CONTEXT_SYSTEM_PROMPT = """\
You are given a meeting transcript and a list of numbered text chunks from it.
For each chunk output ONE sentence (max 30 words) describing what part of the
conversation it covers — include the current discussion topic and any key
entities (names, products, decisions).

Output strict JSON: {"contexts": ["<sentence for chunk 1>", "<sentence for chunk 2>", ...]}
One entry per chunk, in the same order as the input.
Do not add any explanation or extra keys."""


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


def _llm_deployment() -> str:
    """Return the mini model if configured, otherwise fall back to the main LLM."""
    s = get_settings()
    return s.AZURE_OPENAI_DEPLOYMENT_LLM_MINI or s.AZURE_OPENAI_DEPLOYMENT_LLM


def build_contextual_text(
    meeting_subject: str,
    meeting_date: str,
    speakers: list[str],
    chunk: Chunk,
) -> str:
    """
    Build an enriched text string for a single chunk without any LLM call.

    Prepends meeting metadata so the embedding captures who, when, and what
    was being discussed — not just the isolated utterance.

    Example output:
      "Meeting: Q4 Planning on 2026-01-15 with Priyanka, Raj, Sara.
       Speaker Raj Patel: I think we should accept Acme's terms..."
    """
    speaker_list = ", ".join(speakers[:5]) if speakers else "unknown participants"
    header = (
        f"Meeting: {meeting_subject or 'Untitled'} on {meeting_date}. "
        f"Participants: {speaker_list}."
    )
    return f"{header} Speaker {chunk.speaker}: {chunk.text}"


async def contextualize_chunks(
    meeting_subject: str,
    meeting_date: str,
    speakers: list[str],
    chunks: list[Chunk],
) -> list[str]:
    """
    Generate contextual embedding text for every chunk in one meeting.

    For each chunk this produces:
      "<LLM-generated topic sentence>. Meeting: <subject> on <date>.
       Participants: <names>. Speaker <name>: <raw text>"

    The LLM call is a single batched request (all chunks in one prompt) to
    minimise latency and cost.  Falls back to build_contextual_text() per
    chunk if the LLM call fails after retries.

    Returns a list of strings in the same order as `chunks`.
    """
    if not chunks:
        return []

    # Always build the free-layer context first — used as fallback and as the
    # base that the LLM-generated topic sentence is prepended to.
    free_layer = [
        build_contextual_text(meeting_subject, meeting_date, speakers, c)
        for c in chunks
    ]

    try:
        topic_sentences = await _batch_topic_sentences(
            meeting_subject=meeting_subject,
            chunks=chunks,
        )
    except Exception:
        log.warning(
            "contextualize_chunks: LLM call failed — falling back to free-layer context",
            exc_info=True,
        )
        return free_layer

    if len(topic_sentences) != len(chunks):
        log.warning(
            "contextualize_chunks: LLM returned %d sentences for %d chunks — "
            "falling back to free-layer context",
            len(topic_sentences),
            len(chunks),
        )
        return free_layer

    return [
        f"{topic}. {free}"
        for topic, free in zip(topic_sentences, free_layer)
    ]


@retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def _batch_topic_sentences(
    meeting_subject: str,
    chunks: list[Chunk],
) -> list[str]:
    """
    One LLM call that generates a single topic-context sentence per chunk.
    Retries up to 3× on 429 rate-limit errors.
    """
    s = get_settings()
    client = _get_client()

    # Build a compact transcript preview (first 120 words) so the LLM
    # understands the meeting context without consuming too many tokens.
    transcript_preview = " ".join(
        f"{c.speaker}: {c.text}" for c in chunks
    )[:2000]

    chunk_list = "\n".join(
        f"{i + 1}. [{c.speaker}] {c.text[:300]}"
        for i, c in enumerate(chunks)
    )

    user_message = (
        f"Meeting subject: {meeting_subject or 'Untitled'}\n\n"
        f"Transcript excerpt:\n{transcript_preview}\n\n"
        f"Chunks to contextualise:\n{chunk_list}"
    )

    response = await client.chat.completions.create(
        model=_llm_deployment(),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _CONTEXT_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
        max_tokens=len(chunks) * 60,
    )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    contexts: list[str] = data.get("contexts", [])

    if not isinstance(contexts, list):
        raise ValueError(f"LLM returned unexpected shape: {raw[:200]}")

    return [str(c).strip() for c in contexts]
