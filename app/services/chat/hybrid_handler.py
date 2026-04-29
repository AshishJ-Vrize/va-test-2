"""HYBRID route handler — STRUCTURED + SEARCH in parallel via asyncio.gather()."""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.structured_handler import handle_structured
from app.services.chat.search_handler import handle_search


async def handle_hybrid(
    query_embedding: list[float],
    query_text: str,
    authorized_meeting_ids: list[uuid.UUID],
    filters: dict,
    db: AsyncSession,
) -> list[dict]:
    """
    Run STRUCTURED and SEARCH concurrently, merge results.
    Insights come first in the merged list (for prompt ordering).
    If insights are empty, only chunks are returned — no fallthrough needed.
    """
    (insights, _fell), chunks = await asyncio.gather(
        handle_structured(authorized_meeting_ids, filters, db),
        handle_search(query_embedding, query_text, authorized_meeting_ids, filters, db),
    )
    return insights + chunks
