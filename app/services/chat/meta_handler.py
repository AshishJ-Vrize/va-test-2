"""META route handler — meetings list, counts, participants, durations."""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def handle_meta(
    authorized_meeting_ids: list[uuid.UUID],
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    """
    Query meetings + participants for the authorized meeting set.
    Returns list of metadata dicts, one per meeting.
    Default date range: last 30 days when no date filter supplied.
    """
    if not authorized_meeting_ids:
        return []

    ids_str = "{" + ",".join(str(mid) for mid in authorized_meeting_ids) + "}"
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    meeting_title = filters.get("meeting_title")

    sql = text("""
        SELECT
            m.id              AS meeting_id,
            m.meeting_subject AS meeting_title,
            m.meeting_date::text AS meeting_date,
            m.duration_minutes,
            m.status,
            COUNT(mp.participant_graph_id) AS participant_count
        FROM meetings m
        LEFT JOIN meeting_participants mp ON mp.meeting_id = m.id
        WHERE m.id = ANY(CAST(:ids AS uuid[]))
          AND (CAST(:date_from AS timestamptz) IS NULL OR m.meeting_date >= CAST(:date_from AS timestamptz))
          AND (CAST(:date_to   AS timestamptz) IS NULL OR m.meeting_date <= CAST(:date_to   AS timestamptz))
          AND (CAST(:title     AS text)        IS NULL OR m.meeting_subject ILIKE :title_pattern)
          AND (
                CAST(:date_from AS timestamptz) IS NOT NULL OR CAST(:date_to AS timestamptz) IS NOT NULL OR
                CAST(:title AS text) IS NOT NULL OR
                m.meeting_date >= (NOW() - INTERVAL '30 days')
              )
        GROUP BY m.id, m.meeting_subject, m.meeting_date, m.duration_minutes, m.status
        ORDER BY m.meeting_date DESC
        LIMIT 20
    """)
    rows = await db.execute(sql, {
        "ids": ids_str,
        "date_from": date_from,
        "date_to": date_to,
        "title": meeting_title,
        "title_pattern": f"%{meeting_title}%" if meeting_title else None,
    })
    meetings = list(rows)

    if not meetings:
        return []

    # Fetch participant display names via users JOIN
    meeting_ids_str = "{" + ",".join(str(r.meeting_id) for r in meetings) + "}"
    part_sql = text("""
        SELECT mp.meeting_id, mp.participant_name AS display_name
        FROM meeting_participants mp
        WHERE mp.meeting_id = ANY(CAST(:ids AS uuid[]))
          AND mp.participant_name IS NOT NULL
        ORDER BY mp.meeting_id, mp.participant_name
    """)
    part_rows = await db.execute(part_sql, {"ids": meeting_ids_str})

    participants: dict[str, list[str]] = {}
    for row in part_rows:
        key = str(row.meeting_id)
        participants.setdefault(key, []).append(row.display_name)

    return [
        {
            "source_type": "metadata",
            "meeting_id": str(r.meeting_id),
            "meeting_title": r.meeting_title or "",
            "meeting_date": r.meeting_date,
            "duration_minutes": r.duration_minutes,
            "participant_count": r.participant_count,
            "participants": participants.get(str(r.meeting_id), []),
        }
        for r in meetings
    ]
