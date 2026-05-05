"""In-memory SessionStore — v1 implementation.

Per the plan, v1 keeps no cross-refresh persistence: a fresh tab gets a fresh
session, so a process-local dict is sufficient. Conforms to the SessionStore
Protocol (interfaces.py) so a Redis-backed impl can be swapped in later
without touching handler code.

Caveats
-------
- Sessions are PROCESS-local. If you scale to multiple workers behind a load
  balancer, sessions won't be sticky. Acceptable for v1 (single uvicorn worker
  in dev). Revisit when adding LB / autoscale.
- Memory grows unbounded. For v1 fine; long-running prod will want a TTL or
  LRU eviction.
"""
from __future__ import annotations

from threading import Lock
from uuid import UUID

from app.services.chat.config import SESSION_TURN_WINDOW
from app.services.chat.interfaces import (
    ChatScope,
    PendingDisambiguation,
    SessionState,
    Turn,
)

# Default rolling window for stored turns (env-overridable via CHAT_SESSION_TURN_WINDOW).
_DEFAULT_TURN_WINDOW = SESSION_TURN_WINDOW


class InMemorySessionStore:
    """Thread-safe dict-backed SessionStore."""

    def __init__(self, turn_window: int = _DEFAULT_TURN_WINDOW) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = Lock()
        self._turn_window = turn_window

    def get_or_create(self, session_id: str) -> SessionState:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = SessionState(session_id=session_id)
                self._sessions[session_id] = state
            return state

    def update_scope(self, session_id: str, scope: ChatScope) -> None:
        state = self.get_or_create(session_id)
        with self._lock:
            state.scope = scope

    def set_last_referenced_meeting(
        self, session_id: str, meeting_id: UUID | None
    ) -> None:
        state = self.get_or_create(session_id)
        with self._lock:
            state.last_referenced_meeting_id = meeting_id

    def set_last_intent(self, session_id: str, intent: str | None) -> None:
        state = self.get_or_create(session_id)
        with self._lock:
            state.last_intent = intent

    def record_turn(self, session_id: str, role: str, content: str) -> None:
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        state = self.get_or_create(session_id)
        with self._lock:
            state.turns.append(Turn(role=role, content=content))
            # Drop the oldest turns once the rolling window overflows.
            if len(state.turns) > self._turn_window:
                state.turns = state.turns[-self._turn_window :]

    def get_recent_turns(self, session_id: str, n: int = 10) -> list[Turn]:
        """Return up to last `n` user+assistant pairs (so 2n total turns)."""
        state = self.get_or_create(session_id)
        with self._lock:
            return list(state.turns[-(2 * n) :])

    def set_pending_disambiguation(
        self, session_id: str, payload: PendingDisambiguation | None,
    ) -> None:
        state = self.get_or_create(session_id)
        with self._lock:
            state.pending_disambiguation = payload

    # Test/debug helpers — not part of the SessionStore Protocol.
    def _clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


# ── Process-level singleton ───────────────────────────────────────────────────

_singleton: InMemorySessionStore | None = None


def get_session_store() -> InMemorySessionStore:
    """Lazy module-level singleton. Replace with a Redis-backed store here
    when v2 needs cross-worker session continuity."""
    global _singleton
    if _singleton is None:
        _singleton = InMemorySessionStore()
    return _singleton
