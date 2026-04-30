"""
POST /ingest/meeting — Manual meeting ingestion route.

Flow:
  1. Use the caller's Graph token to look up the meeting by join URL.
  2. Resolve display names for the organizer and all attendees via parallel
     get_user_by_id calls. Falls back to UPN on any lookup failure.
  3. Upsert the meeting row and participant rows (no writes to users table —
     users table is platform-only, populated via SSO login).
  4. Fetch the transcript list from Graph.
     → If no transcript yet: commit the meeting rows, return 202 Accepted.
       Teams takes 5–10 minutes after a meeting ends to process transcripts.
  5. Fetch the raw VTT content.
  6. Look up CreditPricing for this tenant's plan in the central DB.
  7. Run the ingestion pipeline (parse → chunk → embed → persist).
  8. Commit the full transaction and return 200.


Token source (CONTEXT.md Open Question #1 — do NOT resolve here):
  graph_token is a delegated Microsoft Graph token acquired by the frontend via
  MSAL.js. It must include the scopes: User.Read, OnlineMeetings.Read,
  OnlineMeetingTranscript.Read.All.  When Open Question #1 is settled (OBO vs
  frontend-passed token), only the GraphClient instantiation line in this file
  changes.

DB commit ownership:
  This route owns the transaction. run_ingestion_pipeline() never calls
  db.commit(). All commits happen here, including the commit that persists the
  "failed" status when the pipeline raises.
"""

import asyncio
import logging
from datetime import datetime, timezone
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_current_user, get_tenant_db
from app.core.security import CurrentUser
from app.db.central.models import CreditPricing
from app.db.central.session import get_central_db
from app.db.tenant.models import Meeting, MeetingParticipant
from app.services.graph.client import GraphClient, get_access_token_app
from app.services.graph.exceptions import (
    GraphClientError,
    MeetingNotFoundError,
    TokenExpiredError,
)
from app.services.ingestion.pipeline import run_ingestion_pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingestion"])


# ── Request / Response schemas ────────────────────────────────────────────────

class IngestMeetingRequest(BaseModel):
    join_url: str = Field(..., description="Teams meeting join URL")
    graph_token: str = Field(
        ...,
        description=(
            "Delegated Microsoft Graph token from MSAL.js. "
            "Required scopes: User.Read, OnlineMeetings.Read, "
            "OnlineMeetingTranscript.Read.All."
        ),
    )


