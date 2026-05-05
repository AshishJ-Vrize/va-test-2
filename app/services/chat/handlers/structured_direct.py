"""STRUCTURED_DIRECT handler — direct extraction from cached insights, no LLM.

Two output modes, decided by query intent:

  - LIST mode (action items / decisions / follow-ups specifically):
      _requested_fields() picks which insight fields to show. Layout is the
      "## Title — date" + bulleted-section format used by the existing tests.

  - DIGEST mode (summary / tl;dr / gist / recap / overview):
      Full per-meeting digest matching STRUCTURED_LLM's layout
      (**Meeting** / **Overview** / **Key Decisions** / **Action Items** /
      **Follow-ups**). Empty sections show "None recorded.". Cached summary
      text is reused VERBATIM — no paraphrasing, no LLM cost.

If a meeting has zero content across all fields, it's skipped. If ALL meetings
are empty, the handler returns the no-results message and the orchestrator
falls through to SEARCH.
"""
from __future__ import annotations

from uuid import UUID

from app.services.chat.answer import (
    _join_for_prompt as _stringify_items,
    format_meeting_date,
)
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.interfaces import InsightsBundle, InsightsRepo
from app.services.chat.sources import build_sources_from_insights

_NO_RESULTS = "I couldn't find anything matching that in your meetings."


# ── Owner / speaker filter helpers ────────────────────────────────────────────
#
# When the router extracted a speaker_name (e.g. "Ashish Jaiswal"), we filter
# action_items / decisions / follow-ups to ONLY rows where the cached `owner`
# field matches. Avoids dumping "all action items across all meetings" when
# the user asked specifically for one person.

def _name_matches(filter_name: str | None, owner: str | None) -> bool:
    """Substring-either-direction match (case-insensitive). Empty inputs miss."""
    fn = (filter_name or "").strip().lower()
    on = (owner or "").strip().lower()
    if not fn or not on:
        return False
    return fn in on or on in fn


def _filter_items_by_owner(items: list, name_filter: str | None) -> list:
    """Keep only items whose `owner` field matches `name_filter`.

    String-typed items have no owner metadata; we keep them only if the
    string itself contains the filter name.
    """
    if not name_filter:
        return items
    out: list = []
    for item in items:
        if isinstance(item, dict):
            if _name_matches(name_filter, item.get("owner")):
                out.append(item)
        elif isinstance(item, str):
            if name_filter.lower() in item.lower():
                out.append(item)
    return out


# ── Field selection from query keywords ───────────────────────────────────────

_ACTION_TOKENS = ("action", "task", "todo", "to-do")
_DECISION_TOKENS = ("decision", "decided", "resolved", "agreed")
_FOLLOWUP_TOKENS = ("follow", "open question", "pending", "deferred", "outstanding")

# Map: insight bundle field name → display label for the answer header.
_FIELD_LABELS = {
    "action_items": "Action items",
    "key_decisions": "Decisions",
    "follow_ups": "Follow-ups",
}


def _requested_fields(query: str) -> list[str]:
    q = query.lower()
    fields: list[str] = []
    if any(t in q for t in _ACTION_TOKENS):
        fields.append("action_items")
    if any(t in q for t in _DECISION_TOKENS):
        fields.append("key_decisions")
    if any(t in q for t in _FOLLOWUP_TOKENS):
        fields.append("follow_ups")
    # When ambiguous, return all sections that have content.
    return fields or ["action_items", "key_decisions", "follow_ups"]


# ── Summary intent detection ──────────────────────────────────────────────────

# Substrings that signal the user wants a full digest (Overview + all fields)
# rather than a single-field list. Kept loose on purpose — the LLM router is
# the primary classifier; this is a backup heuristic that only runs once we've
# already been routed to STRUCTURED_DIRECT.
_SUMMARY_TOKENS = (
    "summari",      # summarise / summarize / summary
    "tl;dr",
    "tldr",
    "recap",
    "overview",
    "gist",
    "what happened in",
    "what was discussed",
    "give me the rundown",
    "rundown",
)


def _is_summary_query(query: str) -> bool:
    q = query.lower()
    return any(t in q for t in _SUMMARY_TOKENS)


# ── Handler ───────────────────────────────────────────────────────────────────

