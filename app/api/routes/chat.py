"""POST /chat — conversational RAG endpoint (RAG v3).

Owns the request transaction. Constructs concrete repos/clients and hands
them to `handle_chat()`. Translates the orchestrator's `OrchestratorResult`
into the wire-shape `ChatResponse`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_feature
from app.core.security import CurrentUser
from app.services.chat.orchestrator import OrchestratorResult, handle_chat
from app.services.chat.repos.chunk_searcher import HybridChunkSearcher
from app.services.chat.repos.insights_repo import InsightsRepoImpl
from app.services.chat.repos.metadata_repo import MetadataRepoImpl
from app.services.chat.repos.speaker_resolver import TenantSpeakerResolver
from app.services.chat.session import get_session_store
from app.services.chat.sources import SourceCard
from app.services.ingestion.embedder import embed_single
from app.services.llm.client import get_llm_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


# ── Wire-shape models ─────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    selected_meeting_ids: list[uuid.UUID] | None = None
    access_filter: Literal["all", "attended", "granted"] = "all"
    session_id: uuid.UUID | None = None


class TimespanOut(BaseModel):
    start_ms: int
    end_ms: int


class SourceOut(BaseModel):
    meeting_id: uuid.UUID
    meeting_title: str
    meeting_date: datetime | None = None
    source_type: str
    speakers: list[str] = []
    timespans: list[TimespanOut] = []


class ScopeChangeOut(BaseModel):
    new_meeting_ids: list[uuid.UUID]
    reason: str


class RbacScopeInfoOut(BaseModel):
    """Lets the UI render banners like 'showing 30 of 87 meetings'."""
    total: int                 # meetings the user can access in the date window
    visible: int               # meetings actually searchable (after count cap)
    capped: bool               # True iff the count cap is biting
    within_days: int           # CHAT_RBAC_WITHIN_DAYS at request time
    max_meetings: int          # CHAT_RBAC_MAX_MEETINGS at request time


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    answer: str
    route: str
    sources: list[SourceOut] = []
    scope_change: ScopeChangeOut | None = None
    out_of_window: bool = False         # query references a date older than CHAT_RBAC_WITHIN_DAYS
    speaker_disambiguation: list[dict] | None = None
    rbac_scope_info: RbacScopeInfoOut | None = None


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    current_user: CurrentUser = Depends(require_feature("chat")),
    db: AsyncSession = Depends(get_tenant_db),
) -> ChatResponse:
    """Run a single chat turn against the user's authorised meetings."""
    # Construct dependencies — one set per request, sharing the tenant DB session.
    metadata_repo = MetadataRepoImpl(db)
    insights_repo = InsightsRepoImpl(db)
    chunk_searcher = HybridChunkSearcher(db)
    speaker_resolver = TenantSpeakerResolver(db)
    llm = get_llm_client()
    session_store = get_session_store()

    result: OrchestratorResult = await handle_chat(
        query=body.query,
        request_meeting_ids=body.selected_meeting_ids,
        access_filter=body.access_filter,
        session_id=body.session_id,
        current_user_graph_id=current_user.graph_id,
        db=db,
        llm=llm,
        metadata_repo=metadata_repo,
        insights_repo=insights_repo,
        chunk_searcher=chunk_searcher,
        speaker_resolver=speaker_resolver,
        session_store=session_store,
        embed=embed_single,
    )

    return _to_wire(result)


# ── Translation: domain → wire ────────────────────────────────────────────────

def _to_wire(result: OrchestratorResult) -> ChatResponse:
    return ChatResponse(
        session_id=result.session_id,
        answer=result.answer,
        route=result.route,
        sources=[_source_to_wire(s) for s in result.sources],
        scope_change=(
            ScopeChangeOut(
                new_meeting_ids=result.scope_change.new_meeting_ids,
                reason=result.scope_change.reason,
            )
            if result.scope_change else None
        ),
        out_of_window=result.out_of_window,
        speaker_disambiguation=result.speaker_disambiguation,
        rbac_scope_info=(
            RbacScopeInfoOut(
                total=result.rbac_scope_info.total,
                visible=result.rbac_scope_info.visible,
                capped=result.rbac_scope_info.capped,
                within_days=result.rbac_scope_info.within_days,
                max_meetings=result.rbac_scope_info.max_meetings,
            )
            if result.rbac_scope_info else None
        ),
    )


def _source_to_wire(s: SourceCard) -> SourceOut:
    return SourceOut(
        meeting_id=s.meeting_id,
        meeting_title=s.meeting_title,
        meeting_date=s.meeting_date,
        source_type=s.source_type,
        speakers=list(s.speakers),
        timespans=[TimespanOut(start_ms=t.start_ms, end_ms=t.end_ms) for t in s.timespans],
    )
