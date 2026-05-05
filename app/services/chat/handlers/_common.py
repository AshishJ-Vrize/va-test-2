"""Shared types for handlers — HandlerResult.

Every handler returns the same shape so the orchestrator can build the
chat HTTP response identically regardless of route.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.services.chat.sources import SourceCard


@dataclass
class HandlerResult:
    """What every handler returns to the orchestrator.

    answer                  — user-facing string, ready to display
    sources                 — UI source cards (see app.services.chat.sources)
    referenced_meeting_ids  — meetings actually used in this answer (for the
                              session store's `last_referenced_meeting_id` —
                              "the meeting" in the next user turn)
    is_empty                — handler had no data to answer with. Orchestrator
                              uses this to fall through to a broader handler
                              (e.g. STRUCTURED_DIRECT empty → SEARCH). Decoupled
                              from `answer` so the user-facing text can change
                              without breaking the fall-through trigger.
    """
    answer: str
    sources: list[SourceCard] = field(default_factory=list)
    referenced_meeting_ids: list[UUID] = field(default_factory=list)
    is_empty: bool = False
