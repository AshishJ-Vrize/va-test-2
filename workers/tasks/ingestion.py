# Celery task: ingest_meeting_task
# Owner: Workers team
# Called by: app/services/graph/webhook.py — handle_notification()
# Depends on: app/services/graph/client.py (DELIVERED)
#             app/services/graph/exceptions.py (DELIVERED)
#             app/db/central/models.py (DELIVERED)
#             app/db/central/session.py (DELIVERED)
#             app/db/tenant/models.py (DELIVERED)
#             app/services/ingestion/pipeline.py (DELIVERED)
# See docs/webhook_dependencies.md for full dependency tracking.

import asyncio
import logging
from datetime import datetime, timezone
from math import ceil
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.state import get_db_manager, get_tenant_registry
from app.db.central.models import CreditPricing, Tenant
from app.db.central.session import central_session
from app.db.tenant.models import Meeting, MeetingParticipant, User
from app.services.graph.client import GraphClient, get_access_token_app
from app.services.graph.exceptions import GraphClientError, MeetingNotFoundError, TokenExpiredError
from app.services.ingestion.pipeline import run_ingestion_pipeline
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="workers.tasks.ingestion.ingest_meeting_task",
    bind=True,
    max_retries=10,
    default_retry_delay=300,
)
def ingest_meeting_task(
    self: Task,
    call_chain_id: str,
    org_name: str,
    ms_tenant_id: str | None = None,
) -> None:
    """
    Entry point for meeting ingestion.

    Called by webhook handle_notification() after a Graph callRecords notification.
    Runs the full async ingestion pipeline inside a fresh event loop.

    Args:
        call_chain_id: Graph call chain ID extracted from the notification resource.
        org_name:      Customer org slug — used to look up tenant if ms_tenant_id absent.
        ms_tenant_id:  Customer Azure AD tenant GUID — passed by webhook if present,
                       otherwise looked up from central DB by org_name.

    Retry policy:
        max_retries=10, default_retry_delay=300s (5 min).
        Teams takes 5–10 min to process transcripts after a meeting ends.
        10 × 5 min = up to 50 min of retrying — covers the processing window.
        Individual steps override countdown when a longer delay makes sense.
    """
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_ingest_async(self, call_chain_id, org_name, ms_tenant_id))