class IngestMeetingResponse(BaseModel):
    meeting_id: UUID
    meeting_graph_id: str
    status: str
    message: str


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post(
    "/meeting",
    response_model=IngestMeetingResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest a Teams meeting transcript",
)
async def ingest_meeting(
    body: IngestMeetingRequest,
    current_user: CurrentUser = Depends(get_current_user),
    tenant_db: AsyncSession = Depends(get_tenant_db),
    central_db: AsyncSession = Depends(get_central_db),
) -> IngestMeetingResponse:
    gc = GraphClient(body.graph_token)

    # ── Step 1: fetch meeting from Graph ──────────────────────────────────────
    try:
        gm = await gc.get_meeting_by_join_url(body.join_url)
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Graph token expired. Re-authenticate via MSAL and retry.",
        ) from exc
    except MeetingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except GraphClientError as exc:
        logger.error(
            "Graph error fetching meeting | join_url=%s | tenant=%s | graph_status=%s",
            body.join_url, current_user.tenant.org_name, exc.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Microsoft Graph is temporarily unavailable. Please try again later.",
        ) from exc

    meeting_graph_id: str = gm["id"]
    subject: str = gm.get("subject") or "Untitled Meeting"
    join_url_from_graph: str = gm.get("joinWebUrl") or body.join_url
    start_dt = _parse_graph_dt(gm.get("startDateTime"))
    end_dt = _parse_graph_dt(gm.get("endDateTime"))
    duration_minutes = _compute_duration(start_dt, end_dt)

    logger.info(
        "Meeting fetched from Graph | meeting_graph_id=%s | subject=%s | tenant=%s",
        meeting_graph_id, subject, current_user.tenant.org_name,
    )

    # ── Step 2: resolve display names using an app-only token ────────────────
    # GET /users/{id} requires User.Read.All which is an app permission —
    # the delegated token from the frontend doesn't have it. We acquire an
    # app-only token via client credentials for these lookups only.
    participants_raw = gm.get("participants", {})
    organizer_raw = participants_raw.get("organizer", {})
    organizer_graph_id: str = (
        organizer_raw.get("identity", {}).get("user", {}).get("id")
        or current_user.graph_id
    )
    organizer_upn: str = organizer_raw.get("upn") or ""

    attendees_raw = [
        a for a in participants_raw.get("attendees", [])
        if a.get("identity", {}).get("user", {}).get("id")
    ]
    attendee_graph_ids: list[str] = [
        a["identity"]["user"]["id"] for a in attendees_raw
    ]
    attendee_upns: list[str] = [a.get("upn") or "" for a in attendees_raw]

    app_token = await asyncio.to_thread(
        get_access_token_app, current_user.tenant.ms_tenant_id
    )
    app_gc = GraphClient(app_token)

    all_graph_ids = [organizer_graph_id] + attendee_graph_ids
    raw_results = await asyncio.gather(
        *[app_gc.get_user_by_id(gid) for gid in all_graph_ids],
        return_exceptions=True,
    )
    profiles = []
    for gid, r in zip(all_graph_ids, raw_results):
        if isinstance(r, dict):
            profiles.append(r)
        else:
            logger.warning(
                "ingest_meeting: get_user_by_id failed — falling back to UPN | "
                "tenant=%s | graph_id=%s | error=%s",
                current_user.tenant.org_name, gid, r,
            )
            profiles.append(None)

    organizer_profile = profiles[0]
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

    # ── Step 3: upsert meeting + participants ─────────────────────────────────
    # No writes to the users table — it is platform-only (SSO login).
    # Participant identity is stored directly on meeting_participants rows.
    try:
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

        for i, attendee_graph_id in enumerate(attendee_graph_ids):
            attendee_profile = profiles[i + 1]
            attendee_upn = attendee_upns[i]
            attendee_name: str = (
                (attendee_profile or {}).get("displayName")
                or attendee_upn
                or "Unknown Attendee"
            )
            attendee_email: str = (
                (attendee_profile or {}).get("mail")
                or (attendee_profile or {}).get("userPrincipalName")
                or attendee_upn
                or ""
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

    except Exception as exc:
        logger.exception(
            "DB error during meeting/participant upsert | meeting_graph_id=%s | "
            "tenant=%s | error=%s",
            meeting_graph_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to save meeting data. Please try again later.",
        ) from exc

    # ── Step 4: check whether the transcript is available ────────────────────
    try:
        transcripts = await gc.get_transcripts(meeting_graph_id)
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Graph token expired. Re-authenticate via MSAL and retry.",
        ) from exc
    except GraphClientError as exc:
        logger.error(
            "Graph error fetching transcript list | meeting_graph_id=%s | "
            "tenant=%s | graph_status=%s",
            meeting_graph_id, current_user.tenant.org_name, exc.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Microsoft Graph is temporarily unavailable. Please try again later.",
        ) from exc

    if not transcripts:
        # No commit needed here — the get_tenant_db dependency commits on clean return.
        logger.info(
            "Transcript not ready yet | meeting_graph_id=%s | tenant=%s",
            meeting_graph_id, current_user.tenant.org_name,
        )
        return IngestMeetingResponse(
            meeting_id=meeting_row.id,
            meeting_graph_id=meeting_graph_id,
            status=meeting_row.status,
            message=(
                "Meeting saved. Transcript is not yet available — Teams typically "
                "takes 5–10 minutes after a meeting ends. "
                "Re-trigger ingestion once the transcript is ready."
            ),
        )

    # ── Step 5: fetch the VTT content ─────────────────────────────────────────
    transcript_id: str = transcripts[0]["id"]
    try:
        vtt_content = await gc.get_transcript_content(meeting_graph_id, transcript_id)
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Graph token expired. Re-authenticate via MSAL and retry.",
        ) from exc
    except GraphClientError as exc:
        logger.error(
            "Graph error fetching VTT | meeting_graph_id=%s | transcript_id=%s | "
            "tenant=%s | graph_status=%s",
            meeting_graph_id, transcript_id, current_user.tenant.org_name, exc.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Microsoft Graph is temporarily unavailable. Please try again later.",
        ) from exc

    # ── Step 6: look up credit pricing ────────────────────────────────────────
    try:
        pricing_result = await central_db.execute(
            select(CreditPricing).where(CreditPricing.plan == current_user.tenant.plan)
        )
        pricing = pricing_result.scalar_one_or_none()
    except Exception as exc:
        logger.exception(
            "Central DB error during credit pricing lookup | tenant=%s | plan=%s | error=%s",
            current_user.tenant.org_name, current_user.tenant.plan, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to retrieve pricing data. Please try again later.",
        ) from exc

    credits_per_minute: int = pricing.credits_per_minute if pricing else 1

    logger.info(
        "Running ingestion pipeline | meeting_graph_id=%s | plan=%s | "
        "credits_per_minute=%d | tenant=%s",
        meeting_graph_id, current_user.tenant.plan,
        credits_per_minute, current_user.tenant.org_name,
    )

    # ── Step 7: run the ingestion pipeline ────────────────────────────────────
    try:
        await run_ingestion_pipeline(
            meeting_id=meeting_row.id,
            vtt_content=vtt_content,
            db=tenant_db,
            credits_per_minute=credits_per_minute,
        )
    except ValueError as exc:
        # Pipeline set meeting.status = "failed" — commit it before raising
        # so the UI can surface the error. HTTPException causes the dependency
        # to rollback, so we must commit explicitly here first.
        await tenant_db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc
    except Exception as exc:
        await tenant_db.commit()
        logger.error(
            "Ingestion pipeline error | meeting_graph_id=%s | tenant=%s | error=%s",
            meeting_graph_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion failed due to an internal error. The meeting has been marked as failed.",
        ) from exc

    # ── Step 8: respond — dependency commits on clean return ─────────────────
    logger.info(
        "Ingestion complete | meeting_id=%s | meeting_graph_id=%s | tenant=%s",
        meeting_row.id, meeting_graph_id, current_user.tenant.org_name,
    )
    return IngestMeetingResponse(
        meeting_id=meeting_row.id,
        meeting_graph_id=meeting_graph_id,
        status="ready",
        message="Meeting transcript ingested successfully.",
    )


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
    try:
        result = await db.execute(
            select(Meeting).where(Meeting.meeting_graph_id == meeting_graph_id)
        )
    except Exception as exc:
        logger.exception(
            "_upsert_meeting: DB error querying meeting | meeting_graph_id=%s | error=%s",
            meeting_graph_id, exc,
        )
        raise

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
            ingestion_source="manual",
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
        meeting.ingestion_source = "manual"
    return meeting


async def _upsert_participant(
    db: AsyncSession,
    *,
    meeting_id: UUID,
    participant_graph_id: str,
    participant_name: str | None,
    participant_email: str | None,
    role: str,
    granted_by: UUID | None = None,
) -> None:
    try:
        result = await db.execute(
            select(MeetingParticipant).where(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.participant_graph_id == participant_graph_id,
            )
        )
    except Exception as exc:
        logger.exception(
            "_upsert_participant: DB error querying participant | "
            "meeting_id=%s | participant_graph_id=%s | error=%s",
            meeting_id, participant_graph_id, exc,
        )
        raise

    participant = result.scalar_one_or_none()
    if participant is None:
        db.add(MeetingParticipant(
            meeting_id=meeting_id,
            participant_graph_id=participant_graph_id,
            participant_name=participant_name,
            participant_email=participant_email,
            role=role,
            granted_by=granted_by,
        ))
    else:
        participant.participant_name = participant_name
        participant.participant_email = participant_email
        participant.role = role
