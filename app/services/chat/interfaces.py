"""Protocol classes + data types for the chat layer.

Two reasons these exist:
  1. Handlers depend on Protocols, not concrete classes — tests inject fakes.
  2. The SessionStore Protocol lets us swap in-memory ↔ Redis later without
     touching handler code.

NO IMPLEMENTATIONS in this module — only types and Protocols. Concrete
implementations live in their own files (session.py, repos, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Turn:
    """One user or assistant turn in a chat session."""
    role: str           # 'user' | 'assistant'
    content: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ChatScope:
    """The set of meetings the bot is currently allowed to search.

    access_filter narrows further:
      'attended' → only rows where meeting_participants.role IN ('organizer','attendee')
      'granted'  → only rows where role = 'granted'
      'all'      → no extra restriction (still RBAC-bounded by membership)
    """
    meeting_ids: list[UUID]
    access_filter: str = "all"


@dataclass
class PendingDisambiguation:
    """Stored when the orchestrator surfaced a speaker-disambiguation prompt
    in the previous turn. On the next turn, the orchestrator checks whether
    the user's reply matches one of the candidates — if so, it replays the
    original query with the speaker resolved (skipping the router entirely)."""
    speaker_name: str
    candidates: list[dict]                  # [{name, email, graph_id}]
    original_query: str
    original_decision: "RouterDecision"     # cached; replayed on resolution


@dataclass
class SessionState:
    """Per-session state held between turns within a single browser session.

    Cross-refresh persistence is OUT for v1 — when the tab closes, this is gone.
    """
    session_id: str
    scope: ChatScope = field(default_factory=lambda: ChatScope(meeting_ids=[]))
    last_referenced_meeting_id: UUID | None = None
    last_intent: str | None = None
    turns: list[Turn] = field(default_factory=list)
    pending_disambiguation: PendingDisambiguation | None = None


@dataclass
class SpeakerCandidate:
    """One candidate when a speaker name resolves to multiple participants."""
    name: str
    email: str | None
    graph_id: str


@dataclass
class RouterDecision:
    """Output of the LLM router. Mirrors the JSON contract in the plan."""
    route: str
    filters: dict[str, Any]
    scope_intent: dict[str, Any]
    out_of_window: bool                 # query references a date older than RBAC_WITHIN_DAYS
    search_query: str


@dataclass
class RetrievedChunk:
    """One chunk from hybrid search + round-robin diversification."""
    chunk_id: UUID
    meeting_id: UUID
    meeting_title: str
    meeting_date: datetime | None
    speakers: list[str]
    chunk_text: list[dict]   # JSONB array of utterances {n, sn, t, st, et}
    start_ms: int
    end_ms: int
    score: float


@dataclass
class MeetingMeta:
    """Meeting + its participants (used by META and COMPARE handlers)."""
    meeting_id: UUID
    title: str
    date: datetime
    duration_minutes: int | None
    organizer_name: str | None
    participants: list[dict]   # {name, email, role, graph_id}
    status: str


@dataclass
class InsightsBundle:
    """All insights for one meeting, joined into a single dict-friendly shape."""
    meeting_id: UUID
    meeting_title: str
    meeting_date: datetime
    summary: str | None
    action_items: list[Any]
    key_decisions: list[Any]
    follow_ups: list[Any]


# ── Protocols ─────────────────────────────────────────────────────────────────

class LLMClient(Protocol):
    """Async LLM client. Concrete impl: AzureOpenAIClient in app.services.llm.client."""

    async def complete_text(
        self,
        deployment: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 600,
        temperature: float = 0.3,
    ) -> str: ...

    async def complete_json(
        self,
        deployment: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 400,
        temperature: float = 0.0,
    ) -> dict: ...


class MetadataRepo(Protocol):
    """Reads from meetings + meeting_participants + transcripts."""

    async def get_meetings(self, meeting_ids: list[UUID]) -> list[MeetingMeta]: ...

    async def get_participants(
        self, meeting_ids: list[UUID]
    ) -> dict[UUID, list[dict]]: ...

    async def search_by_title(
        self,
        candidate_titles: list[str],
        allowed_meeting_ids: list[UUID] | None = None,
    ) -> list[UUID]:
        """ILIKE-match meeting titles. If allowed_meeting_ids is given, restrict to that set."""
        ...

    async def get_meetings_in_date_range(
        self,
        date_from: str | None,
        date_to: str | None,
        allowed_meeting_ids: list[UUID] | None = None,
    ) -> list[UUID]:
        """Return meeting_ids whose date falls in the given range (inclusive of full date_to)."""
        ...

    async def get_authorized_meeting_ids(
        self,
        graph_id: str,
        access_filter: str = "all",
        within_days: int = 30,
        max_meetings: int = 0,
    ) -> list[UUID]:
        """RBAC scope — meetings the user can see, optionally narrowed by access role.

        access_filter:
          'attended' — role IN ('organizer','attendee')   (the user actually spoke)
          'granted'  — role = 'granted'                   (admin gave access)
          'all'      — any role

        within_days and max_meetings combine independently:
          - within_days > 0   → only meetings within the last N days
          - max_meetings > 0  → at most N most-recent meetings (LIMIT)
          - 0 disables the corresponding bound
        Both > 0 is an INTERSECTION (whichever bound hits first wins).
        Both == 0 means no recency RBAC (just the participant membership check).
        """
        ...

    async def count_authorized_meetings(
        self,
        graph_id: str,
        access_filter: str = "all",
        within_days: int = 30,
    ) -> int:
        """Total count of authorised meetings within the date window — UNBOUNDED
        by max_meetings. Used to detect whether the count-cap is actually biting.

        Same WHERE filters as get_authorized_meeting_ids, but no LIMIT clause.
        """
        ...

    async def get_user_role_per_meeting(
        self,
        graph_id: str,
        meeting_ids: list[UUID],
    ) -> dict[UUID, str]:
        """Return {meeting_id → role} for the given user.

        Role values: 'organizer' | 'attendee' | 'granted'. Meetings where the
        user is not a participant are simply absent from the dict.
        """
        ...

    async def get_user_display_name(self, graph_id: str) -> str | None:
        """Return the user's display name from `users.display_name`, or None."""
        ...


