"""Unit tests for every handler — fakes for repos / LLM / chunk searcher / embedder.

Each handler is tested for:
  - happy path: returns a non-empty answer + populated sources
  - empty meeting_ids: returns the route-specific no-results message
  - empty repo result: returns the route-specific no-results message

The LLM call is faked — we don't validate the LLM's output quality, just
that the handler wires inputs/outputs correctly.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from app.services.chat.handlers.compare import handle_compare
from app.services.chat.handlers.general import handle_general_gk, handle_general_refuse
from app.services.chat.handlers.hybrid import handle_hybrid
from app.services.chat.handlers.meta import handle_meta
from app.services.chat.handlers.search import handle_search
from app.services.chat.handlers.structured_direct import (
    _is_summary_query,
    _requested_fields,
    handle_structured_direct,
)
from app.services.chat.handlers.structured_llm import handle_structured_llm
from app.services.chat.interfaces import (
    InsightsBundle,
    MeetingMeta,
    RetrievedChunk,
)
from app.services.chat.prompts import GENERAL_REFUSE_TEMPLATE


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, text: str = "fake answer", raise_on_call: bool = False):
        self._text = text
        self._raise = raise_on_call
        self.last_messages: list[dict] = []

    async def complete_text(self, deployment, messages, max_tokens=600, temperature=0.3):
        self.last_messages = messages
        if self._raise:
            raise RuntimeError("LLM down")
        return self._text

    async def complete_json(self, deployment, messages, max_tokens=400, temperature=0.0):
        return {}


class FakeMetadataRepo:
    def __init__(self, meetings: list[MeetingMeta] | None = None):
        self._meetings = list(meetings or [])

    async def get_meetings(self, meeting_ids):
        return [m for m in self._meetings if m.meeting_id in set(meeting_ids)]

    async def get_participants(self, meeting_ids):
        return {}

    async def search_by_title(self, candidate_titles, allowed_meeting_ids=None):
        return []

    async def get_meetings_in_date_range(self, date_from, date_to, allowed_meeting_ids=None):
        return []


class FakeInsightsRepo:
    def __init__(
        self,
        insights: list[InsightsBundle] | None = None,
        summary_texts: dict[uuid.UUID, str] | None = None,
    ):
        self._insights = list(insights or [])
        self._summaries = dict(summary_texts or {})

    async def get_insights(self, meeting_ids):
        return [ib for ib in self._insights if ib.meeting_id in set(meeting_ids)]

    async def get_summary_text(self, meeting_id):
        return self._summaries.get(meeting_id)


class FakeChunkSearcher:
    def __init__(self, chunks: list[RetrievedChunk] | None = None):
        self._chunks = list(chunks or [])

    async def hybrid_search(self, query_embedding, query_text, meeting_ids, filters, top_k=10):
        meeting_set = set(meeting_ids)
        return [c for c in self._chunks if c.meeting_id in meeting_set][:top_k]


async def fake_embed(text: str) -> list[float]:
    return [0.1] * 8  # tiny vector for tests


# ── Fixture builders ──────────────────────────────────────────────────────────

def _meeting(title="Test Meeting", date_str="2026-04-15") -> MeetingMeta:
    return MeetingMeta(
        meeting_id=uuid.uuid4(),
        title=title,
        date=datetime.fromisoformat(f"{date_str}T10:00:00"),
        duration_minutes=30,
        organizer_name="Org Person",
        participants=[
            {"name": "Ashish Jaiswal", "email": "a@x.com", "role": "attendee", "graph_id": "g1"},
            {"name": "Rahul Verma",   "email": "r@x.com", "role": "attendee", "graph_id": "g2"},
        ],
        status="ready",
    )


def _insights(meeting_id, title="Test Meeting", summary="One-paragraph summary.") -> InsightsBundle:
    return InsightsBundle(
        meeting_id=meeting_id,
        meeting_title=title,
        meeting_date=datetime(2026, 4, 15, 10, 0),
        summary=summary,
        action_items=[
            {"task": "Draft proposal", "owner": "Ashish", "due_date": "2026-05-01"},
            "Review with legal",
        ],
        key_decisions=[{"decision": "Hold pricing", "context": "Customer pushback"}],
        follow_ups=["Confirm Q3 timeline"],
    )


def _chunk(meeting_id, title="Test Meeting", text='We should raise prices.') -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        meeting_id=meeting_id,
        meeting_title=title,
        meeting_date=datetime(2026, 4, 15, 10, 0),
        speakers=["Ashish"],
        chunk_text=[{"n": "Ashish Jaiswal", "sn": "Ashish", "t": text}],
        start_ms=60_000,
        end_ms=70_000,
        score=0.85,
    )


# ── handle_meta ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_meta_happy_path():
    m = _meeting()
    result = await handle_meta(
        query="who attended this meeting",
        meeting_ids=[m.meeting_id],
        metadata_repo=FakeMetadataRepo([m]),
        llm=FakeLLM(text="Ashish and Rahul attended."),
    )
    assert result.answer == "Ashish and Rahul attended."
    assert len(result.sources) == 1
    assert result.referenced_meeting_ids == [m.meeting_id]


@pytest.mark.asyncio
async def test_meta_empty_meeting_ids():
    result = await handle_meta(
        query="x", meeting_ids=[], metadata_repo=FakeMetadataRepo(), llm=FakeLLM(),
    )
    assert "couldn't find" in result.answer.lower()


@pytest.mark.asyncio
async def test_meta_no_meetings_returned():
    m = _meeting()
    result = await handle_meta(
        query="x", meeting_ids=[m.meeting_id],
        metadata_repo=FakeMetadataRepo([]),  # empty repo
        llm=FakeLLM(),
    )
    assert "couldn't find" in result.answer.lower()


# ── handle_structured_direct ──────────────────────────────────────────────────

class TestRequestedFields:
    def test_action_keyword(self):
        assert "action_items" in _requested_fields("list action items")

    def test_decision_keyword(self):
        assert "key_decisions" in _requested_fields("what was decided")

    def test_follow_up_keyword(self):
        assert "follow_ups" in _requested_fields("any open follow-ups?")

    def test_ambiguous_returns_all(self):
        # "what's important" — no specific token → return all 3 sections
        assert _requested_fields("what's important") == [
            "action_items", "key_decisions", "follow_ups",
        ]


@pytest.mark.asyncio
async def test_structured_direct_action_items_renders_owner_and_due():
    m = _meeting()
    ib = _insights(m.meeting_id)
    result = await handle_structured_direct(
        query="list action items",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
    )
    assert "Action items" in result.answer
    assert "Draft proposal" in result.answer
    assert "owner: Ashish" in result.answer or "Ashish" in result.answer
    # Should NOT contain Python dict literal syntax
    assert "{'task'" not in result.answer
    assert "'owner'" not in result.answer


@pytest.mark.asyncio
async def test_structured_direct_empty_returns_no_results():
    result = await handle_structured_direct(
        query="list action items", meeting_ids=[], insights_repo=FakeInsightsRepo(),
    )
    assert "couldn't find" in result.answer.lower()


class TestSummaryDetection:
    @pytest.mark.parametrize("query", [
        "summarise the meeting",
        "summarize this",
        "tl;dr",
        "tldr please",
        "give me a recap of yesterday",
        "what's the gist",
        "what was discussed in the Q4 review",
        "rundown of last meeting",
    ])
    def test_recognises_summary_intent(self, query):
        assert _is_summary_query(query) is True

    @pytest.mark.parametrize("query", [
        "list action items",
        "what was decided",
        "did we agree on pricing",
        "any follow ups",
        "what did Sarah say",
    ])
    def test_does_not_misclassify_non_summary(self, query):
        assert _is_summary_query(query) is False


@pytest.mark.asyncio
async def test_structured_direct_summary_renders_full_digest():
    """Summary intent → full digest with all 4 sections, cached fields verbatim."""
    m = _meeting()
    ib = _insights(m.meeting_id)
    result = await handle_structured_direct(
        query="summarise this meeting",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
    )
    # All four required sections present
    assert "**Meeting**" in result.answer
    assert "**Overview**" in result.answer
    assert "**Key Decisions**" in result.answer
    assert "**Action Items**" in result.answer
    assert "**Follow-ups / Open Questions**" in result.answer
    # Cached summary text used VERBATIM (no LLM paraphrasing)
    assert "One-paragraph summary." in result.answer
    # Action items rendered cleanly, NOT as dict literals
    assert "Draft proposal" in result.answer
    assert "{'task'" not in result.answer
    assert "Hold pricing" in result.answer


@pytest.mark.asyncio
async def test_structured_direct_summary_includes_none_recorded_for_empty_section():
    """Per-section 'None recorded.' shows when at least one field has content."""
    m = _meeting()
    ib = InsightsBundle(
        meeting_id=m.meeting_id,
        meeting_title="X",
        meeting_date=datetime(2026, 4, 15, 10, 0),
        summary="Just a summary, no other data.",
        action_items=[],
        key_decisions=[],
        follow_ups=[],
    )
    result = await handle_structured_direct(
        query="tl;dr",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
    )
    assert "Just a summary, no other data." in result.answer
    # Three "None recorded." occurrences for the three empty sections.
    assert result.answer.count("None recorded.") == 3


@pytest.mark.asyncio
async def test_structured_direct_summary_all_empty_returns_no_results():
    """Meeting with literally no insight content → fall through to no-results,
    so the orchestrator's STRUCTURED_DIRECT → SEARCH fallthrough triggers."""
    m = _meeting()
    ib = InsightsBundle(
        meeting_id=m.meeting_id, meeting_title="X",
        meeting_date=datetime(2026, 4, 15, 10, 0),
        summary=None, action_items=[], key_decisions=[], follow_ups=[],
    )
    result = await handle_structured_direct(
        query="summarise",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
    )
    assert "couldn't find" in result.answer.lower()


