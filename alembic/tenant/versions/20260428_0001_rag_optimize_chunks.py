"""RAG optimisation: contextual_text + embedding_version on chunks, meeting_summaries table.

Revision ID: 20260428_0001
Revises:
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20260428_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── chunks: new RAG columns ───────────────────────────────────────────────
    op.add_column("chunks", sa.Column("contextual_text", sa.Text(), nullable=True))
    op.add_column(
        "chunks",
        sa.Column(
            "embedding_version",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
    )
    # Generated tsvector column for BM25 hybrid retrieval.
    # STORED means Postgres keeps it on disk; no write needed from application code.
    op.execute("""
        ALTER TABLE chunks
        ADD COLUMN text_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
    """)

    # ── meeting_summaries ─────────────────────────────────────────────────────
    op.create_table(
        "meeting_summaries",
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "topics",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("generated_by", sa.String(100), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["meeting_id"],
            ["meetings.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("meeting_id"),
    )


def downgrade() -> None:
    op.drop_table("meeting_summaries")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS text_tsv")
    op.drop_column("chunks", "embedding_version")
    op.drop_column("chunks", "contextual_text")