class InsightsRepo(Protocol):
    """Reads from meeting_insights and meeting_summaries."""

    async def get_insights(self, meeting_ids: list[UUID]) -> list[InsightsBundle]: ...

    async def get_summary_text(self, meeting_id: UUID) -> str | None:
        """Returns meeting_summaries.summary_text if present, else None."""
        ...


class ChunkSearcher(Protocol):
    """Hybrid (BM25 + vector) chunk search with round-robin diversification."""

    async def hybrid_search(
        self,
        query_embedding: list[float],
        query_text: str,
        meeting_ids: list[UUID],
        filters: dict[str, Any],
        top_k: int = 10,
    ) -> list[RetrievedChunk]: ...


class SpeakerResolver(Protocol):
    """Tenant-wide name → graph_id resolution.

    Returns:
      - empty list when no match
      - one or more candidates when matches found
    Caller decides whether to disambiguate (>1 candidate) or proceed (==1).
    """

    async def resolve(self, name: str) -> list[SpeakerCandidate]: ...


class SessionStore(Protocol):
    """Per-session state. v1 is in-memory; future Redis impl satisfies the same Protocol."""

    def get_or_create(self, session_id: str) -> SessionState: ...
    def update_scope(self, session_id: str, scope: ChatScope) -> None: ...
    def set_last_referenced_meeting(
        self, session_id: str, meeting_id: UUID | None
    ) -> None: ...
    def set_last_intent(self, session_id: str, intent: str | None) -> None: ...
    def record_turn(self, session_id: str, role: str, content: str) -> None: ...
    def get_recent_turns(self, session_id: str, n: int = 10) -> list[Turn]: ...
    def set_pending_disambiguation(
        self, session_id: str, payload: PendingDisambiguation | None,
    ) -> None: ...