@pytest.mark.asyncio
async def test_structured_direct_router_hint_overrides_keyword_detection():
    """Router's structured_intent='digest' should produce full digest even when
    the query has no summary keywords (LLM is now the decider, not regex)."""
    m = _meeting()
    ib = _insights(m.meeting_id)
    result = await handle_structured_direct(
        query="give me the rundown",   # ambiguous-ish, no 'summarise' token
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
        structured_intent="digest",     # router's hint
    )
    assert "**Overview**" in result.answer
    assert "**Key Decisions**" in result.answer
    assert "**Action Items**" in result.answer


@pytest.mark.asyncio
async def test_structured_direct_router_hint_list_actions():
    m = _meeting()
    ib = _insights(m.meeting_id)
    result = await handle_structured_direct(
        query="anything I need to do",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
        structured_intent="list_actions",
    )
    assert "Action items" in result.answer
    # Router hint says list_actions only — decisions/follow-ups should NOT appear.
    assert "Decisions" not in result.answer
    assert "Follow-ups" not in result.answer


@pytest.mark.asyncio
async def test_structured_direct_invalid_hint_falls_back_to_keywords():
    """Defensive: if a future caller passes a junk hint, handler still works."""
    m = _meeting()
    ib = _insights(m.meeting_id)
    result = await handle_structured_direct(
        query="list action items",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
        structured_intent="not_a_valid_intent",
    )
    # Falls back to keyword path — should pick action_items from the query.
    assert "Action items" in result.answer
    assert "Draft proposal" in result.answer


