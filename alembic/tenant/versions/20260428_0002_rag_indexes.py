"""RAG indexes: GIN on text_tsv, HNSW on chunks.embedding and meeting_summaries.embedding.

Revision ID: 20260428_0002
Revises: 20260428_0001
Create Date: 2026-04-28

NOTE: All indexes use CREATE INDEX CONCURRENTLY so this migration must be run
OUTSIDE a transaction block.  The alembic env is configured with
transaction_per_migration=True; pass --no-transaction flag or run manually:

    psql <tenant-db-url> -c "CREATE INDEX CONCURRENTLY ..."

for production tenants where the chunks table already has data.
"""
from __future__ import annotations

from alembic import op

revision = "20260428_0002"
down_revision = "20260428_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GIN index for BM25 full-text search on chunks.text_tsv.
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS chunks_text_tsv_gin "
        "ON chunks USING GIN (text_tsv)"
    )

    # HNSW index for approximate nearest-neighbour vector search on chunks.
    # m=16 / ef_construction=64 is the pgvector recommended default for recall/speed.
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS chunks_embedding_hnsw "
        "ON chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # HNSW on meeting_summaries for cross-meeting search.
    op.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS meeting_summaries_embedding_hnsw "
        "ON meeting_summaries USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS meeting_summaries_embedding_hnsw")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS chunks_embedding_hnsw")
    op.execute("DROP INDEX CONCURRENTLY IF EXISTS chunks_text_tsv_gin")
