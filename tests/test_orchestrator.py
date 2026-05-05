"""Unit tests for handle_chat() — orchestrator wiring with all-fakes.

We don't repeat handler-internal assertions here (those live in test_handlers.py).
What this suite validates:
  - Session creation / reuse via session_id
  - RBAC scoping: unauthorised meetings get filtered out
  - Default scope = 'last meeting' on a fresh session
  - Route dispatch: each router output picks the correct handler
  - GENERAL_REFUSE short-circuits without DB calls
  - Speaker disambiguation surfaces when 2+ candidates match
  - STRUCTURED_DIRECT/LLM fallthrough to SEARCH on empty insights
  - Out-of-30-day flag passes through
  - Scope-change suggestion surfaces when extras exist
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from app.services.chat.interfaces import (
    InsightsBundle,
    MeetingMeta,
    RetrievedChunk,
    SpeakerCandidate,
)
from app.services.chat.orchestrator import OrchestratorResult, handle_chat
from app.services.chat.session import InMemorySessionStore


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, *, json_response: dict | None = None, text: str = "fake answer"):
        self._json = json_response or {}
        self._text = text
        self.json_calls: list[dict] = []
        self.text_calls: list[dict] = []

    async def complete_text(self, deployment, messages, max_tokens=600, temperature=0.3):
        self.text_calls.append({"deployment": deployment, "messages": messages})
        return self._text

    async def complete_json(self, deployment, messages, max_tokens=400, temperature=0.0):
        self.json_calls.append({"deployment": deployment, "messages": messages})
        return self._json


class FakeMetadataRepo:
    def __init__(
        self,
        *,
        meetings: list[MeetingMeta] | None = None,
        authorised_ids: list[uuid.UUID] | None = None,
        title_hits: list[uuid.UUID] | None = None,
        user_roles: dict[uuid.UUID, str] | None = None,
        user_display_name: str | None = "Test User",
        total_authorised_in_window: int | None = None,
    ):
        self._meetings = list(meetings or [])
        self._authorised = list(authorised_ids or [m.meeting_id for m in self._meetings])
        self._title_hits = list(title_hits or [])
        self._user_roles = dict(user_roles or {})
        self._user_display_name = user_display_name
        # By default the count == authorised list size (cap not biting). Tests
        # can override to simulate a tenant with more meetings than the cap allows.
        self._total_in_window = (
            total_authorised_in_window
            if total_authorised_in_window is not None
            else len(self._authorised)
        )
        self.last_authorised_call: dict[str, Any] | None = None
        self.count_calls: list[dict[str, Any]] = []

    async def get_meetings(self, meeting_ids):
        ids_set = set(meeting_ids)
        return [m for m in self._meetings if m.meeting_id in ids_set]

    async def get_participants(self, meeting_ids):
        return {}

    async def search_by_title(self, candidate_titles, allowed_meeting_ids=None):
        return list(self._title_hits)

    async def get_meetings_in_date_range(self, date_from, date_to, allowed_meeting_ids=None):
        return []

    async def get_authorized_meeting_ids(
        self, graph_id, access_filter="all", within_days=30, max_meetings=0,
    ):
        self.last_authorised_call = {
            "graph_id": graph_id,
            "access_filter": access_filter,
            "within_days": within_days,
            "max_meetings": max_meetings,
        }
        ids = list(self._authorised)
        if max_meetings and max_meetings > 0:
            ids = ids[:max_meetings]
        return ids

    async def count_authorized_meetings(self, graph_id, access_filter="all", within_days=30):
        self.count_calls.append({
            "graph_id": graph_id,
            "access_filter": access_filter,
            "within_days": within_days,
        })
        return self._total_in_window

    async def get_user_role_per_meeting(self, graph_id, meeting_ids):
        return {mid: r for mid, r in self._user_roles.items() if mid in set(meeting_ids)}

    async def get_user_display_name(self, graph_id):
        return self._user_display_name


class FakeInsightsRepo:
    def __init__(self, insights: list[InsightsBundle] | None = None):
        self._insights = list(insights or [])

    async def get_insights(self, meeting_ids):
        ids_set = set(meeting_ids)
        return [ib for ib in self._insights if ib.meeting_id in ids_set]

    async def get_summary_text(self, meeting_id):
        return None


class FakeChunkSearcher:
    def __init__(self, chunks: list[RetrievedChunk] | None = None):
        self._chunks = list(chunks or [])

    async def hybrid_search(self, query_embedding, query_text, meeting_ids, filters, top_k=10):
        ids_set = set(meeting_ids)
        return [c for c in self._chunks if c.meeting_id in ids_set][:top_k]


class FakeSpeakerResolver:
    def __init__(self, candidates: list[SpeakerCandidate] | None = None):
        self._candidates = list(candidates or [])

    async def resolve(self, name):
        return list(self._candidates)


async def fake_embed(text: str) -> list[float]:
    return [0.1] * 8


# ── Fixture builders ──────────────────────────────────────────────────────────

def _meeting(title="M1", offset_days=0) -> MeetingMeta:
    return MeetingMeta(
        meeting_id=uuid.uuid4(),
        title=title,
        date=datetime(2026, 5, 4, 10, 0),
        duration_minutes=30,
        organizer_name="Org",
        participants=[],
        status="ready",
    )


def _make_deps(*, router_response: dict, **overrides):
    """Common dependency bundle for orchestrator tests."""
    return {
        "llm": overrides.get("llm") or FakeLLM(json_response=router_response,
                                                text=overrides.get("answer_text", "fake answer")),
        "metadata_repo": overrides.get("metadata_repo") or FakeMetadataRepo(),
        "insights_repo": overrides.get("insights_repo") or FakeInsightsRepo(),
        "chunk_searcher": overrides.get("chunk_searcher") or FakeChunkSearcher(),
        "speaker_resolver": overrides.get("speaker_resolver") or FakeSpeakerResolver(),
        "session_store": overrides.get("session_store") or InMemorySessionStore(),
        "embed": overrides.get("embed") or fake_embed,
    }


# ── Session / RBAC ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_call_generates_session_id():
    deps = _make_deps(router_response={"route": "GENERAL_REFUSE", "filters": {}, "search_query": "x"})
    result = await handle_chat(
        query="capital of France?",
        request_meeting_ids=None, access_filter="all",
        session_id=None,
        current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.session_id is not None


@pytest.mark.asyncio
async def test_session_id_reused_when_provided():
    sid = uuid.uuid4()
    deps = _make_deps(router_response={"route": "GENERAL_REFUSE", "filters": {}, "search_query": "x"})
    result = await handle_chat(
        query="x", request_meeting_ids=None, access_filter="all",
        session_id=sid, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.session_id == sid


@pytest.mark.asyncio
async def test_default_scope_is_all_authorised_meetings():
    """With no explicit selection, default to the FULL authorised scope so
    questions like 'action items this month' aren't artificially narrowed
    to just the latest meeting. Bounded by RBAC_MAX_MEETINGS upstream."""
    m1, m2 = _meeting("Latest"), _meeting("Older")
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "x"},
        metadata_repo=FakeMetadataRepo(meetings=[m1, m2], authorised_ids=[m1.meeting_id, m2.meeting_id]),
    )
    await handle_chat(
        query="list meetings", request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    state = deps["session_store"]._sessions
    sole_state = next(iter(state.values()))
    assert sole_state.scope.meeting_ids == [m1.meeting_id, m2.meeting_id]


@pytest.mark.asyncio
async def test_unauthorised_request_meeting_ids_filtered_out():
    authorised = _meeting("Authorised")
    unauthorised = uuid.uuid4()
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "x"},
        metadata_repo=FakeMetadataRepo(
            meetings=[authorised], authorised_ids=[authorised.meeting_id],
        ),
    )
    await handle_chat(
        query="x",
        request_meeting_ids=[authorised.meeting_id, unauthorised],
        access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    state = next(iter(deps["session_store"]._sessions.values()))
    assert state.scope.meeting_ids == [authorised.meeting_id]


@pytest.mark.asyncio
async def test_access_filter_passed_to_repo():
    deps = _make_deps(router_response={"route": "GENERAL_REFUSE", "filters": {}, "search_query": "x"})
    await handle_chat(
        query="x", request_meeting_ids=None, access_filter="attended",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert deps["metadata_repo"].last_authorised_call == {
        "graph_id": "g1", "access_filter": "attended", "within_days": 30, "max_meetings": 30,
    }


# ── Route dispatch ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_general_refuse_short_circuits():
    """Returns the canned refusal without consulting the DB or running an answer LLM."""
    deps = _make_deps(router_response={"route": "GENERAL_REFUSE", "filters": {}, "search_query": "x"})
    result = await handle_chat(
        query="capital of France", request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.route == "GENERAL_REFUSE"
    assert "meeting assistant" in result.answer.lower()
    # complete_json was called for the router; complete_text NOT called for an answer.
    assert len(deps["llm"].json_calls) == 1
    assert deps["llm"].text_calls == []


@pytest.mark.asyncio
async def test_clarify_route_dispatches_to_handle_clarify():
    """Router classifies an ambiguous fragment as CLARIFY → orchestrator runs
    the clarify handler (LLM-only, no DB), which produces a follow-up question."""
    deps = _make_deps(
        router_response={"route": "CLARIFY", "filters": {}, "search_query": "yes"},
        answer_text="Did you mean to confirm the scope change?",
    )
    result = await handle_chat(
        query="yes include them",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.route == "CLARIFY"
    assert result.answer == "Did you mean to confirm the scope change?"
    # Should not consult DB for CLARIFY — text LLM should be the only call.
    # (Router json_call happens; answer text_call happens for clarify; that's it.)
    assert len(deps["llm"].text_calls) == 1


@pytest.mark.asyncio
async def test_general_gk_uses_text_llm():
    deps = _make_deps(
        router_response={"route": "GENERAL_GK", "filters": {}, "search_query": "x"},
        answer_text="Best practice answer.",
    )
    result = await handle_chat(
        query="how can we run better standups",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.route == "GENERAL_GK"
    assert result.answer == "Best practice answer."


@pytest.mark.asyncio
async def test_meta_route_dispatches():
    m = _meeting("M1")
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "list meetings"},
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        answer_text="One meeting today.",
    )
    result = await handle_chat(
        query="list my meetings", request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.route == "META"
    assert result.answer == "One meeting today."


@pytest.mark.asyncio
async def test_search_route_dispatches():
    m = _meeting()
    chunk = RetrievedChunk(
        chunk_id=uuid.uuid4(), meeting_id=m.meeting_id, meeting_title="M1",
        meeting_date=m.date, speakers=["Ashish"],
        chunk_text=[{"n": "Ashish Jaiswal", "sn": "Ashish", "t": "test"}],
        start_ms=0, end_ms=1000, score=0.9,
    )
    deps = _make_deps(
        router_response={"route": "SEARCH", "filters": {}, "search_query": "test"},
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        chunk_searcher=FakeChunkSearcher(chunks=[chunk]),
        answer_text="Found something.",
    )
    result = await handle_chat(
        query="what was said", request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.route == "SEARCH"
    assert len(result.sources) == 1


@pytest.mark.asyncio
async def test_structured_direct_falls_through_to_search_when_no_insights():
    m = _meeting()
    chunk = RetrievedChunk(
        chunk_id=uuid.uuid4(), meeting_id=m.meeting_id, meeting_title="M1",
        meeting_date=m.date, speakers=["A"], chunk_text=[{"n": "A", "sn": "A", "t": "x"}],
        start_ms=0, end_ms=1000, score=0.9,
    )
    deps = _make_deps(
        router_response={"route": "STRUCTURED_DIRECT", "filters": {}, "search_query": "what action items"},
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        insights_repo=FakeInsightsRepo([]),  # NO insights
        chunk_searcher=FakeChunkSearcher(chunks=[chunk]),
        answer_text="Search result content",
    )
    result = await handle_chat(
        query="what are the action items",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # Falls through: route changed to SEARCH for the response.
    assert result.route == "SEARCH"
    assert result.answer == "Search result content"


# ── Speaker disambiguation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disambiguation_surfaces_candidates():
    """Two participants named 'Ashish' → bot asks user to pick by email."""
    m = _meeting()
    cands = [
        SpeakerCandidate(name="Ashish Jaiswal", email="aj@x.com", graph_id="g1"),
        SpeakerCandidate(name="Ashish Kumar", email="ak@x.com", graph_id="g2"),
    ]
    deps = _make_deps(
        router_response={
            "route": "SEARCH",
            "filters": {"speaker_name": "Ashish"},
            "search_query": "what did Ashish say",
        },
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        speaker_resolver=FakeSpeakerResolver(candidates=cands),
    )
    result = await handle_chat(
        query="what did Ashish say", request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.route == "DISAMBIGUATION"
    assert "aj@x.com" in result.answer
    assert "ak@x.com" in result.answer
    assert result.speaker_disambiguation is not None
    assert len(result.speaker_disambiguation) == 2


# ── Out-of-30-day window ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_out_of_30d_flag_set_for_old_dates():
    """The router's out_of_30 flag is now overridden server-side with deterministic
    math (see router._is_out_of_30_days). Old dates in filters → flag True."""
    m = _meeting()
    deps = _make_deps(
        router_response={
            "route": "META",
            "filters": {"date_from": "2020-01-01", "date_to": "2020-01-31"},  # very old
            "search_query": "x",
            "out_of_window": False,   # LLM said no — override should still mark True
        },
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
    )
    result = await handle_chat(
        query="meetings in January 2020",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.out_of_window is True


@pytest.mark.asyncio
async def test_out_of_30d_flag_overridden_false_for_recent_dates():
    """LLM flagged the date as out-of-30 but it's actually within range — override clears."""
    m = _meeting()
    today_iso = datetime.utcnow().date().isoformat()
    deps = _make_deps(
        router_response={
            "route": "META",
            "filters": {"date_from": today_iso, "date_to": today_iso},
            "search_query": "x",
            "out_of_window": True,    # LLM lied
        },
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
    )
    result = await handle_chat(
        query="today's meeting",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.out_of_window is False


# ── Scope-change suggestion ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scope_change_suggestion_surfaces_extras():
    """Title query matches a meeting outside selection → surface banner."""
    selected = _meeting("Selected")
    extra = _meeting("Outside-Selection")
    deps = _make_deps(
        router_response={
            "route": "META",
            "filters": {"meeting_titles": ["Outside"]},
            "search_query": "x",
        },
        metadata_repo=FakeMetadataRepo(
            meetings=[selected, extra],
            authorised_ids=[selected.meeting_id, extra.meeting_id],
            title_hits=[extra.meeting_id],   # title query matches the extra one
        ),
    )
    result = await handle_chat(
        query="what was discussed in the Outside meeting",
        request_meeting_ids=[selected.meeting_id],
        access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert result.scope_change is not None
    assert extra.meeting_id in result.scope_change.new_meeting_ids


# ── Session: turns recorded ───────────────────────────────────────────────────

# ── User-context block ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_context_block_reaches_llm_with_role():
    """The orchestrator should compose a USER CONTEXT block describing the
    user's display name and their role per meeting in scope, then attach it
    to the system prompt of the answer LLM call."""
    m = _meeting("Test Meeting")
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "x"},
        metadata_repo=FakeMetadataRepo(
            meetings=[m],
            authorised_ids=[m.meeting_id],
            user_roles={m.meeting_id: "granted"},
            user_display_name="Ashish Jaiswal",
        ),
    )
    await handle_chat(
        query="did I attend this meeting",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # The answer-LLM (text) call should have a system prompt containing the USER CONTEXT.
    assert deps["llm"].text_calls, "expected an answer-LLM call"
    system_msg = deps["llm"].text_calls[-1]["messages"][0]["content"]
    assert "USER CONTEXT" in system_msg
    assert "Ashish Jaiswal" in system_msg
    assert "granted" in system_msg.lower()


@pytest.mark.asyncio
async def test_session_turns_recorded():
    """User and assistant turns both stored in the session for next-turn history."""
    m = _meeting()
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "x"},
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        answer_text="Answer text.",
    )
    sid = uuid.uuid4()
    await handle_chat(
        query="my question",
        request_meeting_ids=None, access_filter="all",
        session_id=sid, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    state = deps["session_store"].get_or_create(str(sid))
    assert [t.role for t in state.turns] == ["user", "assistant"]
    assert state.turns[0].content == "my question"
    assert state.turns[1].content == "Answer text."


# ── Disambiguation continuity + CLARIFY loop guard ───────────────────────────

@pytest.mark.asyncio
async def test_disambiguation_reply_replays_original_query():
    """Turn 1: user asks 'action items of ashish' → 2 candidates → bot prompts.
    Turn 2: user replies 'Ashish Jaiswal' → orchestrator must replay the
    ORIGINAL query with that graph_id, NOT route 'Ashish Jaiswal' to CLARIFY."""
    cands = [
        SpeakerCandidate(name="Ashish Jaiswal", email="aj@x.com", graph_id="gid-aj"),
        SpeakerCandidate(name="Ashish Choudhary", email="ac@x.com", graph_id="gid-ac"),
    ]
    m = _meeting()
    deps = _make_deps(
        router_response={
            "route": "STRUCTURED_DIRECT",
            "filters": {"speaker_name": "Ashish", "structured_intent": "list_actions"},
            "search_query": "action items of ashish",
        },
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        speaker_resolver=FakeSpeakerResolver(candidates=cands),
    )

    sid = uuid.uuid4()

    # Turn 1: surfaces disambiguation.
    turn1 = await handle_chat(
        query="action items of ashish",
        request_meeting_ids=None, access_filter="all",
        session_id=sid, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert turn1.route == "DISAMBIGUATION"
    # Pending state must be stored so turn 2 can use it.
    state = deps["session_store"].get_or_create(str(sid))
    assert state.pending_disambiguation is not None
    assert state.pending_disambiguation.original_query == "action items of ashish"

    # Capture how many json (router) calls happened so far.
    json_calls_before_turn2 = len(deps["llm"].json_calls)

    # Turn 2: user picks "Ashish Jaiswal".
    turn2 = await handle_chat(
        query="Ashish Jaiswal",
        request_meeting_ids=None, access_filter="all",
        session_id=sid, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # Router MUST be skipped on turn 2 — pending replay handles it.
    assert len(deps["llm"].json_calls) == json_calls_before_turn2, \
        "router should be bypassed when disambiguation reply matches"
    # The replay must NOT loop back into disambiguation or CLARIFY.
    # (Exact final route may be STRUCTURED_DIRECT or its SEARCH fall-through
    # when no insights exist in the test fixture — both are valid.)
    assert turn2.route not in {"CLARIFY", "DISAMBIGUATION"}
    # Pending must be cleared after consumption.
    state = deps["session_store"].get_or_create(str(sid))
    assert state.pending_disambiguation is None


@pytest.mark.asyncio
async def test_disambiguation_no_match_clears_pending_and_runs_router():
    """If the user's disambiguation reply doesn't match any candidate, the
    pending state is dropped and the reply is treated as a fresh query."""
    cands = [
        SpeakerCandidate(name="Ashish Jaiswal", email="aj@x.com", graph_id="gid-aj"),
        SpeakerCandidate(name="Ashish Choudhary", email="ac@x.com", graph_id="gid-ac"),
    ]
    m = _meeting()
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "list meetings"},
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        speaker_resolver=FakeSpeakerResolver(candidates=cands),
    )
    sid = uuid.uuid4()

    # Pre-seed a pending disambiguation as if turn 1 had surfaced it.
    from app.services.chat.interfaces import PendingDisambiguation, RouterDecision
    deps["session_store"].set_pending_disambiguation(str(sid), PendingDisambiguation(
        speaker_name="Ashish",
        candidates=[{"name": c.name, "email": c.email, "graph_id": c.graph_id} for c in cands],
        original_query="action items of ashish",
        original_decision=RouterDecision(
            route="STRUCTURED_DIRECT", filters={}, scope_intent={"needs_change": False, "reason": ""},
            out_of_window=False, search_query="x",
        ),
    ))

    # User typed something that doesn't match either candidate.
    result = await handle_chat(
        query="list my meetings",
        request_meeting_ids=None, access_filter="all",
        session_id=sid, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # Router must run (pending was cleared, query routed normally).
    assert len(deps["llm"].json_calls) == 1
    assert result.route == "META"
    # Pending state must be cleared.
    state = deps["session_store"].get_or_create(str(sid))
    assert state.pending_disambiguation is None


@pytest.mark.asyncio
async def test_clarify_loop_guard_forces_progress_on_consecutive_clarify():
    """If the previous turn was already CLARIFY and the router wants CLARIFY
    again, the orchestrator must force STRUCTURED_LLM instead — preventing
    the 'all' / 'yes' / 'this month' infinite-loop."""
    m = _meeting()
    deps = _make_deps(
        router_response={"route": "CLARIFY", "filters": {}, "search_query": "all"},
        metadata_repo=FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id]),
        answer_text="Forced answer.",
    )
    sid = uuid.uuid4()
    # Pretend the previous turn was CLARIFY.
    deps["session_store"].set_last_intent(str(sid), "CLARIFY")

    result = await handle_chat(
        query="all",
        request_meeting_ids=None, access_filter="all",
        session_id=sid, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # Must NOT be CLARIFY (the guard's whole purpose). May fall through to
    # SEARCH if STRUCTURED_LLM has no insights — both are acceptable; the key
    # invariant is that the user gets a real route, not another question.
    assert result.route != "CLARIFY"
    # last_intent must reflect the actual route, not CLARIFY again.
    state = deps["session_store"].get_or_create(str(sid))
    assert state.last_intent != "CLARIFY"


# ── Auto-expand to extras when selection has no match ────────────────────────

@pytest.mark.asyncio
async def test_auto_expand_to_extras_when_selection_has_no_match():
    """User asks 'did I attend April 24', default selection = May 4 meeting.
    The April 24 meeting exists in authorised scope but outside selection
    (matched=[], extras=[April 24]). Orchestrator must auto-expand instead
    of returning 'couldn't find'."""
    selected = _meeting("May 4 VA Demo")
    other = _meeting("April 24 Sync")
    other_id = other.meeting_id

    # title_hits proxies as 'date_hits' via narrow_within_scope's path. The
    # FakeMetadataRepo returns this for both title and date searches.
    repo = FakeMetadataRepo(
        meetings=[selected, other],
        authorised_ids=[selected.meeting_id, other.meeting_id],
        title_hits=[other_id],   # narrow_within_scope finds this as a date hit too
    )

    # Override get_meetings_in_date_range so the date filter returns [other_id].
    async def _date_range(date_from, date_to, allowed_meeting_ids=None):
        return [other_id]
    repo.get_meetings_in_date_range = _date_range  # type: ignore[method-assign]

    deps = _make_deps(
        router_response={
            "route": "META",
            "filters": {"date_from": "2026-04-24", "date_to": "2026-04-24"},
            "search_query": "did I attend",
        },
        metadata_repo=repo,
        answer_text="Yes — you attended the April 24 Sync.",
    )
    result = await handle_chat(
        query="did I attend a meeting on 24th April",
        request_meeting_ids=[selected.meeting_id],   # only May 4 selected
        access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # The handler should have answered using the April 24 meeting, not refused.
    assert "couldn't find" not in result.answer.lower()
    assert result.answer == "Yes — you attended the April 24 Sync."
    # Sources should include the auto-expanded meeting.
    assert any(s.meeting_id == other_id for s in result.sources)
    # The scope_change banner should disclose the expansion (not ask about it).
    assert result.scope_change is not None
    assert "didn't include" in result.scope_change.reason.lower() or \
           "answered using" in result.scope_change.reason.lower()


# ── Empty-scope SEARCH fallback ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_structured_direct_empty_scope_falls_through_to_search_with_full_rbac():
    """User asks about a date with no matching meetings → narrow returns
    matched=[] AND extras=[]. Orchestrator must NOT short-circuit; instead it
    falls through to SEARCH against the FULL authorised scope so transcript
    chunks across the user's meetings can still surface relevant content."""
    m1, m2 = _meeting("Daily Standup"), _meeting("All-hands")
    chunk = RetrievedChunk(
        chunk_id=uuid.uuid4(), meeting_id=m1.meeting_id, meeting_title="Daily Standup",
        meeting_date=m1.date, speakers=["A"],
        chunk_text=[{"n": "A", "sn": "A", "t": "we discussed pricing"}],
        start_ms=0, end_ms=1000, score=0.9,
    )
    repo = FakeMetadataRepo(
        meetings=[m1, m2],
        authorised_ids=[m1.meeting_id, m2.meeting_id],
    )
    # Date filter that matches nothing in the date-range repo — narrow returns empty.
    async def _date_range(date_from, date_to, allowed_meeting_ids=None):
        return []
    repo.get_meetings_in_date_range = _date_range  # type: ignore[method-assign]

    deps = _make_deps(
        router_response={
            "route": "STRUCTURED_DIRECT",
            "filters": {"date_from": "2026-04-30", "date_to": "2026-04-30",
                        "structured_intent": "digest"},
            "search_query": "summarise yesterday's standup",
        },
        metadata_repo=repo,
        insights_repo=FakeInsightsRepo([]),
        chunk_searcher=FakeChunkSearcher(chunks=[chunk]),
        answer_text="Found something across all meetings.",
    )
    result = await handle_chat(
        query="summarise yesterday's standup",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # Final route must be SEARCH (fall-through happened) and produce an answer
    # that came from the search path, not the canned no-results template.
    assert result.route == "SEARCH"
    assert "couldn't find" not in result.answer.lower()


@pytest.mark.asyncio
async def test_no_results_strings_do_not_leak_internal_terms():
    """User-facing 'couldn't find' messages must never expose backend
    vocabulary like 'meeting insights' / 'meeting transcripts' / 'meeting records'."""
    from app.services.chat.handlers.meta import _NO_RESULTS as META_NRT
    from app.services.chat.handlers.structured_direct import _NO_RESULTS as SD_NRT
    from app.services.chat.handlers.structured_llm import _NO_RESULTS as SL_NRT
    from app.services.chat.handlers.search import _NO_RESULTS as SE_NRT

    leaks = ("meeting insights", "meeting transcripts", "meeting records")
    for s in (META_NRT, SD_NRT, SL_NRT, SE_NRT):
        for leak in leaks:
            assert leak not in s.lower(), f"leaked term {leak!r} in {s!r}"


# ── Cap-aware no-match short-circuit ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_date_filter_no_match_with_cap_biting_returns_cap_aware_message(monkeypatch):
    """User asks 'did I attend a meeting on April 24', cap is biting, no match
    in visible scope → bot must mention the cap, not say 'couldn't find'."""
    import app.services.chat.orchestrator as orch
    monkeypatch.setattr(orch, "RBAC_MAX_MEETINGS", 2)
    # Two visible meetings, both on different dates than the filter. Tenant has 9 total.
    m1, m2 = _meeting("Recent A"), _meeting("Recent B")
    repo = FakeMetadataRepo(
        meetings=[m1, m2],
        authorised_ids=[m1.meeting_id, m2.meeting_id],
        total_authorised_in_window=9,
        title_hits=[],
    )
    deps = _make_deps(
        router_response={
            "route": "META",
            "filters": {"date_from": "2026-04-24", "date_to": "2026-04-24"},
            "search_query": "did I attend",
        },
        metadata_repo=repo,
    )
    result = await handle_chat(
        query="did I attend any meeting on 24th April",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    # Should NOT short-circuit to the canned 'couldn't find' message.
    assert "couldn't find a meeting on 2026-04-24" in result.answer
    assert "9" in result.answer            # total
    assert "2 most-recent" in result.answer  # visible
    assert "CHAT_RBAC_MAX_MEETINGS" in result.answer
    assert result.rbac_scope_info is not None
    assert result.rbac_scope_info.capped is True


@pytest.mark.asyncio
async def test_date_filter_no_match_when_cap_NOT_biting_uses_normal_path():
    """Same query but cap isn't biting → normal META 'couldn't find' message
    (don't lie about a cap that isn't doing anything)."""
    m1 = _meeting("Recent A")
    repo = FakeMetadataRepo(
        meetings=[m1],
        authorised_ids=[m1.meeting_id],
        total_authorised_in_window=1,   # cap not biting
    )
    deps = _make_deps(
        router_response={
            "route": "META",
            "filters": {"date_from": "2026-04-24", "date_to": "2026-04-24"},
            "search_query": "did I attend",
        },
        metadata_repo=repo,
    )
    result = await handle_chat(
        query="did I attend a meeting on April 24",
        request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert "CHAT_RBAC_MAX_MEETINGS" not in result.answer
    assert "couldn't find" in result.answer.lower()


# ── RBAC scope-info awareness (count cap visibility) ──────────────────────────

@pytest.mark.asyncio
async def test_rbac_scope_info_when_cap_not_biting_skips_count_query():
    """If visible < RBAC_MAX_MEETINGS, the unbounded count query is skipped
    entirely — cap is provably not in play."""
    m = _meeting()
    repo = FakeMetadataRepo(meetings=[m], authorised_ids=[m.meeting_id])
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "x"},
        metadata_repo=repo,
    )
    result = await handle_chat(
        query="x", request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert repo.count_calls == [], "unbounded count must not run when cap isn't suspect"
    assert result.rbac_scope_info is not None
    assert result.rbac_scope_info.capped is False
    assert result.rbac_scope_info.visible == 1


@pytest.mark.asyncio
async def test_rbac_scope_info_when_cap_biting_surfaces_capped(monkeypatch):
    """visible == RBAC_MAX_MEETINGS AND total > visible → capped True, banner ready."""
    import app.services.chat.orchestrator as orch
    monkeypatch.setattr(orch, "RBAC_MAX_MEETINGS", 2)
    # Two meetings visible (== cap), but tenant has 5 in the window total.
    m1, m2 = _meeting("M1"), _meeting("M2")
    repo = FakeMetadataRepo(
        meetings=[m1, m2],
        authorised_ids=[m1.meeting_id, m2.meeting_id],
        total_authorised_in_window=5,
    )
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "x"},
        metadata_repo=repo,
    )
    result = await handle_chat(
        query="x", request_meeting_ids=None, access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert len(repo.count_calls) == 1, "should query unbounded count when cap is suspect"
    assert result.rbac_scope_info is not None
    assert result.rbac_scope_info.capped is True
    assert result.rbac_scope_info.visible == 2
    assert result.rbac_scope_info.total == 5


@pytest.mark.asyncio
async def test_capped_state_injects_awareness_into_user_context(monkeypatch):
    """When the cap is biting, the LLM should see a 'SEARCHABLE SCOPE LIMIT'
    line in the USER CONTEXT block of the system prompt."""
    import app.services.chat.orchestrator as orch
    monkeypatch.setattr(orch, "RBAC_MAX_MEETINGS", 2)
    m1, m2 = _meeting("M1"), _meeting("M2")
    repo = FakeMetadataRepo(
        meetings=[m1, m2],
        authorised_ids=[m1.meeting_id, m2.meeting_id],
        total_authorised_in_window=87,
        user_display_name="Ashish",
    )
    deps = _make_deps(
        router_response={"route": "META", "filters": {}, "search_query": "x"},
        metadata_repo=repo,
        answer_text="A",
    )
    await handle_chat(
        query="list ALL my meetings", request_meeting_ids=[m1.meeting_id, m2.meeting_id],
        access_filter="all",
        session_id=None, current_user_graph_id="g1", db=None,  # type: ignore[arg-type]
        **deps,
    )
    assert deps["llm"].text_calls, "expected an answer-LLM call"
    system_msg = deps["llm"].text_calls[-1]["messages"][0]["content"]
    assert "SEARCHABLE SCOPE LIMIT" in system_msg
    assert "87" in system_msg                # total
    assert "2 most-recent" in system_msg     # visible