# ── handle_clarify ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clarify_uses_llm_for_question():
    from app.services.chat.handlers.clarify import handle_clarify
    llm = FakeLLM(text="Did you mean to confirm the scope change?")
    result = await handle_clarify(query="yes include them", llm=llm)
    assert result.answer == "Did you mean to confirm the scope change?"
    # Verify CLARIFY_SYSTEM is used (not GENERAL_REFUSE template etc.)
    assert llm.last_messages[0]["role"] == "system"
    assert "clarifying question" in llm.last_messages[0]["content"].lower()


@pytest.mark.asyncio
async def test_clarify_falls_back_to_template_on_llm_error():
    from app.services.chat.handlers.clarify import handle_clarify
    from app.services.chat.prompts import CLARIFY_TEMPLATE_FALLBACK
    llm = FakeLLM(raise_on_call=True)
    result = await handle_clarify(query="yes", llm=llm)
    assert result.answer == CLARIFY_TEMPLATE_FALLBACK


@pytest.mark.asyncio
async def test_clarify_passes_history_to_llm():
    from app.services.chat.handlers.clarify import handle_clarify
    llm = FakeLLM(text="Could you tell me which meeting you mean?")
    history = [
        {"role": "user", "content": "what about yesterday's meeting"},
        {"role": "assistant", "content": "Want me to expand scope?"},
    ]
    await handle_clarify(query="yes", llm=llm, history=history)
    msgs = llm.last_messages
    # System + 2 history + user query = 4 messages
    assert len(msgs) == 4
    assert msgs[1]["content"] == "what about yesterday's meeting"
    assert msgs[2]["content"] == "Want me to expand scope?"
    assert msgs[3]["content"] == "yes"


