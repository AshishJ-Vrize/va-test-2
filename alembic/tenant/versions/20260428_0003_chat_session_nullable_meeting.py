"""Make chat_sessions.meeting_id nullable for cross-meeting sessions.

Revision ID: 20260428_0003
Revises: 20260428_0002
Create Date: 2026-04-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260428_0003"
down_revision = "20260428_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "chat_sessions",
        "meeting_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    # Will fail if any rows have NULL meeting_id — clear them first.
    op.alter_column(
        "chat_sessions",
        "meeting_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
