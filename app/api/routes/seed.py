"""Temporary one-shot seeding endpoint. Remove after use."""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, get_current_user
from app.core.security import CurrentUser

router = APIRouter(tags=["seed"])

FEATURES = ["chat", "insights_view", "sentiment_view"]
ROLES    = ["admin", "user", "member", "viewer"]


@router.post("/admin/seed-features", summary="Seed feature permissions (run once, then remove)")
async def seed_features(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict:
    inserted = []
    for feature in FEATURES:
        for role in ROLES:
            await db.execute(
                text("""
                    INSERT INTO feature_permissions
                        (target_type, target_id, feature_key, permission)
                    VALUES ('role', :role, :feature, 'allow')
                    ON CONFLICT (target_type, target_id, feature_key) DO NOTHING
                """),
                {"role": role, "feature": feature},
            )
            inserted.append(f"{feature}→{role}")
    return {"seeded": inserted}


@router.post("/admin/fix-chat-sessions", summary="Drop NOT NULL on chat_sessions.meeting_id (run once)")
async def fix_chat_sessions(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict:
    await db.execute(
        text("ALTER TABLE chat_sessions ALTER COLUMN meeting_id DROP NOT NULL")
    )
    await db.commit()
    return {"status": "ok", "detail": "chat_sessions.meeting_id is now nullable"}


@router.post("/admin/apply-rag-migration", summary="Apply RAG migration 20260428_0001 (run once)")
async def apply_rag_migration(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict:
    applied = []

    # Add contextual_text if missing
    result = await db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='chunks' AND column_name='contextual_text'"
    ))
    if not result.scalar():
        await db.execute(text("ALTER TABLE chunks ADD COLUMN contextual_text TEXT"))
        applied.append("chunks.contextual_text")

    # Add embedding_version if missing
    result = await db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='chunks' AND column_name='embedding_version'"
    ))
    if not result.scalar():
        await db.execute(text(
            "ALTER TABLE chunks ADD COLUMN embedding_version SMALLINT NOT NULL DEFAULT 1"
        ))
        applied.append("chunks.embedding_version")

    # Add text_tsv if missing
    result = await db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='chunks' AND column_name='text_tsv'"
    ))
    if not result.scalar():
        await db.execute(text(
            "ALTER TABLE chunks ADD COLUMN text_tsv tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
        ))
        applied.append("chunks.text_tsv")

    # Create meeting_summaries if missing
    result = await db.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name='meeting_summaries'"
    ))
    if not result.scalar():
        await db.execute(text("""
            CREATE TABLE meeting_summaries (
                meeting_id UUID NOT NULL PRIMARY KEY
                    REFERENCES meetings(id) ON DELETE CASCADE,
                summary_text TEXT NOT NULL,
                embedding vector(1536),
                topics TEXT[] NOT NULL DEFAULT '{}',
                generated_by VARCHAR(100) NOT NULL,
                generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """))
        applied.append("meeting_summaries table")

    # Add GIN index on text_tsv if missing (needed for BM25 performance)
    result = await db.execute(text(
        "SELECT 1 FROM pg_indexes WHERE tablename='chunks' AND indexname='idx_chunks_text_tsv'"
    ))
    if not result.scalar():
        await db.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_chunks_text_tsv ON chunks USING GIN (text_tsv)"
        ))
        applied.append("idx_chunks_text_tsv GIN index")

    await db.commit()
    return {"status": "ok", "applied": applied}