@pytest.mark.asyncio
async def test_structured_direct_decision_only():
    m = _meeting()
    ib = _insights(m.meeting_id)
    result = await handle_structured_direct(
        query="what decisions were made",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
    )
    assert "Decisions" in result.answer
    assert "Hold pricing" in result.answer
    # Action items keyword wasn't asked — should not be in the answer.
    assert "Action items" not in result.answer


# ── Owner / speaker filter ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_structured_direct_filters_action_items_by_owner():
    """`speaker_name_filter='Ashish'` keeps only items whose owner matches."""
    m = _meeting()
    ib = InsightsBundle(
        meeting_id=m.meeting_id,
        meeting_title="Test",
        meeting_date=datetime(2026, 4, 15),
        summary="x",
        action_items=[
            {"task": "Draft proposal", "owner": "Ashish Jaiswal"},
            {"task": "Review legal", "owner": "Rahul"},
            {"task": "Onboard intern", "owner": "Ashish J."},
            {"task": "All-hands", "owner": "Team"},
        ],
        key_decisions=[],
        follow_ups=[],
    )
    result = await handle_structured_direct(
        query="action items of Ashish",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
        structured_intent="list_actions",
        speaker_name_filter="Ashish",
    )
    assert "Draft proposal" in result.answer
    assert "Onboard intern" in result.answer            # "Ashish" substring of "Ashish J."
    assert "Review legal" not in result.answer          # owner=Rahul → excluded
    assert "All-hands" not in result.answer             # owner=Team → excluded


@pytest.mark.asyncio
async def test_structured_direct_filter_with_no_matches_skips_meeting():
    """Filter that matches NOTHING → meeting block is skipped → handler
    returns the no-results message so orchestrator can fall through."""
    m = _meeting()
    ib = InsightsBundle(
        meeting_id=m.meeting_id, meeting_title="X", meeting_date=datetime(2026, 4, 15),
        summary="", action_items=[{"task": "A", "owner": "Rahul"}],
        key_decisions=[], follow_ups=[],
    )
    result = await handle_structured_direct(
        query="action items of Sarah",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
        structured_intent="list_actions",
        speaker_name_filter="Sarah",
    )
    assert "couldn't find" in result.answer.lower()


@pytest.mark.asyncio
async def test_structured_direct_digest_with_owner_filter_drops_summary_and_filters_lists():
    """Digest mode + owner filter: free-form summary is suppressed (not
    owner-attributable), lists are filtered."""
    m = _meeting()
    ib = InsightsBundle(
        meeting_id=m.meeting_id, meeting_title="X", meeting_date=datetime(2026, 4, 15),
        summary="Long meeting narrative not tied to any one owner.",
        action_items=[
            {"task": "Mine", "owner": "Ashish Jaiswal"},
            {"task": "Theirs", "owner": "Other"},
        ],
        key_decisions=[{"decision": "Decided X", "owner": "Ashish Jaiswal"}],
        follow_ups=[],
    )
    result = await handle_structured_direct(
        query="summarise for Ashish Jaiswal",
        meeting_ids=[m.meeting_id],
        insights_repo=FakeInsightsRepo([ib]),
        structured_intent="digest",
        speaker_name_filter="Ashish Jaiswal",
    )
    assert "Mine" in result.answer
    assert "Decided X" in result.answer
    assert "Theirs" not in result.answer
    # Free-form summary is suppressed under owner filter.
    assert "Long meeting narrative" not in result.answer


# ── handle_structured_llm ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_structured_llm_happy_path():
    m = _meeting()
    ib = _insights(m.meeting_id)
    llm = FakeLLM(text="Coherent narrative summary.")
    result = await handle_structured_llm(
        query="summarise the meeting",
        meeting_ids=[m.meeting_id],
        metadata_repo=FakeMetadataRepo([m]),
        insights_repo=FakeInsightsRepo([ib]),
        llm=llm,
    )
    assert result.answer == "Coherent narrative summary."
    assert "Meeting:" in llm.last_messages[-1]["content"]
    assert "Summary:" in llm.last_messages[-1]["content"]
    assert result.referenced_meeting_ids == [m.meeting_id]