async def handle_structured_direct(
    *,
    query: str,
    meeting_ids: list[UUID],
    insights_repo: InsightsRepo,
    structured_intent: str | None = None,
    speaker_name_filter: str | None = None,
) -> HandlerResult:
    """If `structured_intent` is supplied (by the LLM router), it wins.
    Otherwise we fall back to keyword detection on the query string —
    that path keeps things working when the router didn't emit a hint.

    `speaker_name_filter` (when set) restricts items to those whose `owner`
    matches — used for queries like "action items of Ashish Jaiswal"."""
    if not meeting_ids:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    insights = await insights_repo.get_insights(meeting_ids)
    if not insights:
        return HandlerResult(answer=_NO_RESULTS, is_empty=True)

    # Map router hint → handler behaviour.
    # When no hint, fall through to existing keyword heuristics.
    if structured_intent == "digest" or (
        structured_intent is None and _is_summary_query(query)
    ):
        answer = _format_full_digest(insights, name_filter=speaker_name_filter)
    elif structured_intent == "list_actions":
        answer = _format_direct_answer(insights, ["action_items"], name_filter=speaker_name_filter)
    elif structured_intent == "list_decisions":
        answer = _format_direct_answer(insights, ["key_decisions"], name_filter=speaker_name_filter)
    elif structured_intent == "list_followups":
        answer = _format_direct_answer(insights, ["follow_ups"], name_filter=speaker_name_filter)
    else:
        fields = _requested_fields(query)
        answer = _format_direct_answer(insights, fields, name_filter=speaker_name_filter)

    if not answer.strip():
        # Empty answer: every selected meeting had no useful insight data.
        # is_empty=True signals the orchestrator to fall through to SEARCH
        # (transcript chunks may still have something).
        return HandlerResult(
            answer=_NO_RESULTS,
            sources=build_sources_from_insights(insights),
            referenced_meeting_ids=[ib.meeting_id for ib in insights],
            is_empty=True,
        )

    return HandlerResult(
        answer=answer,
        sources=build_sources_from_insights(insights),
        referenced_meeting_ids=[ib.meeting_id for ib in insights],
    )


def _format_direct_answer(
    insights: list[InsightsBundle],
    fields: list[str],
    name_filter: str | None = None,
) -> str:
    """Render insights as Markdown sections, grouped by meeting.

    Layout:
      ## <Meeting title> — <date>
      **Action items**
      - <item>
      **Decisions**
      - <item>
      ...

    When `name_filter` is set, items whose `owner` doesn't match are filtered
    out. Meetings with zero matching items across all requested fields are
    skipped — no empty headers in the output.
    """
    sections: list[str] = []
    for ib in insights:
        block = [f"## {ib.meeting_title or '(untitled)'} — {format_meeting_date(ib.meeting_date)}"]
        any_content = False

        for f in fields:
            label = _FIELD_LABELS[f]
            items = getattr(ib, f, []) or []
            items = _filter_items_by_owner(items, name_filter)
            if not items:
                continue
            any_content = True
            block.append(f"**{label}**")
            for item in items:
                rendered = _render_item(item)
                if rendered:
                    block.append(f"- {rendered}")

        if any_content:
            sections.append("\n".join(block))
    return "\n\n".join(sections)


def _format_full_digest(
    insights: list[InsightsBundle],
    name_filter: str | None = None,
) -> str:
    """DIGEST mode — full per-meeting insight dump matching STRUCTURED_LLM's layout.

    Layout (per meeting):
      **Meeting** — <title> · <date>

      **Overview**
      <summary text VERBATIM, or "None recorded." when empty>

      **Key Decisions**
      - <item>     (or "None recorded.")

      **Action Items**
      - <item>     (or "None recorded.")

      **Follow-ups / Open Questions**
      - <item>     (or "None recorded.")

    Meetings with NO content across all 4 fields are skipped entirely. When
    `name_filter` is set, the summary is dropped (it's not owner-attributable)
    and lists are filtered to that owner — meetings with no remaining matches
    after filtering are skipped.
    """
    sections: list[str] = []
    for ib in insights:
        decisions = _filter_items_by_owner(ib.key_decisions or [], name_filter)
        actions = _filter_items_by_owner(ib.action_items or [], name_filter)
        followups = _filter_items_by_owner(ib.follow_ups or [], name_filter)

        # Drop the freeform summary when filtering by owner — it's per-meeting
        # narrative, not owner-attributable, and would re-introduce noise.
        summary = "" if name_filter else (ib.summary or "")

        if not (summary or decisions or actions or followups):
            continue

        block: list[str] = [
            f"**Meeting** — {ib.meeting_title or '(untitled)'} · {format_meeting_date(ib.meeting_date)}",
            "",
        ]
        if summary:
            block += ["**Overview**", summary, ""]
        block.append("**Key Decisions**")
        block.extend(_render_bullets(decisions))
        block += ["", "**Action Items**"]
        block.extend(_render_bullets(actions))
        block += ["", "**Follow-ups / Open Questions**"]
        block.extend(_render_bullets(followups))

        sections.append("\n".join(block))

    return "\n\n".join(sections)


def _render_bullets(items: list) -> list[str]:
    """Render a list of insight items as Markdown bullets, or `["None recorded."]`."""
    if not items:
        return ["None recorded."]
    out: list[str] = []
    for item in items:
        rendered = _render_item(item)
        if rendered:
            out.append(f"- {rendered}")
    return out or ["None recorded."]


def _render_item(item) -> str:
    """Render a single insight item — string or dict.

    Dicts get rendered as a primary clause + parenthesised metadata so the
    user-visible answer never shows raw `{'task': '...', 'owner': 'X'}`.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        # Prefer the most descriptive single field for the headline.
        primary_key = next(
            (k for k in ("task", "decision", "item", "title", "text") if k in item),
            None,
        )
        if primary_key:
            primary = str(item[primary_key])
            extras = [
                f"{k}: {v}" for k, v in item.items()
                if k != primary_key and v not in (None, "", [])
            ]
            return primary + (f" ({'; '.join(extras)})" if extras else "")
        # Fall back to a "k: v" rendering — never expose dict literal syntax.
        return _stringify_items([item])
    return str(item)
