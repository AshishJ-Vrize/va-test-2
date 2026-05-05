"""MetadataRepoImpl — production MetadataRepo implementation.

Reads from `meetings`, `meeting_participants`, and (indirectly) `transcripts`
for the META handler and for `scope.narrow_within_scope()`.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.interfaces import MeetingMeta


class MetadataRepoImpl:
    """Default MetadataRepo implementation."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_meetings(self, meeting_ids: list[UUID]) -> list[MeetingMeta]:
        """Fetch meetings + their participants in a single round-trip pair."""
        if not meeting_ids:
            return []

        ids_str = "{" + ",".join(str(mid) for mid in meeting_ids) + "}"

        meetings_sql = text("""
            SELECT
                m.id, m.meeting_subject, m.meeting_date, m.duration_minutes,
                m.organizer_name, m.status
            FROM meetings m
            WHERE m.id = ANY(CAST(:ids AS uuid[]))
            ORDER BY m.meeting_date DESC
        """)
        rows = list(await self._db.execute(meetings_sql, {"ids": ids_str}))
        if not rows:
            return []

        # Pull participants for these meetings in one query, group in Python.
        participants_by_meeting = await self.get_participants(meeting_ids)

        return [
            MeetingMeta(
                meeting_id=r.id,
                title=r.meeting_subject or "",
                date=r.meeting_date,
                duration_minutes=r.duration_minutes,
                organizer_name=r.organizer_name,
                participants=participants_by_meeting.get(r.id, []),
                status=r.status,
            )
            for r in rows
        ]

    async def get_participants(
        self, meeting_ids: list[UUID]
    ) -> dict[UUID, list[dict[str, Any]]]:
        """Return {meeting_id → [{name, email, role, graph_id}, ...]}."""
        if not meeting_ids:
            return {}

        ids_str = "{" + ",".join(str(mid) for mid in meeting_ids) + "}"
        sql = text("""
            SELECT
                meeting_id,
                participant_name AS name,
                participant_email AS email,
                role,
                participant_graph_id AS graph_id
            FROM meeting_participants
            WHERE meeting_id = ANY(CAST(:ids AS uuid[]))
            ORDER BY meeting_id, role
        """)
        result: dict[UUID, list[dict[str, Any]]] = {}
        for r in await self._db.execute(sql, {"ids": ids_str}):
            result.setdefault(r.meeting_id, []).append({
                "name": r.name,
                "email": r.email,
                "role": r.role,
                "graph_id": r.graph_id,
            })
        return result

    async def search_by_title(
        self,
        candidate_titles: list[str],
        allowed_meeting_ids: list[UUID] | None = None,
    ) -> list[UUID]:
        """ILIKE-match meeting titles. Returns matching meeting_ids.

        Used by `scope.narrow_within_scope()` to resolve user references like
        "the Acme renewal review" to actual meeting_ids within the user's
        allowed set.
        """
        if not candidate_titles:
            return []

        # Build a single OR'd ILIKE expression for all candidates.
        # Each title becomes a pattern '%<title>%'.
        like_clauses = []
        params: dict[str, Any] = {}
        for i, t in enumerate(candidate_titles):
            params[f"t{i}"] = f"%{t}%"
            like_clauses.append(f"meeting_subject ILIKE :t{i}")
        title_clause = " OR ".join(like_clauses)

        sql_str = f"""
            SELECT id FROM meetings
            WHERE ({title_clause})
        """
        if allowed_meeting_ids:
            params["ids"] = "{" + ",".join(str(mid) for mid in allowed_meeting_ids) + "}"
            sql_str += " AND id = ANY(CAST(:ids AS uuid[]))"

        rows = await self._db.execute(text(sql_str), params)
        return [r.id for r in rows]

    async def get_authorized_meeting_ids(
        self,
        graph_id: str,
        access_filter: str = "all",
        within_days: int = 30,
        max_meetings: int = 0,
    ) -> list[UUID]:
        """RBAC scope: meeting_ids the user can see, narrowed by access role and recency.

        access_filter:
          'attended' — role IN ('organizer','attendee')
          'granted'  — role = 'granted'
          'all'      — any role (default)

        within_days and max_meetings combine independently — pass 0 to disable
        the corresponding bound:
          - only date window  → within_days > 0,  max_meetings = 0
          - only count cap    → within_days = 0,  max_meetings > 0
          - both (intersection) → both > 0  (whichever is more restrictive wins)
          - both = 0          → no recency RBAC (membership check only)
        """
        params: dict[str, Any] = {"gid": graph_id}
        role_clause = ""
        if access_filter == "attended":
            role_clause = " AND mp.role IN ('organizer','attendee')"
        elif access_filter == "granted":
            role_clause = " AND mp.role = 'granted'"

        date_clause = ""
        if within_days and within_days > 0:
            params["days"] = within_days
            date_clause = " AND m.meeting_date >= NOW() - (:days || ' days')::interval"

        limit_clause = ""
        if max_meetings and max_meetings > 0:
            params["max_meetings"] = max_meetings
            limit_clause = " LIMIT :max_meetings"

        # ORDER BY meeting_date DESC ensures the LIMIT keeps the most-recent
        # meetings (and is harmless when no LIMIT is applied).
        sql = text(f"""
            SELECT m.id
            FROM meetings m
            JOIN meeting_participants mp ON mp.meeting_id = m.id
            WHERE mp.participant_graph_id = :gid
              {role_clause}
              {date_clause}
            GROUP BY m.id, m.meeting_date
            ORDER BY m.meeting_date DESC
            {limit_clause}
        """)
        rows = await self._db.execute(sql, params)
        return [r.id for r in rows]

    async def count_authorized_meetings(
        self,
        graph_id: str,
        access_filter: str = "all",
        within_days: int = 30,
    ) -> int:
        """Total authorised meetings in the date window — unbounded by max_meetings."""
        params: dict[str, Any] = {"gid": graph_id}
        role_clause = ""
        if access_filter == "attended":
            role_clause = " AND mp.role IN ('organizer','attendee')"
        elif access_filter == "granted":
            role_clause = " AND mp.role = 'granted'"

        date_clause = ""
        if within_days and within_days > 0:
            params["days"] = within_days
            date_clause = " AND m.meeting_date >= NOW() - (:days || ' days')::interval"

        sql = text(f"""
            SELECT COUNT(DISTINCT m.id) AS total
            FROM meetings m
            JOIN meeting_participants mp ON mp.meeting_id = m.id
            WHERE mp.participant_graph_id = :gid
              {role_clause}
              {date_clause}
        """)
        result = await self._db.execute(sql, params)
        row = result.first()
        return int(row.total) if row else 0

    async def get_user_role_per_meeting(
        self,
        graph_id: str,
        meeting_ids: list[UUID],
    ) -> dict[UUID, str]:
        """Return {meeting_id → role} for the given user across the given meetings."""
        if not meeting_ids:
            return {}
        ids_str = "{" + ",".join(str(mid) for mid in meeting_ids) + "}"
        sql = text("""
            SELECT meeting_id, role
            FROM meeting_participants
            WHERE participant_graph_id = :gid
              AND meeting_id = ANY(CAST(:ids AS uuid[]))
        """)
        rows = await self._db.execute(sql, {"gid": graph_id, "ids": ids_str})
        return {r.meeting_id: r.role for r in rows}

    async def get_user_display_name(self, graph_id: str) -> str | None:
        """Return users.display_name for the given graph_id, or None if unknown."""
        sql = text("SELECT display_name FROM users WHERE graph_id = :gid")
        result = await self._db.execute(sql, {"gid": graph_id})
        row = result.first()
        return row.display_name if row else None

    async def get_meetings_in_date_range(
        self,
        date_from: str | None,
        date_to: str | None,
        allowed_meeting_ids: list[UUID] | None = None,
    ) -> list[UUID]:
        """Return meeting_ids whose date falls in [date_from, date_to].

        Both bounds are inclusive of the full day (date_to + 1 day open upper).
        Either bound may be None — open on that side.
        """
        clauses: list[str] = []
        params: dict[str, Any] = {}

        if date_from:
            params["date_from"] = date_from
            clauses.append(
                "meeting_date >= CAST(:date_from AS timestamptz)"
            )
        if date_to:
            params["date_to"] = date_to
            # +1 day so the upper bound is inclusive of the full date.
            clauses.append(
                "meeting_date < CAST(:date_to AS timestamptz) + INTERVAL '1 day'"
            )
        if allowed_meeting_ids:
            params["ids"] = "{" + ",".join(str(mid) for mid in allowed_meeting_ids) + "}"
            clauses.append("id = ANY(CAST(:ids AS uuid[]))")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = text(f"SELECT id FROM meetings{where}")
        rows = await self._db.execute(sql, params)
        return [r.id for r in rows]
