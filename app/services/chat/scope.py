"""Scope resolution & narrowing.

Three responsibilities, each a pure-as-possible function over the router's
output and the user's current selection:

  - resolve_effective_scope() — start from session selection; if router signals
    a change, try to interpret the change. Caller (orchestrator) ultimately
    decides whether to ASK the user (banner) or just narrow silently.

  - narrow_within_scope() — given currently-selected meetings + a request
    referencing specific meetings (by title or date), partition selected
    meetings into matched / dropped, and surface any extras the user did NOT
    select but that the request explicitly mentions.

  - detect_scope_change_suggestion() — given a NarrowResult + RouterDecision,
    decide whether to surface the "Want me to expand to <X>?" banner.

The router-side scope_intent flag is just an LLM hunch. The authoritative
scope decision happens here, with access to actual DB facts via MetadataRepo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.services.chat.interfaces import MetadataRepo, RouterDecision


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class NarrowResult:
    """Output of narrow_within_scope().

    matched_ids — selected meetings that match the request (use these for search)
    dropped_ids — selected meetings irrelevant to this query (excluded)
    extra_ids   — meetings matching the request but NOT in the selection
                  (these are candidates for scope expansion — surface to user)
    narrowed    — True if the request actually narrowed the scope (request
                  referenced specific titles/dates); False if no narrowing
                  was requested (matched_ids == selected_ids)
    """
    matched_ids: list[UUID] = field(default_factory=list)
    dropped_ids: list[UUID] = field(default_factory=list)
    extra_ids: list[UUID] = field(default_factory=list)
    narrowed: bool = False


@dataclass
class ScopeChangeSuggestion:
    """Output of detect_scope_change_suggestion().

    surface — True when the orchestrator should render the banner to the user.
    new_meeting_ids — proposed scope on user's confirmation (selected ∪ extras).
    reason — human-readable banner text.
    """
    surface: bool
    new_meeting_ids: list[UUID] = field(default_factory=list)
    reason: str = ""


# ── Pure helpers ──────────────────────────────────────────────────────────────

async def narrow_within_scope(
    *,
    selected_ids: list[UUID],
    requested_titles: list[str] | None,
    date_from: str | None,
    date_to: str | None,
    tenant_30d_meeting_ids: list[UUID],
    metadata_repo: "MetadataRepo",
) -> NarrowResult:
    """Find which meetings the request actually targets.

    Reasoning:
      - If the user said neither titles nor dates → no narrowing; everything
        selected stays in scope.
      - If the user named titles or a date range → compute the set of meetings
        in the tenant's 30-day window that match. Then partition the user's
        current selection into matched/dropped and gather extras.
    """
    if not requested_titles and not date_from and not date_to:
        # Nothing to narrow on. Selected meetings are the matched set.
        return NarrowResult(
            matched_ids=list(selected_ids),
            dropped_ids=[],
            extra_ids=[],
            narrowed=False,
        )

    # Find all meetings matching the request, scoped to the tenant's 30-day window.
    candidates: set[UUID] = set()

    if requested_titles:
        title_hits = await metadata_repo.search_by_title(
            candidate_titles=requested_titles,
            allowed_meeting_ids=tenant_30d_meeting_ids,
        )
        candidates.update(title_hits)

    if date_from or date_to:
        # MetadataRepo.get_meetings_in_date_range exists on the impl but isn't
        # part of the Protocol — call via duck-typing for now. The Phase 3.2
        # pass will lift it to the Protocol if needed.
        date_hits = await metadata_repo.get_meetings_in_date_range(
            date_from=date_from,
            date_to=date_to,
            allowed_meeting_ids=tenant_30d_meeting_ids,
        )
        # If both titles AND dates were given, AND them — the user wants meetings
        # matching BOTH constraints. If only one was given, use it standalone.
        if requested_titles:
            candidates &= set(date_hits)
        else:
            candidates.update(date_hits)

    selected_set = set(selected_ids)
    matched = candidates & selected_set
    dropped = selected_set - candidates
    extras = candidates - selected_set

    return NarrowResult(
        matched_ids=sorted(matched, key=str),
        dropped_ids=sorted(dropped, key=str),
        extra_ids=sorted(extras, key=str),
        narrowed=True,
    )


def detect_scope_change_suggestion(
    *,
    narrow_result: NarrowResult,
    router_decision: "RouterDecision",
    selected_ids: list[UUID],
    auto_expanded: bool = False,
) -> ScopeChangeSuggestion:
    """Decide whether to surface a "scope change" banner.

    Two flavours of banner:
      - Suggestion ("Want me to include …?") — extras exist alongside matches
        in the user's selection. The user can opt in.
      - Disclosure ("I included … because your selection had no match.") — the
        orchestrator already auto-expanded; tell the user what we did.
    """
    extras = narrow_result.extra_ids
    if not extras:
        return ScopeChangeSuggestion(surface=False)

    new_scope = sorted(set(selected_ids) | set(extras), key=str)
    n_extras = len(extras)
    meeting_word = "meeting" if n_extras == 1 else "meetings"

    if auto_expanded:
        # Selection had no match — orchestrator silently widened to extras.
        reason = (
            f"Your selection didn't include the {meeting_word} you referenced, "
            f"so I answered using the {n_extras} matching {meeting_word} "
            f"instead. Reselect if you'd like a different scope."
        )
    elif narrow_result.narrowed:
        reason = (
            f"Found {n_extras} matching {meeting_word} outside your current "
            f"selection. Want me to include {'it' if n_extras == 1 else 'them'}?"
        )
    else:
        reason = router_decision.scope_intent.get("reason") or (
            f"This question covers {n_extras} additional {meeting_word}. "
            f"Want me to expand the scope?"
        )

    return ScopeChangeSuggestion(
        surface=True,
        new_meeting_ids=new_scope,
        reason=reason,
    )
