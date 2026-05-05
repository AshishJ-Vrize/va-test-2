from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "system_role IN ('user','admin','compliance_officer')",
            name="ck_users_system_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    graph_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    system_role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        CheckConstraint(
            "ingestion_source IN ('manual','webhook')",
            name="ck_meetings_ingestion_source",
        ),
        CheckConstraint(
            "status IN ('pending','ingesting','ready','failed')",
            name="ck_meetings_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_graph_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    organizer_graph_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    organizer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    organizer_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    meeting_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    meeting_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    meeting_end_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    join_url: Mapped[Optional[str]] = mapped_column(String(2000), unique=True, nullable=True)
    ingestion_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MeetingParticipant(Base):
    """Composite PK on (meeting_id, participant_graph_id) — one row per participant per meeting."""

    __tablename__ = "meeting_participants"
    __table_args__ = (
        CheckConstraint(
            "role IN ('organizer','attendee','granted')",
            name="ck_meeting_participants_role",
        ),
    )

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    participant_graph_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    participant_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    participant_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    granted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    word_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Chunk(Base):
    """
    Multi-turn text chunk for hybrid RAG retrieval.

    A chunk groups several consecutive utterances (5-15 turns, ~20-60s span)
    so a single chunk can contain multiple speakers. Speaker attribution lives
    inside `chunk_text` per turn; `speakers` and `speaker_graph_ids` are
    indexed projections for fast filtering.

    Columns
    -------
    meeting_id        : Denormalised FK to meetings(id). Used for meeting-scoped
                        and cross-meeting filtering directly, avoiding the
                        chunks→transcripts→meetings join at query time.
    transcript_id     : FK to transcripts(id). Retained for re-ingestion
                        cleanup (DELETE WHERE transcript_id = X).
    chunk_index       : Zero-based ordinal within the meeting.
    start_ms / end_ms : Chunk time-span in milliseconds. Seconds derived at
                        JSON-write time for chunk_text[].st / .et.
    speakers          : Deduped first-name array per chunk, e.g. ['Ashish','Rahul'].
                        GIN-indexed; speaker filters use `speakers && ARRAY[:name]`.
    speaker_graph_ids : Microsoft Graph IDs aligned by index with `speakers`.
                        NULL slot when name didn't resolve to a participant.
                        Used for cross-meeting per-person identity.
    chunk_text        : JSONB array of utterance objects:
                        [{n: full_name, sn: short_name, t: text, st: sec, et: sec}, ...]
    chunk_context     : Optional short topic string (currently unused; populated
                        later by contextualizer).
    search_text       : Lowercased flat string concatenating speakers + utterance
                        tokens, punctuation stripped. Source of search_vector.
    search_vector     : Generated tsvector — DO NOT write to it. Source of BM25.
    embedding         : pgvector(1536). Initially NULL after chunk insert; the
                        pipeline updates it after embedding the per-chunk
                        conversational input. HNSW index created in migration
                        20260428_0002, retained as-is.
    embedding_version : Bump when embedding recipe changes; pipeline writes
                        the current value (currently 2 for the v2 schema).
    """

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    speakers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    speaker_graph_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    chunk_text: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    chunk_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    search_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(search_text, ''))", persisted=True),
        nullable=True,
    )
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(1536), nullable=True)
    embedding_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=2, server_default="2"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MeetingSummary(Base):
    """
    Precomputed meeting-level summary with embedding for cross-meeting RAG search.
    One row per meeting. Upserted at the end of each ingestion pipeline run.
    """

    __tablename__ = "meeting_summaries"

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(1536), nullable=True)
    topics: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    generated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MeetingInsight(Base):
    __tablename__ = "meeting_insights"
    __table_args__ = (
        CheckConstraint(
            "insight_type IN ('summary','action_items','key_topics','sentiment_overview')",
            name="ck_meeting_insights_type",
        ),
        UniqueConstraint("meeting_id", "insight_type", name="uq_meeting_insights_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    insight_type: Mapped[str] = mapped_column(String(50), nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SpeakerAnalytic(Base):
    __tablename__ = "speaker_analytics"
    __table_args__ = (
        UniqueConstraint("meeting_id", "user_id", name="uq_speaker_analytics_meeting_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    speaker_label: Mapped[str] = mapped_column(String(255), nullable=False)
    talk_time_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sentiment_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VideoAnalysis(Base):
    __tablename__ = "video_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    blob_url: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    analysis_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Rule(Base):
    __tablename__ = "rules"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','inactive','draft')",
            name="ck_rules_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RuleVersion(Base):
    """Append-only audit trail — rows are never updated or deleted."""

    __tablename__ = "rule_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RuleViolation(Base):
    __tablename__ = "rule_violations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rules.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rule_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditUsage(Base):
    """Append-only ledger — rows are never updated or deleted."""

    __tablename__ = "credit_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    credits_consumed: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FeaturePermission(Base):
    """
    target_id is plain String(255), not a FK.
    It holds either a user UUID string (target_type='user')
    or a role name string (target_type='role') — polymorphic reference.
    """

    __tablename__ = "feature_permissions"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('user','role')",
            name="ck_feature_permissions_target_type",
        ),
        CheckConstraint(
            "feature_key IN ('chat','rules_management','insights_view','sentiment_view',"
            "'video_analytics','compliance_dashboard','user_management')",
            name="ck_feature_permissions_feature_key",
        ),
        CheckConstraint(
            "permission IN ('allow','deny')",
            name="ck_feature_permissions_permission",
        ),
        UniqueConstraint(
            "target_type", "target_id", "feature_key",
            name="uq_feature_permissions_target_feature",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(50), nullable=False)
    permission: Mapped[str] = mapped_column(String(10), nullable=False)
    granted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# NOTE: ChatSession + ChatMessage models were removed when the RAG/chat layer
# was deleted. The corresponding chat_sessions / chat_messages tables may still
# exist in databases that were bootstrapped before this change — they are now
# orphaned (no code references them) and can be dropped manually if desired.
