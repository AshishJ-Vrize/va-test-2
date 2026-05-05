"""Temporary one-shot seeding endpoint for feature permissions.

Stripped of chat/RAG-specific helpers (fix_chat_sessions, apply_rag_migration)
when the chat layer was removed.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_tenant_db, get_current_user
from app.core.security import CurrentUser

router = APIRouter(tags=["seed"])

# 'chat' is intentionally omitted now that the chat endpoint is gone.
# Existing rows with feature_key='chat' remain in the DB but are inert.
FEATURES = ["insights_view", "sentiment_view"]
ROLES    = ["admin", "user", "member", "viewer"]


@router.post("/admin/seed-features", summary="Seed feature permissions (run once)")
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