async def _ingest_async(
    task: Task,
    call_chain_id: str,
    org_name: str,
    ms_tenant_id: str | None,
) -> None:

    # ── Step 1: resolve ms_tenant_id ─────────────────────────────────────────
    # Webhook passes ms_tenant_id directly from the Graph notification (fast path).
    # If absent, fall back to central DB lookup by org_name.

    if ms_tenant_id is None:
        logger.info(
            "ingest_meeting_task: ms_tenant_id not provided — looking up by org_name | "
            "org=%s | call_chain_id=%s",
            org_name, call_chain_id,
        )
        async with central_session() as db:
            result = await db.execute(
                select(Tenant).where(Tenant.org_name == org_name)
            )
            tenant = result.scalars().first()

        if tenant is None:
            logger.error(
                "ingest_meeting_task: tenant not found in central DB — aborting | "
                "org=%s | call_chain_id=%s",
                org_name, call_chain_id,
            )
            return  # bad data — retrying will not help

        if tenant.status != "active":
            logger.warning(
                "ingest_meeting_task: tenant not active — aborting | "
                "org=%s | status=%s | call_chain_id=%s",
                org_name, tenant.status, call_chain_id,
            )
            return  # not retrying — status won't change on its own

        ms_tenant_id = tenant.ms_tenant_id

    logger.info(
        "ingest_meeting_task: starting | org=%s | tenant=%s | call_chain_id=%s",
        org_name, ms_tenant_id, call_chain_id,
    )

    # ── Step 2: get app-only token ────────────────────────────────────────────
    # MSAL is sync — run in thread pool so we don't block the event loop.

    try:
        access_token = await asyncio.to_thread(get_access_token_app, ms_tenant_id)
    except GraphClientError as exc:
        logger.error(
            "ingest_meeting_task: failed to acquire app token | "
            "org=%s | tenant=%s | error=%s",
            org_name, ms_tenant_id, exc,
        )
        raise task.retry(exc=exc, countdown=300)

    client = GraphClient(access_token)

    # ── Step 3: fetch callRecord from Graph ───────────────────────────────────
    # participants is a regular property on callRecord (not a navigation property)
    # so it is returned in the default response — no $expand needed.
    # All participant graph IDs are collected here to run the platform-user gate
    # (Step 3.5) before any user-scoped Graph calls.

    try:
        call_record = await client.get(
            f"/communications/callRecords/{call_chain_id}",
        )
    except TokenExpiredError as exc:
        logger.error(
            "ingest_meeting_task: app token rejected fetching callRecord | "
            "org=%s | tenant=%s | call_chain_id=%s",
            org_name, ms_tenant_id, call_chain_id,
        )
        raise task.retry(exc=exc, countdown=600)
    except GraphClientError as exc:
        logger.error(
            "ingest_meeting_task: Graph error fetching callRecord | "
            "org=%s | call_chain_id=%s | status=%s | error=%s",
            org_name, call_chain_id, exc.status_code, exc,
        )
        raise task.retry(exc=exc, countdown=300)

    join_web_url: str | None = call_record.get("joinWebUrl")
    organizer_graph_id: str | None = (
        call_record.get("organizer", {}).get("user", {}).get("id")
    )

    if not join_web_url or not organizer_graph_id:
        logger.error(
            "ingest_meeting_task: callRecord missing joinWebUrl or organizer.user.id — aborting | "
            "org=%s | call_chain_id=%s | joinWebUrl=%r | organizer_graph_id=%r",
            org_name, call_chain_id, join_web_url, organizer_graph_id,
        )
        return  # structural issue — retrying won't help

    # Collect every participant graph ID present in the callRecord.
    all_participant_graph_ids: set[str] = {organizer_graph_id}
    for p in call_record.get("participants", []):
        pid: str | None = p.get("identity", {}).get("user", {}).get("id")
        if pid:
            all_participant_graph_ids.add(pid)

    logger.info(
        "ingest_meeting_task: callRecord fetched | org=%s | call_chain_id=%s | "
        "organizer_graph_id=%s | participant_count=%d",
        org_name, call_chain_id, organizer_graph_id, len(all_participant_graph_ids),
    )

    # ── Step 3.5: platform user gate ─────────────────────────────────────────
    # At least one meeting participant must be a platform user (present in the
    # tenant users table) for ingestion to proceed.
    # This user's graph_id is used for all subsequent user-scoped Graph API
    # calls (Steps 4a/4b/4c) — it must belong to a user in the customer's
    # Azure AD tenant, which internal platform users always are.

    db_manager = get_db_manager()
    registry = get_tenant_registry()
    cached_tenant = registry.get(ms_tenant_id)

    if cached_tenant is None:
        async with central_session() as central_db:
            cached_tenant = await registry.refresh_one(ms_tenant_id, central_db)

    if cached_tenant is None:
        logger.error(
            "ingest_meeting_task: tenant not found in registry after refresh — aborting | "
            "org=%s | tenant=%s",
            org_name, ms_tenant_id,
        )
        return

    async with db_manager.get_session(ms_tenant_id, cached_tenant) as tenant_db:
        platform_user_result = await tenant_db.execute(
            select(User)
            .where(
                User.graph_id.in_(all_participant_graph_ids),
                User.is_active.is_(True),
            )
            .limit(1)
        )
        platform_user = platform_user_result.scalar_one_or_none()

    if platform_user is None:
        logger.info(
            "ingest_meeting_task: no platform user found among meeting participants — skipping | "
            "org=%s | call_chain_id=%s | participants_checked=%d",
            org_name, call_chain_id, len(all_participant_graph_ids),
        )
        return

    lookup_user_id: str = platform_user.graph_id
    logger.info(
        "ingest_meeting_task: platform user found — proceeding with ingestion | "
        "org=%s | call_chain_id=%s | lookup_user_id=%s",
        org_name, call_chain_id, lookup_user_id,
    )

    # ── Step 4: fetch transcript VTT from Graph ───────────────────────────────
    # Transcripts live under the online meeting, not the callRecord directly.
    # Verified flow (2026-04-21):
    #   joinWebUrl → get_meeting_by_join_url() → meeting_graph_id
    #   meeting_graph_id → get_transcripts()   → transcript_id
    #   meeting_graph_id + transcript_id → get_transcript_content() → VTT string

    # Step 4a: resolve meeting_graph_id from joinWebUrl
    try:
        meeting = await client.get_meeting_by_join_url(
            join_web_url, user_id=lookup_user_id
        )
    except MeetingNotFoundError as exc:
        logger.error(
            "ingest_meeting_task: online meeting not found for joinWebUrl — aborting | "
            "org=%s | call_chain_id=%s | error=%s",
            org_name, call_chain_id, exc,
        )
        return  # URL is stale or meeting deleted — retrying won't help
    except TokenExpiredError as exc:
        logger.error(
            "ingest_meeting_task: app token rejected fetching online meeting | "
            "org=%s | call_chain_id=%s",
            org_name, call_chain_id,
        )
        raise task.retry(exc=exc, countdown=600)
    except GraphClientError as exc:
        logger.error(
            "ingest_meeting_task: Graph error fetching online meeting | "
            "org=%s | call_chain_id=%s | status=%s | error=%s",
            org_name, call_chain_id, exc.status_code, exc,
        )
        raise task.retry(exc=exc, countdown=300)

    meeting_graph_id: str = meeting["id"]

    logger.info(
        "ingest_meeting_task: meeting identified | org=%s | call_chain_id=%s | "
        "meeting_graph_id=%s | subject=%r | join_url=%s",
        org_name, call_chain_id, meeting_graph_id,
        meeting.get("subject") or "Untitled Meeting",
        meeting.get("joinWebUrl", ""),
    )

    # Step 4b: list transcripts — may be empty if Teams hasn't processed them yet
    try:
        transcripts = await client.get_transcripts(meeting_graph_id, user_id=lookup_user_id)
    except TokenExpiredError as exc:
        logger.error(
            "ingest_meeting_task: app token rejected fetching transcripts | "
            "org=%s | meeting_graph_id=%s",
            org_name, meeting_graph_id,
        )
        raise task.retry(exc=exc, countdown=600)
    except GraphClientError as exc:
        logger.error(
            "ingest_meeting_task: Graph error fetching transcripts list | "
            "org=%s | meeting_graph_id=%s | status=%s | error=%s",
            org_name, meeting_graph_id, exc.status_code, exc,
        )
        raise task.retry(exc=exc, countdown=300)

    if not transcripts:
        logger.warning(
            "ingest_meeting_task: no transcripts available yet — retrying | "
            "org=%s | meeting_graph_id=%s | call_chain_id=%s",
            org_name, meeting_graph_id, call_chain_id,
        )
        raise task.retry(
            exc=Exception("Transcript not yet available — Teams is still processing"),
            countdown=300,
        )

    transcript_id: str = transcripts[0]["id"]

    # Step 4c: download the raw VTT content
    try:
        vtt_content = await client.get_transcript_content(
            meeting_graph_id, transcript_id, user_id=lookup_user_id
        )
    except TokenExpiredError as exc:
        logger.error(
            "ingest_meeting_task: app token rejected fetching VTT | "
            "org=%s | meeting_graph_id=%s | transcript_id=%s",
            org_name, meeting_graph_id, transcript_id,
        )
        raise task.retry(exc=exc, countdown=600)
    except GraphClientError as exc:
        logger.error(
            "ingest_meeting_task: Graph error fetching VTT content | "
            "org=%s | meeting_graph_id=%s | transcript_id=%s | status=%s | error=%s",
            org_name, meeting_graph_id, transcript_id, exc.status_code, exc,
        )
        raise task.retry(exc=exc, countdown=300)

    logger.info(
        "ingest_meeting_task: VTT fetched | org=%s | meeting_graph_id=%s | "
        "transcript_id=%s | vtt_length=%d chars",
        org_name, meeting_graph_id, transcript_id, len(vtt_content),
    )

    # ── Step 5: credit pricing ────────────────────────────────────────────────
    # cached_tenant already resolved in Step 3.5 (platform user gate).

    async with central_session() as central_db:
        pricing_result = await central_db.execute(
            select(CreditPricing).where(CreditPricing.plan == cached_tenant.plan)
        )
        pricing = pricing_result.scalar_one_or_none()

    credits_per_minute: int = pricing.credits_per_minute if pricing else 1

    # ── Step 6: upsert meeting + participants in tenant DB ────────────────────
    # Real display names are fetched from Graph via get_user_by_id (read-only,
    # no DB writes). Falls back to UPN if the call fails (e.g. external user).
    # No user records are created here — the users table is platform-only.

    participants_raw = meeting.get("participants", {})
    organizer_raw = participants_raw.get("organizer", {})
    organizer_upn: str = organizer_raw.get("upn") or ""

    try:
        organizer_profile = await client.get_user_by_id(organizer_graph_id)
    except (TokenExpiredError, GraphClientError):
        organizer_profile = None

    organizer_display_name: str = (
        (organizer_profile or {}).get("displayName")
        or organizer_upn
        or "Unknown Organizer"
    )
    organizer_email: str = (
        (organizer_profile or {}).get("mail")
        or (organizer_profile or {}).get("userPrincipalName")
        or organizer_upn
        or ""
    )

    subject: str = meeting.get("subject") or "Untitled Meeting"
    start_dt = _parse_graph_dt(meeting.get("startDateTime"))
    end_dt = _parse_graph_dt(meeting.get("endDateTime"))
    duration_minutes = _compute_duration(start_dt, end_dt)
    join_url_from_graph: str = meeting.get("joinWebUrl") or join_web_url

    async with db_manager.get_session(ms_tenant_id, cached_tenant) as tenant_db:

        meeting_row = await _upsert_meeting(
            tenant_db,
            meeting_graph_id=meeting_graph_id,
            organizer_graph_id=organizer_graph_id,
            organizer_name=organizer_display_name,
            organizer_email=organizer_email,
            subject=subject,
            meeting_date=start_dt or datetime.now(timezone.utc),
            meeting_end_date=end_dt,
            duration_minutes=duration_minutes,
            join_url=join_url_from_graph,
        )
        await tenant_db.flush()

        await _upsert_participant(
            tenant_db,
            meeting_id=meeting_row.id,
            participant_graph_id=organizer_graph_id,
            participant_name=organizer_display_name,
            participant_email=organizer_email,
            role="organizer",
        )

        for attendee_raw in participants_raw.get("attendees", []):
            attendee_graph_id: str | None = (
                attendee_raw.get("identity", {}).get("user", {}).get("id")
            )
            if not attendee_graph_id:
                continue
            attendee_upn: str = attendee_raw.get("upn") or ""

            try:
                attendee_profile = await client.get_user_by_id(attendee_graph_id)
            except (TokenExpiredError, GraphClientError):
                attendee_profile = None

            attendee_name: str | None = (
                (attendee_profile or {}).get("displayName")
                or attendee_upn
                or None
            )
            attendee_email: str | None = (
                (attendee_profile or {}).get("mail")
                or (attendee_profile or {}).get("userPrincipalName")
                or attendee_upn
                or None
            )
            await _upsert_participant(
                tenant_db,
                meeting_id=meeting_row.id,
                participant_graph_id=attendee_graph_id,
                participant_name=attendee_name,
                participant_email=attendee_email,
                role="attendee",
            )

        await tenant_db.flush()

        # ── Step 7: run ingestion pipeline ────────────────────────────────────
        # pipeline.py owns: parse VTT → chunk → embed → persist chunks →
        # speaker analytics → credit usage → set meeting.status = 'ready'
        # It never calls db.commit() — db_manager.get_session() commits on clean exit.
        # On failure, pipeline sets meeting.status = 'failed' and flushes.
        # We commit explicitly before retrying so the 'failed' status is persisted.

        try:
            await run_ingestion_pipeline(
                meeting_id=meeting_row.id,
                vtt_content=vtt_content,
                db=tenant_db,
                credits_per_minute=credits_per_minute,
            )
        except Exception as exc:
            await tenant_db.commit()  # persist meeting.status = 'failed'
            logger.exception(
                "ingest_meeting_task: pipeline failed | org=%s | "
                "meeting_graph_id=%s | error=%s",
                org_name, meeting_graph_id, exc,
            )
            raise task.retry(exc=exc, countdown=300)

        logger.info(
            "ingest_meeting_task: complete | org=%s | meeting_graph_id=%s | meeting_id=%s",
            org_name, meeting_graph_id, meeting_row.id,
        )

    # ── TODO: Step 8 — fan out ────────────────────────────────────────────────
    # TODO: send_task insights_task, sentiment_task, rules_task (pending other teams)


