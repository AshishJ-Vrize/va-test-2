"""DB helpers for meeting_insights — save_insights, get_insights_for_meeting."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tenant.models import MeetingInsight


async def save_insights(db: AsyncSession, meeting_id: uuid.UUID, insights: dict) -> None:
    """
    Upsert meeting insights into meeting_insights table.
    Each insight type is stored as a separate row (matching the existing schema).
    insight_types: summary, action_items, key_decisions (→ key_topics), follow_ups (→ sentiment_overview skipped)
    """
    type_map = {
        "summary": "summary",
        "action_items": "action_items",
        "key_decisions": "key_topics",
    }

    for field_key, insight_type in type_map.items():
        value = insights.get(field_key)
        if value is None:
            continue

        fields_value = {"text": value} if isinstance(value, str) else {"items": value}

        result = await db.execute(
            select(MeetingInsight).where(
                MeetingInsight.meeting_id == meeting_id,
                MeetingInsight.insight_type == insight_type,
            )
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            existing.fields = fields_value
        else:
            db.add(MeetingInsight(
                meeting_id=meeting_id,
                insight_type=insight_type,
                fields=fields_value,
            ))

    await db.flush()


async def get_insights_for_meeting(db: AsyncSession, meeting_id: uuid.UUID) -> dict:
    """Return all insight rows for a meeting merged into a single dict."""
    result = await db.execute(
        select(MeetingInsight).where(MeetingInsight.meeting_id == meeting_id)
    )
    rows = result.scalars().all()
    return {row.insight_type: row.fields for row in rows}


async def has_insights(db: AsyncSession, meeting_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(MeetingInsight.id).where(MeetingInsight.meeting_id == meeting_id).limit(1)
    )
    return result.scalar_one_or_none() is not None
