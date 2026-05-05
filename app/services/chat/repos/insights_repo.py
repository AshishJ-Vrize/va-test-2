"""InsightsRepoImpl — production InsightsRepo implementation.

Reads from `meeting_insights` and `meeting_summaries`. The `meeting_insights`
table stores each insight type as a separate row with a JSONB `fields` column
shaped as either:
    {"items": [...]}   for arrays (action_items, key_decisions, follow_ups, key_topics)
    {"text":  "..."}   for strings (summary, sentiment_overview)

The `_normalize_field()` pure function unwraps that envelope so handlers and
prompts see plain Python lists/strings/None — never raw JSONB dicts. This
fixes the long-standing "action items render as `{'items': [...]}` literal"
bug at the source.

The shape mapping (storage `insight_type` → bundle field name):
    'summary'             → bundle.summary           (str | None)
    'action_items'        → bundle.action_items      (list[Any])
    'key_topics'          → bundle.key_decisions     (list[Any])  *
    'sentiment_overview'  → ignored (not in bundle for v1)

* The storage type is `key_topics` for historical reasons, but the LLM prompt
  generates them under `key_decisions`. The repo translates the storage name
  back to the bundle's `key_decisions` field. Follow-ups are not currently
  stored as a separate insight_type — they live inside the JSON shape; for v1
  bundle.follow_ups is always [].
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.interfaces import InsightsBundle


_STORAGE_TO_BUNDLE_FIELD = {
    "summary":      "summary",
    "action_items": "action_items",
    "key_topics":   "key_decisions",
    # 'sentiment_overview' intentionally not mapped — v1 ignores it.
}


def _normalize_field(value: Any) -> Any:
    """Unwrap the {"items": [...]} or {"text": "..."} envelope.

    Pure function — DB-free, fully unit-testable.

    Behaviour:
      {"items": [...]}      → [...]
      {"text":  "..."}      → "..."
      already a list/str    → returned as-is
      None / {}             → None / None
      anything unexpected   → str() of the value
    """
    if value is None:
        return None
    if isinstance(value, dict):
        if not value:
            return None
        if "items" in value:
            inner = value["items"]
            return inner if isinstance(inner, list) else [inner]
        if "text" in value:
            inner = value["text"]
            return inner if isinstance(inner, str) else str(inner)
        # Unrecognised dict envelope — surface stringified for visibility.
        return str(value)
    return value


class InsightsRepoImpl:
    """Default InsightsRepo implementation."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_insights(self, meeting_ids: list[UUID]) -> list[InsightsBundle]:
        """Fetch + merge insights for the given meetings.

        Returns one InsightsBundle per meeting that has at least one insight row.
        Meetings with NO insights (e.g. >90 min and skipped, or generation failed)
        are simply absent from the result — caller's responsibility to handle
        missing data per-route (e.g. STRUCTURED falls through to SEARCH).
        """
        if not meeting_ids:
            return []

        ids_str = "{" + ",".join(str(mid) for mid in meeting_ids) + "}"
        sql = text("""
            SELECT
                mi.meeting_id,
                mi.insight_type,
                mi.fields,
                m.meeting_subject AS meeting_title,
                m.meeting_date
            FROM meeting_insights mi
            JOIN meetings m ON m.id = mi.meeting_id
            WHERE mi.meeting_id = ANY(CAST(:ids AS uuid[]))
        """)
        rows = await self._db.execute(sql, {"ids": ids_str})

        # Aggregate per meeting_id; build the bundle once we've seen all rows.
        per_meeting: dict[UUID, dict] = {}
        for r in rows:
            mid = r.meeting_id
            if mid not in per_meeting:
                per_meeting[mid] = {
                    "meeting_id": mid,
                    "meeting_title": r.meeting_title or "",
                    "meeting_date": r.meeting_date,
                    "summary": None,
                    "action_items": [],
                    "key_decisions": [],
                    "follow_ups": [],
                }
            field_name = _STORAGE_TO_BUNDLE_FIELD.get(r.insight_type)
            if field_name is None:
                continue
            normalized = _normalize_field(r.fields)
            if field_name == "summary":
                per_meeting[mid]["summary"] = normalized if isinstance(normalized, str) else None
            else:
                # action_items, key_decisions — expect list
                if isinstance(normalized, list):
                    per_meeting[mid][field_name] = normalized
                elif normalized is not None:
                    per_meeting[mid][field_name] = [normalized]

        return [InsightsBundle(**data) for data in per_meeting.values()]

    async def get_summary_text(self, meeting_id: UUID) -> str | None:
        """Returns meeting_summaries.summary_text if present, else None.

        Used by the COMPARE handler — comparison works at summary level.
        """
        sql = text("""
            SELECT summary_text FROM meeting_summaries
            WHERE meeting_id = :mid
        """)
        result = await self._db.execute(sql, {"mid": str(meeting_id)})
        row = result.first()
        return row.summary_text if row else None
