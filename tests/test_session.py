"""Unit tests for InMemorySessionStore.

Validates the Phase 1 Definition of Done items:
  - get_or_create returns the same state across calls for one session_id
  - scope updates persist
  - last-referenced-meeting persists
  - turn recording rolls a window (drops oldest after limit)
  - get_recent_turns respects the requested pair count
  - threading lock prevents corruption under concurrent writes (smoke check)
"""
from __future__ import annotations

import threading
import uuid

import pytest

from app.services.chat.interfaces import ChatScope
from app.services.chat.session import InMemorySessionStore


# ── Identity / state lifecycle ────────────────────────────────────────────────

def test_get_or_create_returns_same_state_for_same_id():
    store = InMemorySessionStore()
    a = store.get_or_create("s1")
    b = store.get_or_create("s1")
    assert a is b
    assert a.session_id == "s1"
    assert store._session_count() == 1


def test_get_or_create_creates_independent_states_per_id():
    store = InMemorySessionStore()
    a = store.get_or_create("s1")
    b = store.get_or_create("s2")
    assert a is not b
    assert store._session_count() == 2


# ── Scope ────────────────────────────────────────────────────────────────────

def test_update_scope_persists():
    store = InMemorySessionStore()
    mid = uuid.uuid4()
    store.update_scope("s1", ChatScope(meeting_ids=[mid], access_filter="attended"))

    state = store.get_or_create("s1")
    assert state.scope.meeting_ids == [mid]
    assert state.scope.access_filter == "attended"


def test_default_scope_is_empty():
    store = InMemorySessionStore()
    state = store.get_or_create("s1")
    assert state.scope.meeting_ids == []
    assert state.scope.access_filter == "all"


# ── Last-referenced meeting ───────────────────────────────────────────────────

def test_set_last_referenced_meeting():
    store = InMemorySessionStore()
    mid = uuid.uuid4()
    store.set_last_referenced_meeting("s1", mid)
    assert store.get_or_create("s1").last_referenced_meeting_id == mid


def test_clear_last_referenced_meeting():
    store = InMemorySessionStore()
    mid = uuid.uuid4()
    store.set_last_referenced_meeting("s1", mid)
    store.set_last_referenced_meeting("s1", None)
    assert store.get_or_create("s1").last_referenced_meeting_id is None


# ── Turn rolling window ───────────────────────────────────────────────────────

def test_record_turn_appends():
    store = InMemorySessionStore()
    store.record_turn("s1", "user", "hello")
    store.record_turn("s1", "assistant", "hi")
    state = store.get_or_create("s1")
    assert [t.role for t in state.turns] == ["user", "assistant"]
    assert state.turns[0].content == "hello"


def test_record_turn_rejects_invalid_role():
    store = InMemorySessionStore()
    with pytest.raises(ValueError):
        store.record_turn("s1", "system", "nope")


def test_turn_window_trims_oldest():
    # Use a small window for fast assertion
    store = InMemorySessionStore(turn_window=4)
    for i in range(6):
        store.record_turn("s1", "user", f"u{i}")

    state = store.get_or_create("s1")
    assert len(state.turns) == 4
    # Oldest two ('u0', 'u1') should be gone — window keeps last 4.
    assert [t.content for t in state.turns] == ["u2", "u3", "u4", "u5"]


def test_get_recent_turns_pair_count():
    store = InMemorySessionStore(turn_window=20)
    for i in range(5):
        store.record_turn("s1", "user", f"u{i}")
        store.record_turn("s1", "assistant", f"a{i}")
    # Request last 2 pairs → expect 4 turns (u3, a3, u4, a4)
    recent = store.get_recent_turns("s1", n=2)
    assert len(recent) == 4
    assert [t.content for t in recent] == ["u3", "a3", "u4", "a4"]


# ── Concurrency smoke ─────────────────────────────────────────────────────────

def test_concurrent_record_turn_does_not_corrupt():
    """Smoke check — 10 threads, 50 turns each, on a single session.

    Total appends = 500. Window = 1000 (no trimming). Final count must be 500.
    Looser assertion than full thread-safety proof, but catches obvious races.
    """
    store = InMemorySessionStore(turn_window=1000)

    def writer(tid: int):
        for i in range(50):
            store.record_turn("shared", "user", f"t{tid}-{i}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = store.get_or_create("shared")
    assert len(state.turns) == 500