@pytest.mark.asyncio
async def test_structured_llm_no_insights():
    m = _meeting()
    result = await handle_structured_llm(
        query="summarise",
        meeting_ids=[m.meeting_id],
        metadata_repo=FakeMetadataRepo([m]),
        insights_repo=FakeInsightsRepo([]),
        llm=FakeLLM(),
    )
    assert "couldn't find" in result.answer.lower()


# ── handle_search ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_happy_path():
    m = _meeting()
    ch = _chunk(m.meeting_id)
    llm = FakeLLM(text="Ashish suggested raising prices.")
    result = await handle_search(
        query="what did Ashish say about prices",
        search_query="ashish prices",
        meeting_ids=[m.meeting_id],
        filters={},
        metadata_repo=FakeMetadataRepo([m]),
        chunk_searcher=FakeChunkSearcher([ch]),
        llm=llm,
        embed=fake_embed,
    )
    assert result.answer == "Ashish suggested raising prices."
    assert len(result.sources) == 1
    assert result.sources[0].source_type == "transcript"
    # Context should contain time-ranged transcript layout.
    user_msg = llm.last_messages[-1]["content"]
    assert "[Time:" in user_msg
    assert "Ashish Jaiswal" in user_msg


@pytest.mark.asyncio
async def test_search_no_chunks():
    m = _meeting()
    result = await handle_search(
        query="x", search_query="x", meeting_ids=[m.meeting_id],
        filters={}, metadata_repo=FakeMetadataRepo([m]),
        chunk_searcher=FakeChunkSearcher([]),  # no chunks
        llm=FakeLLM(), embed=fake_embed,
    )
    assert "couldn't find" in result.answer.lower()


# ── handle_hybrid ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hybrid_happy_path():
    m = _meeting()
    ib = _insights(m.meeting_id)
    ch = _chunk(m.meeting_id)
    llm = FakeLLM(text="Combined narrative + quote.")
    result = await handle_hybrid(
        query="summarise and quote the discussion",
        search_query="discussion",
        meeting_ids=[m.meeting_id],
        filters={},
        metadata_repo=FakeMetadataRepo([m]),
        insights_repo=FakeInsightsRepo([ib]),
        chunk_searcher=FakeChunkSearcher([ch]),
        llm=llm,
        embed=fake_embed,
    )
    assert result.answer == "Combined narrative + quote."
    # Context should contain BOTH insight section AND chunk section.
    user_msg = llm.last_messages[-1]["content"]
    assert "Summary:" in user_msg
    assert "[Time:" in user_msg


@pytest.mark.asyncio
async def test_hybrid_empty_inputs():
    m = _meeting()
    result = await handle_hybrid(
        query="x", search_query="x", meeting_ids=[m.meeting_id],
        filters={}, metadata_repo=FakeMetadataRepo([m]),
        insights_repo=FakeInsightsRepo([]),
        chunk_searcher=FakeChunkSearcher([]),
        llm=FakeLLM(), embed=fake_embed,
    )
    assert "couldn't find" in result.answer.lower()


# ── handle_compare ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compare_happy_path():
    m1 = _meeting(title="Q3 Review", date_str="2026-04-01")
    m2 = _meeting(title="Q4 Planning", date_str="2026-04-20")
    ib1 = _insights(m1.meeting_id, title="Q3 Review", summary="Q3 results.")
    ib2 = _insights(m2.meeting_id, title="Q4 Planning", summary="Q4 plans.")
    llm = FakeLLM(text="Common ground: pricing was discussed in both.")
    result = await handle_compare(
        query="compare these meetings",
        meeting_ids=[m1.meeting_id, m2.meeting_id],
        metadata_repo=FakeMetadataRepo([m1, m2]),
        insights_repo=FakeInsightsRepo([ib1, ib2]),
        llm=llm,
    )
    assert result.answer == "Common ground: pricing was discussed in both."
    user_msg = llm.last_messages[-1]["content"]
    # Both labelled sections should be present.
    assert "=== Meeting A:" in user_msg
    assert "=== Meeting B:" in user_msg
    # Chronological order — Q3 should be Meeting A.
    assert user_msg.index("=== Meeting A: Q3 Review") < user_msg.index("=== Meeting B: Q4 Planning")


