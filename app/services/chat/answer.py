"""Final GPT-4o answer generation — route-aware, meeting-grouped context.

Renders retrieved items into a context string organised by meeting, then
calls the LLM with route-specific system prompt and chat history. Each
meeting block contains its metadata (title, date, ID) once, followed by
zero or more transcript chunks (each with time span + multi-speaker
utterances), insight blocks, or metadata fields.

Format
------
    Meeting: <title>  |  Date: <YYYY-MM-DD HH:MM>  |  ID: <meeting_id>
      Participants: ...                            (when META)
      Duration: <X> min                            (when META)
      Summary: ...                                 (when STRUCTURED)
      Action items: ...                            (when STRUCTURED)
      Key topics: ...                              (when STRUCTURED)
      [Time: MM:SS – MM:SS]                        (per transcript chunk)
        <full_name>: "<text>"
        <full_name>: "<text>"
      [Time: MM:SS – MM:SS] Context: <chunk_context>
        <full_name>: "<text>"

    Meeting: <next title>  |  ...
      ...

Meetings appear in first-rank order (the meeting whose top-ranked chunk
ranks highest comes first).
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime

from app.config.settings import get_settings
from app.services.chat.prompts import ROUTE_PROMPTS

log = logging.getLogger(__name__)

_MAX_CONTEXT_ITEMS = 15
_NO_RESULTS_MSG = "I couldn't find anything relevant in your meetings."


async def generate_answer(
    query: str,
    route: str,
    handler_result: list[dict],
    history: list[dict[str, str]],
) -> str:
    """Call GPT-4o with route-specific system prompt, history, and grouped context."""
    from app.services.ingestion.contextualizer import _get_client

    system = ROUTE_PROMPTS.get(route, ROUTE_PROMPTS["SEARCH"])
    deployment = get_settings().AZURE_OPENAI_DEPLOYMENT_LLM
    client = _get_client()

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(_truncate_history(history))
    if handler_result:
        context = _build_context(handler_result, route)
        messages.append({"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"})
    else:
        messages.append({"role": "user", "content": query})

    try:
        resp = await client.chat.completions.create(
            model=deployment,
            messages=messages,
            temperature=0.3,
            max_tokens=600,
        )
        return (resp.choices[0].message.content or "").strip() or _NO_RESULTS_MSG
    except Exception as exc:
        log.error("answer: LLM call failed: %s", exc)
        return _NO_RESULTS_MSG


def _build_context(items: list[dict], route: str) -> str:
    """Group items by meeting_id (preserving rank order) and render the prompt."""
    # Group by meeting_id while preserving first-appearance (rank) order.
    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for item in items[:_MAX_CONTEXT_ITEMS]:
        mid = str(item.get("meeting_id") or "")
        if not mid:
            continue
        grouped.setdefault(mid, []).append(item)

    if not grouped:
        return "(No relevant content found.)"

    blocks: list[str] = []
    for mid, group in grouped.items():
        blocks.append(_render_meeting_block(mid, group))
    return "\n\n".join(blocks)


def _render_meeting_block(meeting_id: str, items: list[dict]) -> str:
    """Render one meeting and all its retrieved items under a single header."""
    # Pick title and date from the first item that carries them.
    title = next((it.get("meeting_title") or "" for it in items if it.get("meeting_title")), "")
    date_raw = next((it.get("meeting_date") for it in items if it.get("meeting_date")), None)
    date_str = _format_meeting_date(date_raw)

    header_parts = [f"Meeting: {title or '(untitled)'}"]
    if date_str:
        header_parts.append(f"Date: {date_str}")
    header_parts.append(f"ID: {meeting_id}")
    lines: list[str] = ["  |  ".join(header_parts)]

    # Render each item's body (indented under the header).
    for item in items:
        body = _render_item(item)
        if body:
            lines.append(body)
    return "\n".join(lines)


def _render_item(item: dict) -> str:
    """Render the body of a single retrieved item based on its source_type."""
    stype = item.get("source_type", "")

    if stype == "metadata":
        return _render_metadata(item)
    if stype == "insights":
        return _render_insights(item)
    if stype == "transcript":
        return _render_transcript_chunk(item)
    return ""


def _render_metadata(item: dict) -> str:
    parts: list[str] = []
    participants = ", ".join(item.get("participants") or [])
    parts.append(f"  Participants: {participants or 'unknown'}")
    duration = item.get("duration_minutes")
    if duration:
        parts.append(f"  Duration: {duration} min")
    return "\n".join(parts)


def _render_insights(item: dict) -> str:
    parts: list[str] = []
    if item.get("summary"):
        parts.append(f"  Summary: {_safe_text(item['summary'])}")
    if item.get("action_items"):
        parts.append(f"  Action items: {_safe_text(item['action_items'])}")
    if item.get("key_topics"):
        parts.append(f"  Key topics: {_safe_text(item['key_topics'])}")
    if item.get("sentiment_overview"):
        parts.append(f"  Sentiment: {_safe_text(item['sentiment_overview'])}")
    return "\n".join(parts)


def _render_transcript_chunk(item: dict) -> str:
    """Render a multi-turn chunk: time span line + one indented line per turn.

    Pulls full names from the chunk_text JSON utterances. If chunk_text is
    missing or empty, falls back to a flat line using `speakers` + nothing.
    """
    start = _ms_to_display(item.get("start_ms") or item.get("timestamp_ms"))
    end = _ms_to_display(item.get("end_ms"))
    span = f"[Time: {start or '00:00'} – {end or '00:00'}]"
    context = item.get("chunk_context")
    if context:
        span += f" Context: {context}"

    lines: list[str] = [f"  {span}"]
    chunk_text = item.get("chunk_text") or []
    for turn in chunk_text:
        n = (turn.get("n") or "Unknown").strip()
        t = (turn.get("t") or "").strip()
        if t:
            lines.append(f'    {n}: "{t}"')
    if len(lines) == 1:
        # No utterances rendered — show speakers list as a fallback.
        speakers = item.get("speakers") or []
        if speakers:
            lines.append(f"    {', '.join(speakers)}: (text unavailable)")
    return "\n".join(lines)


# ── Formatting helpers ───────────────────────────────────────────────────────

def _safe_text(val) -> str:
    """Render insight values cleanly. Unwraps the {items: [...]} / {text: ...}
    shape used in meeting_insights.fields JSONB before stringifying."""
    if isinstance(val, dict):
        if "items" in val:
            val = val["items"]
        elif "text" in val:
            val = val["text"]
    if isinstance(val, list):
        return "; ".join(str(v) for v in val)
    return str(val)


def _ms_to_display(ms: int | None) -> str | None:
    """Render ms as MM:SS, or HH:MM:SS when over an hour."""
    if ms is None:
        return None
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_meeting_date(raw) -> str | None:
    """Format meeting_date (text or datetime) as 'YYYY-MM-DD HH:MM'.

    Accepts a Postgres `timestamptz::text` string or a Python datetime.
    Falls back to the raw string if parsing fails.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d %H:%M")
    s = str(raw).strip()
    if not s:
        return None
    # Try a few common shapes coming from Postgres.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.replace("+00", "+0000")
                                      if s.endswith(("+00", "-00")) else s, fmt
                                     ).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return s  # last resort — give the LLM whatever we got


def _truncate_history(history: list[dict], max_chars: int = 6000) -> list[dict]:
    """Truncate from the oldest end to stay within max_chars total."""
    result = list(history)
    total = sum(len(m.get("content", "")) for m in result)
    while result and total > max_chars:
        removed = result.pop(0)
        total -= len(removed.get("content", ""))
    return result