# ── Private helpers ───────────────────────────────────────────────────────────

def _parse_graph_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _compute_duration(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(1, ceil((end - start).total_seconds() / 60))



async def _upsert_meeting(
    db: AsyncSession,
    *,
    meeting_graph_id: str,
    organizer_graph_id: str,
    organizer_name: str | None,
    organizer_email: str | None,
    subject: str,
    meeting_date: datetime,
    meeting_end_date: datetime | None,
    duration_minutes: int | None,
    join_url: str,
) -> Meeting:
    result = await db.execute(
        select(Meeting).where(Meeting.meeting_graph_id == meeting_graph_id)
    )
    meeting = result.scalar_one_or_none()
    if meeting is None:
        meeting = Meeting(
            meeting_graph_id=meeting_graph_id,
            organizer_graph_id=organizer_graph_id,
            organizer_name=organizer_name,
            organizer_email=organizer_email,
            meeting_subject=subject,
            meeting_date=meeting_date,
            meeting_end_date=meeting_end_date,
            duration_minutes=duration_minutes,
            join_url=join_url,
            ingestion_source="webhook",
            status="pending",
        )
        db.add(meeting)
    else:
        meeting.organizer_graph_id = organizer_graph_id
        meeting.organizer_name = organizer_name
        meeting.organizer_email = organizer_email
        meeting.meeting_subject = subject
        meeting.meeting_date = meeting_date
        meeting.meeting_end_date = meeting_end_date
        meeting.duration_minutes = duration_minutes
        meeting.join_url = join_url
        meeting.ingestion_source = "webhook"
    return meeting


async def _upsert_participant(
    db: AsyncSession,
    *,
    meeting_id: UUID,
    participant_graph_id: str,
    participant_name: str | None,
    participant_email: str | None,
    role: str,
) -> None:
    result = await db.execute(
        select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.participant_graph_id == participant_graph_id,
        )
    )
    if result.scalar_one_or_none() is None:
        db.add(MeetingParticipant(
            meeting_id=meeting_id,
            participant_graph_id=participant_graph_id,
            participant_name=participant_name,
            participant_email=participant_email,
            role=role,
        ))
