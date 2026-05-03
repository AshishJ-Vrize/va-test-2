"""Speaker resolver — map VTT speaker labels to participants and stable graph IDs.

Run once per meeting before chunking. Builds a mapping from each unique VTT
speaker label to a resolved identity:

    {n: full_name, sn: short_name, graph_id: str|None, ambiguous: bool}

Resolution tiers — try in order, accept the first tier that yields exactly
one match. If a tier yields multiple matches, mark ambiguous and stop.
1. Exact match (case-insensitive, trimmed, parentheticals stripped)
2. Email local-part match (when VTT label is an email-like form)
3. First-name unique match (VTT first token vs participant first token)
4. Levenshtein <= 2 on full name (for minor spelling/diacritic variation)

Scope is this meeting's participants only — VTT lines should map to people
who were in the room. Cross-meeting name resolution at query time is a
separate function in the chat router.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tenant.models import MeetingParticipant

log = logging.getLogger(__name__)

_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")
_WS_RE = re.compile(r"\s+")
_LEV_THRESHOLD = 2


@dataclass
class ResolvedSpeaker:
    """Resolved identity for one VTT speaker label.

    n          : Full display name — resolved participant name when matched,
                 else the cleaned VTT label.
    sn         : Short name (first whitespace token of n).
    graph_id   : Microsoft Graph user ID when uniquely matched, else None.
                 None for external attendees and for ambiguous matches.
    ambiguous  : True when 2+ participants matched the same VTT label —
                 we cannot pick from the name alone (e.g. two Ashishes in
                 the participant list both labeled "Ashish" in the VTT).
    """
    n: str
    sn: str
    graph_id: str | None
    ambiguous: bool


def _clean(label: str) -> str:
    """Strip parentheticals, trim, collapse whitespace. Keeps original casing."""
    s = _PAREN_RE.sub(" ", label).strip()
    return _WS_RE.sub(" ", s)


def _first_token(s: str) -> str:
    parts = s.split()
    return parts[0] if parts else s


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance — small alphabets only, fine for names."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * lb
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _unresolved(cleaned: str, ambiguous: bool = False) -> ResolvedSpeaker:
    """Build a ResolvedSpeaker for the no-match-or-ambiguous case."""
    return ResolvedSpeaker(
        n=cleaned or "Unknown",
        sn=_first_token(cleaned) or "Unknown",
        graph_id=None,
        ambiguous=ambiguous,
    )


def _from_participant(p: MeetingParticipant, fallback_name: str) -> ResolvedSpeaker:
    """Build a ResolvedSpeaker from a uniquely matched participant."""
    name = (p.participant_name or "").strip() or fallback_name
    return ResolvedSpeaker(
        n=name,
        sn=_first_token(name),
        graph_id=p.participant_graph_id,
        ambiguous=False,
    )


def _resolve_one(
    vtt_label: str,
    participants: list[MeetingParticipant],
) -> ResolvedSpeaker:
    """Run the four resolution tiers against a single VTT label."""
    cleaned = _clean(vtt_label)
    if not cleaned:
        return _unresolved("Unknown")

    cleaned_lower = cleaned.lower()

    # Tier 1: exact case-insensitive name match.
    matches = [
        p for p in participants
        if (p.participant_name or "").strip().lower() == cleaned_lower
    ]
    if len(matches) == 1:
        return _from_participant(matches[0], cleaned)
    if len(matches) > 1:
        return _unresolved(cleaned, ambiguous=True)

    # Tier 2: email local-part (VTT sometimes shows raw addresses for guests).
    if "@" in cleaned:
        local = cleaned.split("@", 1)[0].lower()
        matches = [
            p for p in participants
            if p.participant_email
            and p.participant_email.split("@", 1)[0].lower() == local
        ]
        if len(matches) == 1:
            return _from_participant(matches[0], cleaned)
        if len(matches) > 1:
            return _unresolved(cleaned, ambiguous=True)

    # Tier 3: unique first-name match.
    cleaned_first = _first_token(cleaned).lower()
    if cleaned_first:
        matches = [
            p for p in participants
            if _first_token((p.participant_name or "")).lower() == cleaned_first
        ]
        if len(matches) == 1:
            return _from_participant(matches[0], cleaned)
        if len(matches) > 1:
            return _unresolved(cleaned, ambiguous=True)

    # Tier 4: near-match on full name (typos, diacritics).
    near = [
        p for p in participants
        if p.participant_name
        and _levenshtein(cleaned_lower, p.participant_name.lower()) <= _LEV_THRESHOLD
    ]
    if len(near) == 1:
        return _from_participant(near[0], cleaned)
    if len(near) > 1:
        return _unresolved(cleaned, ambiguous=True)

    # External attendee not in participant list.
    return _unresolved(cleaned)


async def build_speaker_resolution(
    meeting_id: uuid.UUID,
    vtt_speakers: list[str],
    db: AsyncSession,
) -> dict[str, ResolvedSpeaker]:
    """Build the VTT label → ResolvedSpeaker mapping for one meeting.

    Args:
        meeting_id: target meeting
        vtt_speakers: speaker labels exactly as they appear in the VTT
                      (caller should pass unique labels)
        db: tenant DB session

    Returns:
        Dict keyed by the original VTT label (preserves original casing).
        Every input label gets an entry, including unresolved ones.
    """
    result = await db.execute(
        select(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting_id)
    )
    participants = list(result.scalars().all())

    resolution: dict[str, ResolvedSpeaker] = {}
    ambiguous_count = 0
    unresolved_count = 0

    for vtt_label in vtt_speakers:
        if vtt_label in resolution:
            continue
        rs = _resolve_one(vtt_label, participants)
        resolution[vtt_label] = rs
        if rs.ambiguous:
            ambiguous_count += 1
        elif rs.graph_id is None:
            unresolved_count += 1

    if ambiguous_count or unresolved_count:
        log.info(
            "speaker_resolver: meeting %s — %d ambiguous, %d unresolved out of %d labels",
            meeting_id, ambiguous_count, unresolved_count, len(vtt_speakers),
        )

    return resolution
