"""Answer-composition helpers shared by every handler.

Two responsibilities:
  1. Build context blocks the LLM consumes (meeting-grouped layout).
  2. Wrap the LLM call (`compose_with_llm`) so handlers don't repeat the
     "system + user with context + question" boilerplate.

Per the prompts (SEARCH / HYBRID / STRUCTURED_LLM / META / COMPARE), the
expected layout is:

    Meeting: <title>  |  Date: <YYYY-MM-DD HH:MM>  |  ID: <meeting_id>
      <body specific to source_type>

For COMPARE the layout differs slightly (=== Meeting <title> · <date> ===)
and is built by a dedicated helper below.

Pure formatting code lives here so handlers stay focused on retrieval +
orchestration; the LLM call wrapper is here so retry / backoff / logging
have one home.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from app.services.chat.config import HISTORY_MAX_CHARS
from app.services.chat.interfaces import (
    InsightsBundle,
    LLMClient,
    MeetingMeta,
    RetrievedChunk,
)

log = logging.getLogger(__name__)


# ── Time / date formatting ────────────────────────────────────────────────────

def ms_to_display(ms: int | None) -> str:
    """Render ms as MM:SS, or HH:MM:SS for spans crossing an hour."""
    if ms is None:
        return "00:00"
    total = max(0, int(ms)) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_meeting_date(dt: datetime | str | None) -> str:
    """Normalise meeting_date to 'YYYY-MM-DD HH:MM'.

    Accepts datetime instances or Postgres `timestamptz::text` strings; falls
    back to the raw string when parsing fails so the LLM still gets some signal.
    """
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    s = str(dt).strip()
    if not s:
        return ""
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",   "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",      "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return s


# ── Headers / per-meeting formatters ──────────────────────────────────────────

def _meeting_header(meta: MeetingMeta) -> str:
    return f"Meeting: {meta.title or '(untitled)'}  |  Date: {format_meeting_date(meta.date)}  |  ID: {meta.meeting_id}"


def format_meeting_for_meta(meta: MeetingMeta) -> str:
    """META layout — header + Participants + Duration + Status."""
    parts = [_meeting_header(meta)]
    names = [
        p.get("name") for p in meta.participants
        if p.get("name") and p.get("role") != "granted"
    ]
    if names:
        parts.append(f"  Participants: {', '.join(names)}")
    elif meta.participants:
        parts.append("  Participants: (none recorded)")
    if meta.duration_minutes:
        parts.append(f"  Duration: {meta.duration_minutes} min")
    if meta.organizer_name:
        parts.append(f"  Organizer: {meta.organizer_name}")
    return "\n".join(parts)


def format_meeting_for_search(
    meta: MeetingMeta,
    chunks: list[RetrievedChunk],
) -> str:
    """SEARCH/HYBRID transcript layout — header + chunked utterances by time."""
    if not chunks:
        return ""
    lines: list[str] = [_meeting_header(meta)]
    for c in chunks:
        start = ms_to_display(c.start_ms)
        end = ms_to_display(c.end_ms)
        lines.append(f"  [Time: {start} – {end}]")
        for turn in c.chunk_text:
            n = (turn.get("n") or "Unknown").strip()
            t = (turn.get("t") or "").strip()
            if t:
                lines.append(f'    {n}: "{t}"')
    return "\n".join(lines)


def format_meeting_for_structured(
    meta: MeetingMeta,
    insights: InsightsBundle | None,
) -> str:
    """STRUCTURED_LLM / HYBRID insight layout — header + Summary/Action items/etc."""
    parts = [_meeting_header(meta)]
    if insights is None:
        parts.append("  (No insights recorded for this meeting.)")
        return "\n".join(parts)

    if insights.summary:
        parts.append(f"  Summary: {insights.summary}")
    if insights.action_items:
        parts.append(f"  Action items: {_join_for_prompt(insights.action_items)}")
    if insights.key_decisions:
        parts.append(f"  Key decisions: {_join_for_prompt(insights.key_decisions)}")
    if insights.follow_ups:
        parts.append(f"  Follow-ups: {_join_for_prompt(insights.follow_ups)}")
    return "\n".join(parts)


def format_meeting_for_compare(
    meta: MeetingMeta,
    summary_text: str | None,
    insights: InsightsBundle | None,
    label: str,
) -> str:
    """COMPARE layout — '=== Meeting <label>: <title> · <date> ==='."""
    header = f"=== Meeting {label}: {meta.title or '(untitled)'} · {format_meeting_date(meta.date)} ==="
    parts = [header]
    if summary_text:
        parts.append(f"Summary: {summary_text}")
    elif insights and insights.summary:
        parts.append(f"Summary: {insights.summary}")
    if insights:
        if insights.key_decisions:
            parts.append(f"Decisions: {_join_for_prompt(insights.key_decisions)}")
        if insights.action_items:
            parts.append(f"Action items: {_join_for_prompt(insights.action_items)}")
        if insights.follow_ups:
            parts.append(f"Follow-ups: {_join_for_prompt(insights.follow_ups)}")
    return "\n".join(parts)


def _join_for_prompt(items: list[Any]) -> str:
    """Render a list of insight items (strings or dicts) as a single prompt-safe line.

    Each item becomes a "; "-joined entry. Dict items collapse to "k=v" pairs
    so the prompt never sees raw dict literals (e.g. `{'task': '...', 'owner': 'X'}`).
    """
    out: list[str] = []
    for it in items:
        if isinstance(it, dict):
            kv = "; ".join(f"{k}={v}" for k, v in it.items() if v is not None)
            out.append(kv or str(it))
        else:
            out.append(str(it))
    return " ; ".join(out)


# ── Group chunks by meeting (for SEARCH / HYBRID) ─────────────────────────────

def group_chunks_by_meeting(
    chunks: list[RetrievedChunk],
) -> dict[UUID, list[RetrievedChunk]]:
    """Preserve first-appearance order so the prompt's first meeting is the
    most-relevant one."""
    by_meeting: dict[UUID, list[RetrievedChunk]] = {}
    for c in chunks:
        by_meeting.setdefault(c.meeting_id, []).append(c)
    return by_meeting


# ── LLM composition wrapper ───────────────────────────────────────────────────

async def compose_with_llm(
    *,
    llm: LLMClient,
    deployment: str,
    system_prompt: str,
    context_block: str,
    user_query: str,
    history: list[dict[str, str]] | None = None,
    no_results_msg: str,
    max_tokens: int = 600,
    temperature: float = 0.3,
    user_context: str | None = None,
) -> str:
    """Standard LLM call shape used by every meeting-data route.

    `user_context` (optional) is a short block describing the asker's identity
    and their role per meeting (organizer / attendee / granted access). When
    provided, it's appended to the system prompt under a USER CONTEXT header
    so the LLM can answer attendance-style questions accurately.

    Falls back to `no_results_msg` when:
      - context_block is empty (no retrieved data)
      - LLM returns empty content
      - LLM call raises
    """
    if not context_block.strip():
        return no_results_msg

    full_system = system_prompt
    if user_context:
        full_system = f"{system_prompt}\n\nUSER CONTEXT\n{user_context}"

    messages: list[dict] = [{"role": "system", "content": full_system}]
    if history:
        messages.extend(_truncate_history(history))
    messages.append({
        "role": "user",
        "content": f"Context:\n{context_block}\n\nQuestion: {user_query}",
    })

    try:
        out = await llm.complete_text(
            deployment=deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        log.error("compose_with_llm: LLM call failed: %s", exc)
        return no_results_msg

    return out.strip() or no_results_msg


def _truncate_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep the most recent turns until the total payload size fits under
    HISTORY_MAX_CHARS (set in app.services.chat.config)."""
    result = list(history)
    total = sum(len(m.get("content", "")) for m in result)
    while result and total > HISTORY_MAX_CHARS:
        removed = result.pop(0)
        total -= len(removed.get("content", ""))
    return result
