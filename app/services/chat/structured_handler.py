"""STRUCTURED route handler — queries meeting_insights with silent fallthrough to SEARCH."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


async def handle_structured(
    authorized_meeting_ids: list[uuid.UUID],
    filters: dict,
    db: AsyncSession,
) -> tuple[list[dict], bool]:
    """
    Query meeting_insights for the authorized meeting set.
    Returns (results, fell_through).
    fell_through=True when no insights exist — caller must run SEARCH instead.
    """
    if not authorized_meeting_ids:
        return [], True

    ids_str = "{" + ",".join(str(mid) for mid in authorized_meeting_ids) + "}"
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")

    sql = text("""
        SELECT
            mi.meeting_id,
            mi.insight_type,
            mi.fields,
            m.meeting_subject AS meeting_title,
            m.meeting_date::text AS meeting_date
        FROM meeting_insights mi
        JOIN meetings m ON mi.meeting_id = m.id
        WHERE mi.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND (:date_from IS NULL OR m.meeting_date >= CAST(:date_from AS timestamptz))
          AND (:date_to   IS NULL OR m.meeting_date <= CAST(:date_to   AS timestamptz))
        ORDER BY m.meeting_date DESC
        LIMIT 50
    """)
    rows = await db.execute(sql, {"ids": ids_str, "date_from": date_from, "date_to": date_to})
    all_rows = list(rows)

    if not all_rows:
        log.info("structured_handler: no insights found, falling through to SEARCH")
        return [], True

    # Merge separate insight_type rows into one dict per meeting
    merged: dict[str, dict] = {}
    for row in all_rows:
        mid = str(row.meeting_id)
        if mid not in merged:
            merged[mid] = {
                "source_type": "insights",
                "meeting_id": mid,
                "meeting_title": row.meeting_title or "",
                "meeting_date": row.meeting_date,
            }
        merged[mid][row.insight_type] = row.fields

    return list(merged.values()), False
