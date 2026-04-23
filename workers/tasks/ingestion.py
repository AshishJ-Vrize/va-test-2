# Celery task: ingest_meeting_task
# Owner: Workers team
# Called by: app/services/graph/webhook.py — handle_notification()
# Depends on: app/services/graph/client.py (DELIVERED)
#             app/services/graph/exceptions.py (DELIVERED)
#             app/db/central/models.py (DELIVERED)
#             app/db/central/session.py (DELIVERED)
# See docs/webhook_dependencies.md for full dependency tracking.

import asyncio
import logging

from celery import Task
from sqlalchemy import select

from app.db.central.models import Tenant
from app.db.central.session import central_session
from app.services.graph.client import GraphClient, get_access_token_app
from app.services.graph.exceptions import GraphClientError, MeetingNotFoundError, TokenExpiredError
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
    # callRecord contains joinWebUrl (needed to look up the online meeting)
    # and organizer.user.id (needed as user_id for app-only Graph calls).

    try:
        call_record = await client.get(f"/communications/callRecords/{call_chain_id}")
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
    organizer_user_id: str | None = (
        call_record.get("organizer", {}).get("user", {}).get("id")
    )

    if not join_web_url or not organizer_user_id:
        logger.error(
            "ingest_meeting_task: callRecord missing joinWebUrl or organizer.user.id — aborting | "
            "org=%s | call_chain_id=%s | joinWebUrl=%r | organizer_user_id=%r",
            org_name, call_chain_id, join_web_url, organizer_user_id,
        )
        return  # structural issue — retrying won't help

    logger.info(
        "ingest_meeting_task: callRecord fetched | org=%s | call_chain_id=%s | "
        "organizer_user_id=%s",
        org_name, call_chain_id, organizer_user_id,
    )

    # ── Step 4: fetch transcript VTT from Graph ───────────────────────────────
    # Transcripts live under the online meeting, not the callRecord directly.
    # Verified flow (2026-04-21):
    #   joinWebUrl → get_meeting_by_join_url() → meeting_id
    #   meeting_id → get_transcripts()         → transcript_id
    #   meeting_id + transcript_id → get_transcript_content() → VTT string

    # Step 4a: resolve meeting_id from joinWebUrl
    try:
        meeting = await client.get_meeting_by_join_url(
            join_web_url, user_id=organizer_user_id
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

    meeting_id: str = meeting["id"]

    # Step 4b: list transcripts — may be empty if Teams hasn't processed them yet
    try:
        transcripts = await client.get_transcripts(meeting_id, user_id=organizer_user_id)
    except TokenExpiredError as exc:
        logger.error(
            "ingest_meeting_task: app token rejected fetching transcripts | "
            "org=%s | meeting_id=%s",
            org_name, meeting_id,
        )
        raise task.retry(exc=exc, countdown=600)
    except GraphClientError as exc:
        logger.error(
            "ingest_meeting_task: Graph error fetching transcripts list | "
            "org=%s | meeting_id=%s | status=%s | error=%s",
            org_name, meeting_id, exc.status_code, exc,
        )
        raise task.retry(exc=exc, countdown=300)

    if not transcripts:
        logger.warning(
            "ingest_meeting_task: no transcripts available yet — retrying | "
            "org=%s | meeting_id=%s | call_chain_id=%s",
            org_name, meeting_id, call_chain_id,
        )
        raise task.retry(
            exc=Exception("Transcript not yet available — Teams is still processing"),
            countdown=300,
        )

    transcript_id: str = transcripts[0]["id"]

    # Step 4c: download the raw VTT content
    try:
        vtt_content = await client.get_transcript_content(
            meeting_id, transcript_id, user_id=organizer_user_id
        )
    except TokenExpiredError as exc:
        logger.error(
            "ingest_meeting_task: app token rejected fetching VTT | "
            "org=%s | meeting_id=%s | transcript_id=%s",
            org_name, meeting_id, transcript_id,
        )
        raise task.retry(exc=exc, countdown=600)
    except GraphClientError as exc:
        logger.error(
            "ingest_meeting_task: Graph error fetching VTT content | "
            "org=%s | meeting_id=%s | transcript_id=%s | status=%s | error=%s",
            org_name, meeting_id, transcript_id, exc.status_code, exc,
        )
        raise task.retry(exc=exc, countdown=300)

    logger.info(
        "ingest_meeting_task: VTT fetched | org=%s | meeting_id=%s | "
        "transcript_id=%s | vtt_length=%d chars",
        org_name, meeting_id, transcript_id, len(vtt_content),
    )

    # ── TODO: steps owned by other teams ─────────────────────────────────────
    # TODO: Step 5 — parse VTT into segments (workers/services/vtt_parser.py — pending)
    # TODO: Step 6 — generate embeddings (Azure OpenAI — pending)
    # TODO: Step 7 — write call_record + segments to tenant DB (tenant DB models — pending)
    # TODO: Step 8 — fan out: send_task insights, sentiment, rules (pending)
