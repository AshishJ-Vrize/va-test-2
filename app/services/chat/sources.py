"""Source-card builder for the UI's collapsible "Sources" panel.

Per Question-4b in the plan: clean prose answer in the chat bubble, sources
shown in an expandable card below. Each card represents one meeting and
collects the time-spans / speakers / metadata used by the answer.

Caps at 5 source cards (UI affordance — the rest is hidden behind "show more"
in a future iteration).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.services.chat.config import MAX_SOURCE_CARDS
from app.services.chat.interfaces import (
    InsightsBundle,
    MeetingMeta,
    RetrievedChunk,
)


@dataclass
class SourceCardTimespan:
    start_ms: int
    end_ms: int


@dataclass
class SourceCard:
    meeting_id: UUID
    meeting_title: str
    meeting_date: datetime | None
    source_type: str                       # 'transcript' | 'insights' | 'metadata' | 'summary'
    speakers: list[str] = field(default_factory=list)
    timespans: list[SourceCardTimespan] = field(default_factory=list)


_DEFAULT_MAX_SOURCES = MAX_SOURCE_CARDS


# ── Builders per route ────────────────────────────────────────────────────────

def build_sources_from_chunks(
    chunks: list[RetrievedChunk],
    max_sources: int = _DEFAULT_MAX_SOURCES,
) -> list[SourceCard]:
    """One card per meeting, aggregating all timespans/speakers from retrieved chunks.

    Order follows first-appearance in the (already RRF + round-robin sorted) list.
    """
    by_meeting: dict[UUID, SourceCard] = {}
    order: list[UUID] = []
    for c in chunks:
        if c.meeting_id not in by_meeting:
            by_meeting[c.meeting_id] = SourceCard(
                meeting_id=c.meeting_id,
                meeting_title=c.meeting_title,
                meeting_date=c.meeting_date,
                source_type="transcript",
            )
            order.append(c.meeting_id)
        card = by_meeting[c.meeting_id]
        card.timespans.append(SourceCardTimespan(start_ms=c.start_ms, end_ms=c.end_ms))
        for sn in c.speakers:
            if sn not in card.speakers:
                card.speakers.append(sn)

    return [by_meeting[mid] for mid in order[:max_sources]]


def build_sources_from_insights(
    insights: list[InsightsBundle],
    max_sources: int = _DEFAULT_MAX_SOURCES,
) -> list[SourceCard]:
    """One card per meeting that contributed insight data."""
    cards: list[SourceCard] = []
    for ib in insights[:max_sources]:
        cards.append(SourceCard(
            meeting_id=ib.meeting_id,
            meeting_title=ib.meeting_title,
            meeting_date=ib.meeting_date,
            source_type="insights",
        ))
    return cards


def build_sources_from_meetings(
    meetings: list[MeetingMeta],
    max_sources: int = _DEFAULT_MAX_SOURCES,
) -> list[SourceCard]:
    """One card per meeting referenced by META."""
    cards: list[SourceCard] = []
    for m in meetings[:max_sources]:
        cards.append(SourceCard(
            meeting_id=m.meeting_id,
            meeting_title=m.title,
            meeting_date=m.date,
            source_type="metadata",
        ))
    return cards


def merge_sources(
    *parts: list[SourceCard],
    max_sources: int = _DEFAULT_MAX_SOURCES,
) -> list[SourceCard]:
    """Merge multiple source-card lists, deduping by meeting_id (first wins).

    Used by HYBRID (insights + chunks) and COMPARE (multiple meetings, each
    contributing its own card). Source_type from the FIRST occurrence wins —
    callers should pass the more "evidence-heavy" list first (e.g. chunks
    before insights).
    """
    seen: set[UUID] = set()
    merged: list[SourceCard] = []
    for cards in parts:
        for c in cards:
            if c.meeting_id in seen:
                continue
            seen.add(c.meeting_id)
            merged.append(c)
            if len(merged) >= max_sources:
                return merged
    return merged
