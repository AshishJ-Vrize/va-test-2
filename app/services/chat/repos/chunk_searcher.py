"""HybridChunkSearcher — production ChunkSearcher implementation.

Thin wrapper around `app.db.helpers.chat_search.hybrid_chunk_search()` that
makes it conform to the ChunkSearcher Protocol and accepts a `filters` dict
with a stable shape (matching the router's filter contract).

Tests inject a fake ChunkSearcher; production wires this class with a live
`AsyncSession`.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.helpers.chat_search import hybrid_chunk_search
from app.services.chat.interfaces import RetrievedChunk


class HybridChunkSearcher:
    """Default ChunkSearcher implementation."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def hybrid_search(
        self,
        query_embedding: list[float],
        query_text: str,
        meeting_ids: list[UUID],
        filters: dict[str, Any],
        top_k: int = 10,
    ) -> list[RetrievedChunk]:
        return await hybrid_chunk_search(
            db=self._db,
            query_embedding=query_embedding,
            query_text=query_text,
            meeting_ids=meeting_ids,
            speaker_graph_ids=filters.get("speaker_graph_ids"),
            top_k=top_k,
        )
