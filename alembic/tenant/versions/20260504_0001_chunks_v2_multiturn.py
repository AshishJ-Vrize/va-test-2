"""Chunks v2: multi-turn JSON chunks with hybrid-search columns.

Revision ID: 20260504_0001
Revises: 20260428_0003
Create Date: 2026-05-04

Restructures the chunks table for multi-turn chunks (multiple speakers per chunk)
and explicit hybrid-search columns. Wipes existing chunks — pipeline must
re-ingest meetings from transcripts.raw_text after this migration runs.

Schema changes
--------------
DROP    text                -- replaced by chunk_text (JSONB array of turns)
DROP    speaker             -- replaced by speakers (TEXT[])
DROP    contextual_text     -- replaced by chunk_context
DROP    text_tsv            -- replaced by search_vector (generated from search_text)

ADD     meeting_id          UUID NOT NULL FK→meetings(id) ON DELETE CASCADE
ADD     speakers            TEXT[] NOT NULL DEFAULT '{}'
ADD     speaker_graph_ids   TEXT[] NOT NULL DEFAULT '{}'
ADD     chunk_text          JSONB NOT NULL DEFAULT '[]'::jsonb
ADD     chunk_context       TEXT NULL
ADD     search_text         TEXT NULL
ADD     search_vector       TSVECTOR GENERATED ALWAYS AS
                              (to_tsvector('english', coalesce(search_text,''))) STORED

KEEP    id, transcript_id, chunk_index, start_ms, end_ms,
        embedding, embedding_version, created_at

Indexes added: GIN on search_vector, speakers, speaker_graph_ids;
btree on (meeting_id, start_ms, end_ms). HNSW on embedding kept as-is.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260504_0001"
down_revision = "20260428_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Wipe existing chunks. Multi-turn JSON shape and contextual-vs-search-text
    # split mean old rows can't be coerced — pipeline re-ingests meetings from
    # transcripts.raw_text after this migration runs.
    op.execute("TRUNCATE chunks RESTART IDENTITY CASCADE")

    # ── Drop old indexes tied to columns being removed ────────────────────────
    op.execute("DROP INDEX IF EXISTS chunks_text_tsv_gin")

    # ── Drop generated column first (depends on `text`) ──────────────────────
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS text_tsv")

    # ── Drop replaced columns ─────────────────────────────────────────────────
    op.drop_column("chunks", "text")
    op.drop_column("chunks", "speaker")
    op.drop_column("chunks", "contextual_text")

    # ── Add new columns ───────────────────────────────────────────────────────
    op.add_column(
        "chunks",
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.add_column(
        "chunks",
        sa.Column(
            "speakers",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "chunks",
        sa.Column(
            "speaker_graph_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "chunks",
        sa.Column(
            "chunk_text",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("chunks", sa.Column("chunk_context", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("search_text", sa.Text(), nullable=True))

    # Generated tsvector — STORED so Postgres persists it; no app writes.
    op.execute("""
        ALTER TABLE chunks
        ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(search_text, ''))) STORED
    """)

    # ── Foreign key on meeting_id ─────────────────────────────────────────────
    op.create_foreign_key(
        "chunks_meeting_id_fkey",
        "chunks",
        "meetings",
        ["meeting_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    # Plain CREATE INDEX (not CONCURRENTLY) is fine here because the table was
    # just truncated — no rows, no locking concern.
    op.create_index(
        "ix_chunks_search_vector_gin",
        "chunks",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_chunks_speakers_gin",
        "chunks",
        ["speakers"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_chunks_speaker_graph_ids_gin",
        "chunks",
        ["speaker_graph_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_chunks_meeting_time",
        "chunks",
        ["meeting_id", "start_ms", "end_ms"],
    )


def downgrade() -> None:
    # Wipe again — the v2 multi-turn data can't be coerced back to single-speaker rows.
    op.execute("TRUNCATE chunks RESTART IDENTITY CASCADE")

    op.drop_index("ix_chunks_meeting_time", table_name="chunks")
    op.drop_index("ix_chunks_speaker_graph_ids_gin", table_name="chunks")
    op.drop_index("ix_chunks_speakers_gin", table_name="chunks")
    op.drop_index("ix_chunks_search_vector_gin", table_name="chunks")

    op.drop_constraint("chunks_meeting_id_fkey", "chunks", type_="foreignkey")

    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS search_vector")
    op.drop_column("chunks", "search_text")
    op.drop_column("chunks", "chunk_context")
    op.drop_column("chunks", "chunk_text")
    op.drop_column("chunks", "speaker_graph_ids")
    op.drop_column("chunks", "speakers")
    op.drop_column("chunks", "meeting_id")

    # Restore old columns
    op.add_column(
        "chunks",
        sa.Column("contextual_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "chunks",
        sa.Column("speaker", sa.String(255), nullable=True),
    )
    op.add_column(
        "chunks",
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
    )

    op.execute("""
        ALTER TABLE chunks
        ADD COLUMN text_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS chunks_text_tsv_gin "
        "ON chunks USING GIN (text_tsv)"
    )
