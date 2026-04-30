"""Final GPT-4o answer generation — route-aware, with chat history context."""
from __future__ import annotations

import logging

from app.config.settings import get_settings
from app.services.chat.prompts import ROUTE_PROMPTS

log = logging.getLogger(__name__)

_MAX_CONTEXT_ITEMS = 10
_NO_RESULTS_MSG = "I couldn't find anything relevant in your meetings."


async def generate_answer(
    query: str,
    route: str,
    handler_result: list[dict],
    history: list[dict[str, str]],
) -> str:
    """
    Call GPT-4o with route-specific system prompt, chat history (last 10 turns),
    and the handler's retrieved data. Returns the answer string.
    """
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
    """Format handler results into a context string for the LLM."""
    parts: list[str] = []

    for i, item in enumerate(items[:_MAX_CONTEXT_ITEMS], 1):
        stype = item.get("source_type", "")
        title = item.get("meeting_title", "")
        date = item.get("meeting_date", "")
        header = f"[{i}] {title}" + (f" ({date})" if date else "")

        if stype == "metadata":
            participants = ", ".join(item.get("participants") or [])
            duration = item.get("duration_minutes")
            parts.append(
                f"{header}\n"
                f"  Participants: {participants or 'unknown'}\n"
                f"  Duration: {duration} min" if duration else f"{header}\n  Participants: {participants or 'unknown'}"
            )

        elif stype == "insights":
            lines = [header]
            if item.get("summary"):
                lines.append(f"  Summary: {_safe_text(item['summary'])}")
            if item.get("action_items"):
                lines.append(f"  Action items: {_safe_text(item['action_items'])}")
            if item.get("key_topics"):
                lines.append(f"  Key topics: {_safe_text(item['key_topics'])}")
            if item.get("sentiment_overview"):
                lines.append(f"  Sentiment: {_safe_text(item['sentiment_overview'])}")
            parts.append("\n".join(lines))

        elif stype == "transcript":
            speaker = item.get("speaker_name") or "Unknown"
            ts = _ms_to_display(item.get("timestamp_ms"))
            text_val = item.get("text", "")
            ts_str = f" · {ts}" if ts else ""
            parts.append(f"{header}\n  {speaker}{ts_str}: \"{text_val}\"")

    return "\n\n".join(parts) if parts else "(No relevant content found.)"


def _safe_text(val) -> str:
    if isinstance(val, list):
        return "; ".join(str(v) for v in val)
    return str(val)


def _ms_to_display(ms: int | None) -> str | None:
    if ms is None:
        return None
    total = ms // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def _truncate_history(history: list[dict], max_chars: int = 6000) -> list[dict]:
    """Truncate from the oldest end to stay within max_chars total."""
    result = list(history)
    total = sum(len(m.get("content", "")) for m in result)
    while result and total > max_chars:
        removed = result.pop(0)
        total -= len(removed.get("content", ""))
    return result
