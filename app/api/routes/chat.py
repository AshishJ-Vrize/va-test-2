"""POST /chat — conversational RAG interface."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, require_feature
from app.core.security import CurrentUser
from app.services.chat.answer import _ms_to_display

router = APIRouter()


# ── Request / Response models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    meeting_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


class Source(BaseModel):
    source_type: str          # "metadata" | "insights" | "transcript"
    meeting_id: uuid.UUID
    meeting_title: str
    meeting_date: str | None = None
    speaker_name: str | None = None
    timestamp_ms: int | None = None
    timestamp_display: str | None = None
    similarity_score: float | None = None


class ChatResponse(BaseModel):
    message_id: uuid.UUID
    answer: str
    route: str                # META | STRUCTURED | SEARCH | HYBRID
    fallthrough: bool = False
    sources: list[Source]
    session_id: uuid.UUID


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    current_user: CurrentUser = Depends(require_feature("chat")),
    db: AsyncSession = Depends(get_tenant_db),
) -> ChatResponse:
    """
    Conversational RAG endpoint.

    Routes the query through META / STRUCTURED / SEARCH / HYBRID automatically.
    Provide `meeting_id` to scope the search to a specific meeting.
    Provide `session_id` to continue an existing conversation.
    """
    # Imported here to avoid circular imports at module level
    from app.services.chat.router import classify_query
    from app.services.chat.meta_handler import handle_meta
    from app.services.chat.structured_handler import handle_structured
    from app.services.chat.search_handler import handle_search
    from app.services.chat.hybrid_handler import handle_hybrid
    from app.services.chat.orchestrator import get_authorized_meeting_ids, _get_or_create_session, _load_history
    from app.services.ingestion.embedder import embed_single
    from app.services.chat.answer import generate_answer
    from app.db.tenant.models import ChatMessage

    # 1. RBAC — compute once, pass to every handler
    authorized_ids = await get_authorized_meeting_ids(current_user.id, db)
    if not authorized_ids:
        return ChatResponse(
            message_id=uuid.uuid4(),
            answer="You haven't attended any meetings yet.",
            route="META",
            fallthrough=False,
            sources=[],
            session_id=body.session_id or uuid.uuid4(),
        )

    # 2. Session
    session = await _get_or_create_session(
        current_user.id, body.meeting_id, body.session_id, db
    )

    # 3. Route classification
    classification = await classify_query(body.query)
    route = classification["route"]
    filters = classification["filters"]
    search_query = classification["search_query"]

    # Scope meeting filter when a specific meeting is requested
    if body.meeting_id is not None:
        scoped_ids = [body.meeting_id] if body.meeting_id in authorized_ids else []
        if not scoped_ids:
            raise HTTPException(status_code=403, detail="Not a participant of this meeting.")
    else:
        scoped_ids = authorized_ids

    # 4. Embed query (not needed for META — skip to save an API call)
    query_embedding: list[float] = []
    if route != "META":
        query_embedding = await embed_single(search_query)

    # 5. Dispatch to handler
    fallthrough = False
    if route == "META":
        result = await handle_meta(scoped_ids, filters, db)
    elif route == "STRUCTURED":
        result, fell = await handle_structured(scoped_ids, filters, db)
        fallthrough = fell
        if fallthrough:
            route = "SEARCH"
            result = await handle_search(query_embedding, search_query, scoped_ids, filters, db)
    elif route == "SEARCH":
        result = await handle_search(query_embedding, search_query, scoped_ids, filters, db)
    else:  # HYBRID
        result = await handle_hybrid(query_embedding, search_query, scoped_ids, filters, db)

    # 6. History
    history = await _load_history(session.id, db)

    # 7. Generate answer
    answer = await generate_answer(
        query=body.query,
        route=route,
        handler_result=result,
        history=history,
    )

    # 8. Build sources (max 5, deduplicated by meeting_id)
    sources = _build_sources(result, route)

    # 9. Persist messages
    message_id = uuid.uuid4()
    db.add(ChatMessage(session_id=session.id, role="user", content=body.query))
    db.add(
        ChatMessage(
            session_id=session.id,
            role="assistant",
            content=answer,
            citations=[s.model_dump() for s in sources],
        )
    )
    await db.flush()
    await db.commit()

    return ChatResponse(
        message_id=message_id,
        answer=answer,
        route=route,
        fallthrough=fallthrough,
        sources=sources,
        session_id=session.id,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_sources(result: list[dict], route: str) -> list[Source]:
    """Deduplicate by meeting_id, keep best score, cap at 5."""
    seen: dict[str, Source] = {}
    for item in result:
        mid = str(item.get("meeting_id", ""))
        score = item.get("similarity_score")
        existing = seen.get(mid)
        if existing and existing.similarity_score is not None:
            if score is None or score <= existing.similarity_score:
                continue

        source_type = item.get("source_type", "transcript")
        seen[mid] = Source(
            source_type=source_type,
            meeting_id=item["meeting_id"],
            meeting_title=item.get("meeting_title") or item.get("meeting_subject") or "",
            meeting_date=item.get("meeting_date"),
            speaker_name=item.get("speaker_name") or item.get("speaker"),
            timestamp_ms=item.get("timestamp_ms") or item.get("start_ms"),
            timestamp_display=_ms_to_display(item.get("timestamp_ms") or item.get("start_ms")),
            similarity_score=score,
        )

    ranked = sorted(seen.values(), key=lambda s: s.similarity_score or 0, reverse=True)
    return ranked[:5]
