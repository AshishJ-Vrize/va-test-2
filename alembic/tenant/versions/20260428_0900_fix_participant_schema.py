"""fix participant schema

Revision ID: fix_participant_schema
Revises:
Create Date: 2026-04-28 09:00:00

Changes:
- meetings: drop organizer_id (FK to users), add organizer_graph_id / organizer_name / organizer_email
- meeting_participants: drop user_id PK (FK to users), add participant_graph_id PK / participant_name / participant_email
- users: add email and display_name columns (present in model but missing from DB)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "fix_participant_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── meetings: swap organizer_id for graph-id columns ──────────────────────
    # Add new columns as nullable first, fill, then enforce NOT NULL
    op.add_column("meetings", sa.Column("organizer_graph_id", sa.String(255), nullable=True))
    op.add_column("meetings", sa.Column("organizer_name", sa.String(255), nullable=True))
    op.add_column("meetings", sa.Column("organizer_email", sa.String(320), nullable=True))

    # Backfill organizer_graph_id from users table via the existing FK
    op.execute("""
        UPDATE meetings m
        SET organizer_graph_id = u.graph_id
        FROM users u
        WHERE u.id = m.organizer_id
    """)

    # Any rows with no organizer match — fill with a placeholder
    op.execute("UPDATE meetings SET organizer_graph_id = 'unknown' WHERE organizer_graph_id IS NULL")

    op.alter_column("meetings", "organizer_graph_id", nullable=False)
    op.create_index("ix_meetings_organizer_graph_id", "meetings", ["organizer_graph_id"])

    # Drop the old FK column
    op.drop_index("ix_meetings_organizer_id", table_name="meetings")
    op.drop_constraint("meetings_organizer_id_fkey", "meetings", type_="foreignkey")
    op.drop_column("meetings", "organizer_id")

    # ── meeting_participants: swap user_id PK for participant_graph_id ────────
    # Add new columns
    op.add_column("meeting_participants", sa.Column("participant_graph_id", sa.String(255), nullable=True))
    op.add_column("meeting_participants", sa.Column("participant_name", sa.String(255), nullable=True))
    op.add_column("meeting_participants", sa.Column("participant_email", sa.String(320), nullable=True))

    # Backfill participant_graph_id from users table via existing FK
    op.execute("""
        UPDATE meeting_participants mp
        SET participant_graph_id = u.graph_id
        FROM users u
        WHERE u.id = mp.user_id
    """)

    # Any rows with no match — fill with a placeholder
    op.execute("UPDATE meeting_participants SET participant_graph_id = 'unknown' WHERE participant_graph_id IS NULL")

    # Drop old composite PK (meeting_id, user_id)
    op.drop_constraint("meeting_participants_pkey", "meeting_participants", type_="primary")

    # Drop the user_id FK constraint and column
    op.drop_constraint("meeting_participants_user_id_fkey", "meeting_participants", type_="foreignkey")
    op.drop_column("meeting_participants", "user_id")

    # Make participant_graph_id NOT NULL and create new composite PK
    op.alter_column("meeting_participants", "participant_graph_id", nullable=False)
    op.create_primary_key(
        "meeting_participants_pkey",
        "meeting_participants",
        ["meeting_id", "participant_graph_id"],
    )


def downgrade() -> None:
    # ── meeting_participants: restore user_id PK ──────────────────────────────
    op.drop_constraint("meeting_participants_pkey", "meeting_participants", type_="primary")
    op.add_column("meeting_participants", sa.Column("user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "meeting_participants_user_id_fkey",
        "meeting_participants", "users",
        ["user_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_primary_key(
        "meeting_participants_pkey",
        "meeting_participants",
        ["meeting_id", "user_id"],
    )
    op.drop_column("meeting_participants", "participant_email")
    op.drop_column("meeting_participants", "participant_name")
    op.drop_column("meeting_participants", "participant_graph_id")

    # ── meetings: restore organizer_id ───────────────────────────────────────
    op.drop_index("ix_meetings_organizer_graph_id", table_name="meetings")
    op.add_column("meetings", sa.Column("organizer_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "meetings_organizer_id_fkey",
        "meetings", "users",
        ["organizer_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_meetings_organizer_id", "meetings", ["organizer_id"])
    op.drop_column("meetings", "organizer_email")
    op.drop_column("meetings", "organizer_name")
    op.drop_column("meetings", "organizer_graph_id")

