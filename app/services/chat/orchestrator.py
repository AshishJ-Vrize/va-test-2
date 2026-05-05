"""Chat orchestrator — single entry point that wires every chat phase together.

handle_chat() does, in order:
   1. Resolve session (get-or-create per session_id).
   2. Compute RBAC scope: meetings the user can see in the last 30 days,
      narrowed by `access_filter` (attended / granted / all).
   3. Resolve effective selected_meeting_ids — caller's request override OR
      session's stored scope. Silently filters to authorised+30d meetings.
   4. Run the LLM router → `RouterDecision`.
   5. Short-circuit GENERAL_REFUSE (no LLM call needed at all).
   6. Run scope.narrow_within_scope (handles "specific meeting within scope"
      and surfaces extras outside selection).
   7. Detect scope-change suggestion banner.
   8. Dispatch to the matched handler. STRUCTURED_DIRECT auto-falls-through to
      SEARCH when no insights exist for the selected meetings.
   9. Update session state (scope, last_referenced_meeting, last_intent, turns).
  10. Wrap into an OrchestratorResult — endpoint serialises to ChatResponse.

Tests inject fakes for every dependency; production wiring constructs them
from the request's tenant `db` session.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.config import (
    HISTORY_TURN_PAIRS,
    RBAC_MAX_MEETINGS,
    RBAC_WITHIN_DAYS,
)
from app.services.chat.handlers._common import HandlerResult
from app.services.chat.handlers.clarify import handle_clarify
from app.services.chat.handlers.compare import handle_compare
from app.services.chat.handlers.general import handle_general_gk, handle_general_refuse
from app.services.chat.handlers.hybrid import handle_hybrid
from app.services.chat.handlers.meta import handle_meta
from app.services.chat.handlers.search import handle_search
from app.services.chat.handlers.structured_direct import handle_structured_direct
from app.services.chat.handlers.structured_llm import handle_structured_llm
from app.services.chat.interfaces import (
    ChatScope,
    ChunkSearcher,
    InsightsRepo,
    LLMClient,
    MetadataRepo,
    PendingDisambiguation,
    RouterDecision,
    SessionStore,
    SpeakerResolver,
)
from app.services.chat.router import classify_query
from app.services.chat.scope import (
    ScopeChangeSuggestion,
    detect_scope_change_suggestion,
    narrow_within_scope,
)
from app.services.chat.sources import SourceCard

log = logging.getLogger(__name__)


# Type alias for embedding fn — orchestrator passes through to handlers.
EmbedFn = Callable[[str], Awaitable[list[float]]]


# ── Result type returned to the route ─────────────────────────────────────────

@dataclass
class RbacScopeInfo:
    """Visibility into how the RBAC rails are bounding the searchable set.

    `total` is the number of meetings the user has access to inside the date
    window (unbounded by RBAC_MAX_MEETINGS). `visible` is what's actually
    searchable after the count cap. `capped` is True iff total > visible.
    """
    total: int
    visible: int
    capped: bool
    within_days: int
    max_meetings: int


@dataclass
class OrchestratorResult:
    session_id: UUID
    answer: str
    route: str
    sources: list[SourceCard] = field(default_factory=list)
    scope_change: ScopeChangeSuggestion | None = None
    out_of_window: bool = False
    speaker_disambiguation: list[dict] | None = None
    rbac_scope_info: RbacScopeInfo | None = None


# ── Public entry point ───────────────────────────────────────────────────────

async def handle_chat(
    *,
    query: str,
    request_meeting_ids: list[UUID] | None,
    access_filter: str,
    session_id: UUID | None,
    current_user_graph_id: str,
    db: AsyncSession,
    # Dependencies — injectable for tests:
    llm: LLMClient,
    metadata_repo: MetadataRepo,
    insights_repo: InsightsRepo,
    chunk_searcher: ChunkSearcher,
    speaker_resolver: SpeakerResolver,
    session_store: SessionStore,
    embed: EmbedFn,
) -> OrchestratorResult:
    """Run one user turn end-to-end. Returns the data the endpoint serialises."""
    # 1. Session
    sid = session_id or uuid.uuid4()
    state = session_store.get_or_create(str(sid))

    # 2. RBAC: authorised meetings in the configured recency window for this user.
    #    Combined as INTERSECTION — date window AND most-recent-N cap. Whichever
    #    bound is more restrictive wins.
    authorised_ids = await metadata_repo.get_authorized_meeting_ids(
        graph_id=current_user_graph_id,
        access_filter=access_filter,
        within_days=RBAC_WITHIN_DAYS,
        max_meetings=RBAC_MAX_MEETINGS,
    )
    authorised_set = set(authorised_ids)

    # 2a. Detect whether the count cap is actually biting. We only run a second
    # query when the result count *equals* the cap — otherwise the cap is
    # provably not in play and the unbounded count is already known.
    rbac_scope_info = await _build_rbac_scope_info(
        metadata_repo=metadata_repo,
        graph_id=current_user_graph_id,
        access_filter=access_filter,
        visible_count=len(authorised_ids),
    )

    # 2b. Identity context — user display name + their role in each authorised
    # meeting. Used downstream to answer attendance-style questions accurately
    # ("did I attend …" must consult role, not the speakers list).
    user_display_name = await metadata_repo.get_user_display_name(current_user_graph_id)
    user_roles = await metadata_repo.get_user_role_per_meeting(
        graph_id=current_user_graph_id, meeting_ids=authorised_ids,
    )

    # 3. Effective selection — caller override or session default.
    #    With nothing explicitly selected, default to the FULL authorised scope
    #    (capped by RBAC_MAX_MEETINGS). The router's date_from/date_to/title
    #    filters narrow further in step 7. This way "action items this month"
    #    looks across every meeting in scope instead of only the most-recent one.
    if request_meeting_ids:                          # non-None and non-empty
        selected = [mid for mid in request_meeting_ids if mid in authorised_set]
    elif state.scope.meeting_ids:
        selected = [mid for mid in state.scope.meeting_ids if mid in authorised_set]
    else:
        selected = list(authorised_ids)              # full scope

    # Persist the access filter + selection for follow-up turns.
    session_store.update_scope(str(sid), ChatScope(meeting_ids=selected, access_filter=access_filter))

    # Always record the user turn (even when we short-circuit below).
    session_store.record_turn(str(sid), "user", query)
    history = _format_history(
        session_store.get_recent_turns(str(sid), n=HISTORY_TURN_PAIRS)
    )

    # 4. Router (skipped when we resolve a pending disambiguation below).
    decision: RouterDecision | None = None
    query_for_dispatch = query

    # 4a. Pending-disambiguation pre-check.
    # If the previous turn surfaced a candidate list and this turn looks like
    # the user picking one, REPLAY the original query with the speaker resolved.
    # Skips router entirely for this turn.
    pending = state.pending_disambiguation
    if pending:
        matched_gid = _resolve_disambiguation_choice(query, pending.candidates)
        # Always clear — user gets one chance to pick. If they didn't pick,
        # treat their input as a fresh query (router runs normally below).
        session_store.set_pending_disambiguation(str(sid), None)
        if matched_gid:
            decision = _patch_decision_with_speaker(
                pending.original_decision, matched_gid, candidates=pending.candidates,
            )
            query_for_dispatch = pending.original_query
            log.info(
                "disambig: resolved reply=%r → graph_id=%s; replaying original=%r",
                query[:80], matched_gid, pending.original_query[:80],
            )

    # Run the router only if disambiguation didn't already produce a decision.
    if decision is None:
        decision = await classify_query(
            query=query,
            llm=llm,
            speaker_resolver=speaker_resolver,
        )

        # 4b. CLARIFY-loop guard. If the previous turn was already a CLARIFY
        # and the router wants to CLARIFY again, force progress instead — the
        # user is trying to answer and our follow-ups aren't landing.
        if decision.route == "CLARIFY" and state.last_intent == "CLARIFY":
            log.info("clarify-loop guard: forcing STRUCTURED_LLM fallback")
            decision = RouterDecision(
                route="STRUCTURED_LLM",
                filters=decision.filters,
                scope_intent=decision.scope_intent,
                out_of_window=decision.out_of_window,
                search_query=decision.search_query or query,
            )

    # Use query_for_dispatch from here on (may be the original from a replay).
    query = query_for_dispatch

    # 5. GENERAL_REFUSE — no LLM, return immediately
    if decision.route == "GENERAL_REFUSE":
        result = handle_general_refuse()
        session_store.record_turn(str(sid), "assistant", result.answer)
        session_store.set_last_intent(str(sid), decision.route)
        return _wrap(sid, decision.route, result, scope_change=None,
                     out_of_win=False, decision=decision,
                     rbac_scope_info=rbac_scope_info)

    # 5b. GENERAL_GK — LLM only, no DB
    if decision.route == "GENERAL_GK":
        result = await handle_general_gk(query=query, llm=llm, history=history)
        session_store.record_turn(str(sid), "assistant", result.answer)
        session_store.set_last_intent(str(sid), decision.route)
        return _wrap(sid, decision.route, result, scope_change=None,
                     out_of_win=False, decision=decision,
                     rbac_scope_info=rbac_scope_info)

    # 5c. CLARIFY — short fragment / ambiguous query → ask follow-up
    if decision.route == "CLARIFY":
        result = await handle_clarify(query=query, llm=llm, history=history)
        session_store.record_turn(str(sid), "assistant", result.answer)
        session_store.set_last_intent(str(sid), decision.route)
        return _wrap(sid, decision.route, result, scope_change=None,
                     out_of_win=False, decision=decision,
                     rbac_scope_info=rbac_scope_info)

    # 6. Speaker disambiguation — surface candidates if the router found 2+ matches.
    # Stash the original query so the NEXT turn can replay it with the resolved speaker.
    if decision.filters.get("speaker_disambiguation_needed"):
        return _disambiguation_response(
            sid, decision, session_store, original_query=query,
        )

    # 7. Narrow within scope (handles "specific meeting within selection")
    narrow = await narrow_within_scope(
        selected_ids=selected,
        requested_titles=decision.filters.get("meeting_titles"),
        date_from=decision.filters.get("date_from"),
        date_to=decision.filters.get("date_to"),
        tenant_30d_meeting_ids=authorised_ids,
        metadata_repo=metadata_repo,
    )
    # Effective scope:
    #   - request not narrowed → keep the user's selection
    #   - narrowed AND matched found in selection → use the matches (selection-respecting)
    #   - narrowed AND nothing matched in selection BUT extras exist → AUTO-EXPAND
    #     to those extras. The user explicitly referenced a date/title; the right
    #     answer comes from those meetings, not from a default selection that
    #     happens to be unrelated. The scope_change banner will retroactively
    #     disclose the expansion.
    if not narrow.narrowed:
        effective_ids = selected
        auto_expanded = False
    elif narrow.matched_ids:
        effective_ids = narrow.matched_ids
        auto_expanded = False
    elif narrow.extra_ids:
        effective_ids = narrow.extra_ids
        auto_expanded = True
    else:
        effective_ids = []
        auto_expanded = False

    # 7b. Cap-aware empty-result short-circuit. The user filtered by date or
    # title and we found nothing in the authorised scope (matched=[]) AND no
    # extras within the broader searchable set (extras=[]). If the count cap
    # is biting, the meeting they're asking about may exist beyond the cap —
    # tell them so instead of pretending it doesn't exist.
    has_specific_filter = bool(
        decision.filters.get("date_from")
        or decision.filters.get("date_to")
        or decision.filters.get("meeting_titles")
    )
    if (
        has_specific_filter
        and narrow.narrowed
        and not narrow.matched_ids
        and not narrow.extra_ids
        and rbac_scope_info.capped
    ):
        msg = _capped_no_match_message(decision.filters, rbac_scope_info)
        result = HandlerResult(answer=msg)
        session_store.record_turn(str(sid), "assistant", msg)
        session_store.set_last_intent(str(sid), decision.route)
        return _wrap(sid, decision.route, result, scope_change=None,
                     out_of_win=decision.out_of_window, decision=decision,
                     rbac_scope_info=rbac_scope_info)

    # Build a short USER CONTEXT block describing the asker's identity + role
    # per effective meeting. Handlers prepend this to the system prompt so the
    # LLM can answer "did I attend …" correctly (role-based, not speaker-based).
    user_context = _build_user_context(
        display_name=user_display_name,
        graph_id=current_user_graph_id,
        roles=user_roles,
        effective_ids=effective_ids,
        rbac_scope_info=rbac_scope_info,
    )

    # 8. Scope-change suggestion banner
    scope_change = detect_scope_change_suggestion(
        narrow_result=narrow,
        router_decision=decision,
        selected_ids=selected,
        auto_expanded=auto_expanded,
    )

    # 9. Out-of-recency-window soft suggestion (date-window only — count-cap
    # exhaustion is silent because users don't query in count terms).
    out_of_win = decision.out_of_window

    # 10. Dispatch to handler
    route = decision.route
    result: HandlerResult
    if route == "META":
        result = await handle_meta(
            query=query, meeting_ids=effective_ids,
            metadata_repo=metadata_repo, llm=llm, history=history,
            user_context=user_context,
        )
    elif route == "STRUCTURED_DIRECT":
        result = await handle_structured_direct(
            query=query, meeting_ids=effective_ids, insights_repo=insights_repo,
            structured_intent=decision.filters.get("structured_intent"),
            speaker_name_filter=decision.filters.get("speaker_name"),
        )
        # Fall-through to SEARCH when insights returned nothing. We try the
        # NARROWED scope first; if even that's empty (date filter matched no
        # meetings), search the broader RBAC scope so the user gets *something*
        # rather than a flat refusal.
        if result.is_empty:
            search_scope = effective_ids or list(authorised_ids)
            log.info("STRUCTURED_DIRECT empty — falling through to SEARCH (scope=%d ids)",
                     len(search_scope))
            route = "SEARCH"
            result = await handle_search(
                query=query, search_query=decision.search_query,
                meeting_ids=search_scope, filters=decision.filters,
                metadata_repo=metadata_repo, chunk_searcher=chunk_searcher,
                llm=llm, embed=embed, history=history,
                user_context=user_context,
            )
    elif route == "STRUCTURED_LLM":
        result = await handle_structured_llm(
            query=query, meeting_ids=effective_ids,
            metadata_repo=metadata_repo, insights_repo=insights_repo,
            llm=llm, history=history,
            user_context=user_context,
        )
        if result.is_empty:
            search_scope = effective_ids or list(authorised_ids)
            log.info("STRUCTURED_LLM empty — falling through to SEARCH (scope=%d ids)",
                     len(search_scope))
            route = "SEARCH"
            result = await handle_search(
                query=query, search_query=decision.search_query,
                meeting_ids=search_scope, filters=decision.filters,
                metadata_repo=metadata_repo, chunk_searcher=chunk_searcher,
                llm=llm, embed=embed, history=history,
                user_context=user_context,
            )
    elif route == "SEARCH":
        # Same broad-scope fallback for direct-SEARCH routing — ensures a
        # date filter that excluded all meetings still gets a chance to find
        # transcript matches across the user's full RBAC scope.
        search_scope = effective_ids or list(authorised_ids)
        result = await handle_search(
            query=query, search_query=decision.search_query,
            meeting_ids=search_scope, filters=decision.filters,
            metadata_repo=metadata_repo, chunk_searcher=chunk_searcher,
            llm=llm, embed=embed, history=history,
            user_context=user_context,
        )
    elif route == "HYBRID":
        result = await handle_hybrid(
            query=query, search_query=decision.search_query,
            meeting_ids=effective_ids, filters=decision.filters,
            metadata_repo=metadata_repo, insights_repo=insights_repo,
            chunk_searcher=chunk_searcher, llm=llm, embed=embed, history=history,
            user_context=user_context,
        )
    elif route == "COMPARE":
        result = await handle_compare(
            query=query, meeting_ids=effective_ids,
            metadata_repo=metadata_repo, insights_repo=insights_repo,
            llm=llm, history=history,
            user_context=user_context,
        )
    else:
        log.error("orchestrator: unknown route %r — falling back to SEARCH", route)
        result = await handle_search(
            query=query, search_query=decision.search_query,
            meeting_ids=effective_ids, filters=decision.filters,
            metadata_repo=metadata_repo, chunk_searcher=chunk_searcher,
            llm=llm, embed=embed, history=history,
            user_context=user_context,
        )

    # 11. Persist session updates
    session_store.record_turn(str(sid), "assistant", result.answer)
    session_store.set_last_intent(str(sid), route)
    if result.referenced_meeting_ids:
        session_store.set_last_referenced_meeting(
            str(sid), result.referenced_meeting_ids[0]
        )

    return _wrap(sid, route, result, scope_change=scope_change,
                 out_of_win=out_of_win, decision=decision,
                 rbac_scope_info=rbac_scope_info)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_history(turns: list) -> list[dict[str, str]]:
    """Turns dataclass list → message-shape for compose_with_llm."""
    return [{"role": t.role, "content": t.content} for t in turns]


_ROLE_DESCRIPTIONS = {
    "organizer": "organizer (you led this meeting)",
    "attendee":  "attendee (you attended)",
    "granted":   "granted access (you did NOT attend; an admin granted you access)",
}


def _build_user_context(
    *,
    display_name: str | None,
    graph_id: str,
    roles: dict[UUID, str],
    effective_ids: list[UUID],
    rbac_scope_info: RbacScopeInfo | None = None,
) -> str | None:
    """Compose the USER CONTEXT block for the LLM.

    Format:
        The user asking is <name>.
        Their role per meeting in scope:
        - <meeting_id>: organizer (...)
        - <meeting_id>: attendee (...)
        - <meeting_id>: granted access (...)
        SEARCHABLE SCOPE LIMIT: ...   (only when count cap is biting)
    """
    name = display_name or "the user"
    lines = [f"The user asking is {name} (graph_id={graph_id})."]

    if effective_ids:
        role_lines: list[str] = []
        for mid in effective_ids:
            role = roles.get(mid)
            if role:
                desc = _ROLE_DESCRIPTIONS.get(role, role)
                role_lines.append(f"- {mid}: {desc}")
            # If user not in participants for this meeting at all, omit — RBAC
            # should mean we never reach here, but staying defensive.

        if role_lines:
            lines.append("Their role per meeting in scope:")
            lines.extend(role_lines)

    # Surface count-cap state so the LLM can mention it when relevant
    # (e.g. user asks "list ALL my meetings" → it can say "showing the M most
    # recent of N total"). Only added when the cap is actually biting.
    if rbac_scope_info and rbac_scope_info.capped:
        lines.append(
            f"SEARCHABLE SCOPE LIMIT: This user has access to "
            f"{rbac_scope_info.total} meetings within the last "
            f"{rbac_scope_info.within_days} days, but only the "
            f"{rbac_scope_info.visible} most-recent are searchable in this "
            f"conversation (admin-configured cap). If the user asks about "
            f"older or 'all' meetings, mention this limit and suggest they "
            f"narrow by date or meeting title to reach beyond it."
        )

    return "\n".join(lines)


def _capped_no_match_message(filters: dict, info: RbacScopeInfo) -> str:
    """Compose a cap-aware "couldn't find" reply.

    Used when the user filtered by date/title, no match was found inside the
    visible scope, and the count cap is the likely culprit.
    """
    df, dt = filters.get("date_from"), filters.get("date_to")
    titles = filters.get("meeting_titles") or []

    # Describe what was searched.
    if titles:
        thing = f"a meeting matching {', '.join(repr(t) for t in titles)}"
    elif df and dt and df == dt:
        thing = f"a meeting on {df}"
    elif df and dt:
        thing = f"a meeting between {df} and {dt}"
    elif df:
        thing = f"a meeting on or after {df}"
    elif dt:
        thing = f"a meeting on or before {dt}"
    else:
        thing = "a matching meeting"

    return (
        f"I couldn't find {thing} in your currently-searchable scope. "
        f"Note: only the {info.visible} most-recent meetings are searchable "
        f"right now (out of {info.total} you have access to in the last "
        f"{info.within_days} days) — the meeting you're asking about may exist "
        f"but is outside this cap. Ask your admin to raise "
        f"`CHAT_RBAC_MAX_MEETINGS` to widen the searchable window."
    )


async def _build_rbac_scope_info(
    *,
    metadata_repo: MetadataRepo,
    graph_id: str,
    access_filter: str,
    visible_count: int,
) -> RbacScopeInfo:
    """Determine whether the count cap is biting; lazily query for the unbounded
    total only when it might be."""
    # If the cap is disabled, or the visible result set is smaller than the cap,
    # the cap is provably not in play — no need for a second roundtrip.
    if RBAC_MAX_MEETINGS <= 0 or visible_count < RBAC_MAX_MEETINGS:
        return RbacScopeInfo(
            total=visible_count,
            visible=visible_count,
            capped=False,
            within_days=RBAC_WITHIN_DAYS,
            max_meetings=RBAC_MAX_MEETINGS,
        )

    # visible_count == RBAC_MAX_MEETINGS — could be a coincidence (exactly that
    # many in window) or the cap actually biting. Query to find out.
    total = await metadata_repo.count_authorized_meetings(
        graph_id=graph_id,
        access_filter=access_filter,
        within_days=RBAC_WITHIN_DAYS,
    )
    return RbacScopeInfo(
        total=total,
        visible=visible_count,
        capped=total > visible_count,
        within_days=RBAC_WITHIN_DAYS,
        max_meetings=RBAC_MAX_MEETINGS,
    )


def _wrap(
    sid: UUID,
    route: str,
    result: HandlerResult,
    *,
    scope_change: ScopeChangeSuggestion | None,
    out_of_win: bool,
    decision: RouterDecision,
    rbac_scope_info: RbacScopeInfo | None = None,
) -> OrchestratorResult:
    return OrchestratorResult(
        session_id=sid,
        answer=result.answer,
        route=route,
        sources=result.sources,
        scope_change=scope_change if (scope_change and scope_change.surface) else None,
        out_of_window=out_of_win,
        rbac_scope_info=rbac_scope_info,
    )


def _disambiguation_response(
    sid: UUID,
    decision: RouterDecision,
    session_store: SessionStore,
    original_query: str,
) -> OrchestratorResult:
    """User mentioned a name that resolves to multiple participants —
    ask them to disambiguate, and stash pending state so the next turn
    can replay the original query with the speaker resolved."""
    candidates = decision.filters.get("speaker_candidates") or []
    speaker_name = decision.filters.get("speaker_name") or ""
    bullets = "\n".join(
        f"- {c['name']} ({c['email'] or 'no email on record'})"
        for c in candidates
    )
    answer = (
        f"You mentioned **{speaker_name}** — multiple people in your tenant "
        f"match that name. Which one did you mean?\n\n{bullets}\n\n"
        f"Reply with the email or full name and I'll re-run your question."
    )
    session_store.record_turn(str(sid), "assistant", answer)
    session_store.set_last_intent(str(sid), "DISAMBIGUATION")
    session_store.set_pending_disambiguation(str(sid), PendingDisambiguation(
        speaker_name=speaker_name,
        candidates=list(candidates),
        original_query=original_query,
        original_decision=decision,
    ))
    return OrchestratorResult(
        session_id=sid,
        answer=answer,
        route="DISAMBIGUATION",
        sources=[],
        speaker_disambiguation=candidates,
    )


def _resolve_disambiguation_choice(text: str, candidates: list[dict]) -> str | None:
    """Match a user's disambiguation reply against the candidate list.

    Tries (in order):
      1. Exact full-name match (case-insensitive).
      2. Exact email match.
      3. Last-token (last name) match unique to one candidate — useful for
         replies like "Jaiswal" when candidates are "Ashish Jaiswal" + "Ashish Choudhary".

    Returns the matched candidate's graph_id, or None if no/multiple matches.
    """
    t = text.strip().lower()
    if not t:
        return None

    name_matches = [
        c["graph_id"] for c in candidates
        if (c.get("name") or "").lower() == t
    ]
    if len(name_matches) == 1:
        return name_matches[0]

    email_matches = [
        c["graph_id"] for c in candidates
        if (c.get("email") or "").lower() == t
    ]
    if len(email_matches) == 1:
        return email_matches[0]

    user_tokens = t.split()
    if user_tokens:
        last = user_tokens[-1]
        last_token_matches = [
            c["graph_id"] for c in candidates
            if (c.get("name") or "").lower().split()[-1:] == [last]
        ]
        if len(last_token_matches) == 1:
            return last_token_matches[0]

    return None


def _patch_decision_with_speaker(
    decision: RouterDecision, graph_id: str,
    candidates: list[dict] | None = None,
) -> RouterDecision:
    """Clone a RouterDecision with the speaker resolved to a single graph_id —
    used to replay the original query after disambiguation.

    When `candidates` is provided, also overwrites `speaker_name` with the
    resolved candidate's full display name (e.g. "Ashish" → "Ashish Jaiswal")
    so downstream owner-matching works against the canonical name.
    """
    new_filters = dict(decision.filters)
    new_filters["speaker_graph_ids"] = [graph_id]
    new_filters["speaker_disambiguation_needed"] = False
    new_filters["speaker_candidates"] = None
    if candidates:
        matched = next((c for c in candidates if c.get("graph_id") == graph_id), None)
        if matched and matched.get("name"):
            new_filters["speaker_name"] = matched["name"]
    return RouterDecision(
        route=decision.route,
        filters=new_filters,
        scope_intent=dict(decision.scope_intent),
        out_of_window=decision.out_of_window,
        search_query=decision.search_query,
    )