@pytest.mark.asyncio
async def test_compare_summary_only_mode_for_medium_scope():
    """6+ meetings → handler skips the bulk insights fetch and only shows
    summaries in the LLM context. Per-meeting Decisions/Actions/Follow-ups
    are deliberately absent."""
    meetings = [_meeting(title=f"M{i}", date_str=f"2026-04-{10+i:02d}") for i in range(7)]
    summaries = {m.meeting_id: f"Summary of {m.title}" for m in meetings}
    llm = FakeLLM(text="Common theme: X.")

    # Provide insights for ALL meetings — but the handler should NOT use them
    # in the LLM context when scope > COMPARE_MAX_FULL.
    insights = [_insights(m.meeting_id, title=m.title) for m in meetings]

    result = await handle_compare(
        query="compare these",
        meeting_ids=[m.meeting_id for m in meetings],
        metadata_repo=FakeMetadataRepo(meetings),
        insights_repo=FakeInsightsRepo(insights, summary_texts=summaries),
        llm=llm,
    )
    user_msg = llm.last_messages[-1]["content"]
    # Each meeting label should appear, with its summary text from the cache.
    assert user_msg.count("=== Meeting") == 7
    assert "Summary of M0" in user_msg
    # Decisions / Actions / Follow-ups MUST be absent in summary-only mode.
    assert "Decisions:" not in user_msg
    assert "Action items:" not in user_msg
    assert "Follow-ups:" not in user_msg


@pytest.mark.asyncio
async def test_compare_refuses_when_scope_exceeds_summary_limit():
    """Scope > COMPARE_MAX_SUMMARY → bot refuses politely, no LLM call."""
    from app.services.chat.config import COMPARE_MAX_SUMMARY
    n = COMPARE_MAX_SUMMARY + 5
    meetings = [_meeting(title=f"M{i}") for i in range(n)]
    llm = FakeLLM(text="(should not be called)")
    result = await handle_compare(
        query="compare these",
        meeting_ids=[m.meeting_id for m in meetings],
        metadata_repo=FakeMetadataRepo(meetings),
        insights_repo=FakeInsightsRepo([]),
        llm=llm,
    )
    assert "more than i can compare meaningfully" in result.answer.lower()
    assert str(n) in result.answer
    # No LLM calls — refusal is templated.
    assert llm.last_messages == []


@pytest.mark.asyncio
async def test_compare_full_mode_includes_all_sections_for_small_scope():
    """≤ COMPARE_MAX_FULL meetings → full insight bundle in context."""
    m1 = _meeting("Q3 Review", date_str="2026-04-01")
    m2 = _meeting("Q4 Planning", date_str="2026-04-20")
    ib1 = _insights(m1.meeting_id, title="Q3 Review")
    ib2 = _insights(m2.meeting_id, title="Q4 Planning")
    llm = FakeLLM(text="Common ground.")
    await handle_compare(
        query="compare",
        meeting_ids=[m1.meeting_id, m2.meeting_id],
        metadata_repo=FakeMetadataRepo([m1, m2]),
        insights_repo=FakeInsightsRepo([ib1, ib2]),
        llm=llm,
    )
    user_msg = llm.last_messages[-1]["content"]
    # Full bundle: Summary + Decisions + Action items + Follow-ups all present.
    assert "Summary:" in user_msg
    assert "Decisions:" in user_msg
    assert "Action items:" in user_msg


@pytest.mark.asyncio
async def test_compare_needs_two_meetings():
    m = _meeting()
    result = await handle_compare(
        query="compare", meeting_ids=[m.meeting_id],
        metadata_repo=FakeMetadataRepo([m]),
        insights_repo=FakeInsightsRepo([]), llm=FakeLLM(),
    )
    assert "at least two meetings" in result.answer.lower()


# ── handle_general ────────────────────────────────────────────────────────────

def test_general_refuse_returns_template_without_llm():
    result = handle_general_refuse()
    assert result.answer == GENERAL_REFUSE_TEMPLATE
    assert result.sources == []


@pytest.mark.asyncio
async def test_general_gk_uses_llm():
    llm = FakeLLM(text="Best practice: timebox standups to 15 minutes.")
    result = await handle_general_gk(
        query="how can we improve our standups",
        llm=llm,
    )
    assert "timebox" in result.answer
    # System prompt should be the GENERAL_GK_SYSTEM.
    assert llm.last_messages[0]["role"] == "system"


@pytest.mark.asyncio
async def test_general_gk_falls_back_on_llm_error():
    llm = FakeLLM(raise_on_call=True)
    result = await handle_general_gk(query="x", llm=llm)
    assert result.answer == GENERAL_REFUSE_TEMPLATE
