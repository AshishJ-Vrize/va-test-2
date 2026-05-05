"""TenantSpeakerResolver — tenant-wide name → graph_id resolution for chat.

Different from `app.services.ingestion.speaker_resolver` (which scopes to ONE
meeting's participants at ingest time). This resolver searches across the
ENTIRE tenant's `meeting_participants` table because the chat router doesn't
know which specific meeting the user is referring to yet.

Tiered match (try in order, accept first tier with ≥1 result):
  1. Exact case-insensitive on participant_name
  2. First-name-only match (split_part)

When 2+ candidates match, we return ALL of them so the orchestrator can ask
the user to disambiguate using their email addresses.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.interfaces import SpeakerCandidate


class TenantSpeakerResolver:
    """Default SpeakerResolver implementation."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def resolve(self, name: str) -> list[SpeakerCandidate]:
        """Return all candidate speakers for the given name.

        Empty list when no match. Multiple entries when the name is ambiguous
        (e.g. two Ashishes in the tenant).
        """
        cleaned = (name or "").strip()
        if not cleaned:
            return []

        # Tier 1: exact full-name match.
        rows = list(await self._db.execute(
            text("""
                SELECT DISTINCT ON (participant_graph_id)
                    participant_graph_id, participant_name, participant_email
                FROM meeting_participants
                WHERE LOWER(TRIM(participant_name)) = LOWER(:name)
            """),
            {"name": cleaned},
        ))
        if rows:
            return [
                SpeakerCandidate(
                    name=r.participant_name,
                    email=r.participant_email,
                    graph_id=r.participant_graph_id,
                )
                for r in rows
            ]

        # Tier 2: first-name match.
        first = cleaned.split()[0] if cleaned.split() else cleaned
        rows = list(await self._db.execute(
            text("""
                SELECT DISTINCT ON (participant_graph_id)
                    participant_graph_id, participant_name, participant_email
                FROM meeting_participants
                WHERE LOWER(SPLIT_PART(TRIM(participant_name), ' ', 1)) = LOWER(:first)
            """),
            {"first": first},
        ))
        return [
            SpeakerCandidate(
                name=r.participant_name,
                email=r.participant_email,
                graph_id=r.participant_graph_id,
            )
            for r in rows
        ]
