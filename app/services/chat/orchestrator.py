"""Chat session helpers — RBAC gate, session resolution, history loader."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tenant.models import ChatMessage, ChatSession

log = logging.getLogger(__name__)

_MAX_HISTORY_TURNS = 10  # last 10 user+assistant pairs = 20 messages


async def get_authorized_meeting_ids(user_id: uuid.UUID, db: AsyncSession) -> list[uuid.UUID]:
    """Return meeting IDs the user personally attended — the RBAC gate for all handlers."""
    from sqlalchemy import text

    result = await db.execute(
        text("SELECT DISTINCT meeting_id FROM meeting_participants WHERE user_id = :uid"),
        {"uid": user_id},
    )
    return [row[0] for row in result]


async def _get_or_create_session(
    user_id: uuid.UUID,
    meeting_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    db: AsyncSession,
) -> ChatSession:
    if session_id is not None:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.user_id == user_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

    session = ChatSession(user_id=user_id, meeting_id=meeting_id)
    db.add(session)
    await db.flush()
    return session


async def _load_history(session_id: uuid.UUID, db: AsyncSession) -> list[dict[str, str]]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(desc(ChatMessage.created_at))
        .limit(_MAX_HISTORY_TURNS * 2)
    )
    messages = list(reversed(result.scalars().all()))
    return [{"role": m.role, "content": m.content} for m in messages]
