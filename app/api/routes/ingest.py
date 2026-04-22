"""
POST /ingest/meeting — Manual meeting ingestion route.

Flow:
  1. Use the caller's Graph token to look up the meeting by join URL.
  2. Upsert the organizer (and any attendees with a known Graph ID) in the
     tenant users table.
  3. Upsert the meeting row and participant rows.
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

import logging
from datetime import datetime, timezone
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_central_db, get_current_user, get_tenant_db
from app.core.security import CurrentUser
from app.db.central.models import CreditPricing
from app.db.tenant.models import Meeting, MeetingParticipant, User
from app.services.graph.client import GraphClient
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
            "OnlineMeetingTranscript.Read.All. "
            "CONTEXT.md Open Question #1 tracks whether to replace this with OBO flow."
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
    description=(
        "Fetches meeting metadata and transcript from Microsoft Graph, then runs "
        "the ingestion pipeline (parse → chunk → embed). "
        "Returns 202 if the transcript is not yet ready — Teams typically takes "
        "5–10 minutes after a meeting ends."
    ),
)
def ingest_meeting(
    body: IngestMeetingRequest,
    current_user: CurrentUser = Depends(get_current_user),
    tenant_db: Session = Depends(get_tenant_db),
    central_db: Session = Depends(get_central_db),
) -> IngestMeetingResponse:
    gc = GraphClient(body.graph_token)

    # ── Step 1: fetch meeting from Graph ──────────────────────────────────────
    try:
        gm = gc.get_meeting_by_join_url(body.join_url)
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Graph token expired. Re-authenticate via MSAL and retry.",
        ) from exc
    except MeetingNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
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

    # ── Step 2: resolve organizer display name ────────────────────────────────
    # displayName in the meeting participants response is always null (verified live).
    # Call get_user_by_id to get the real display name.
    participants_raw = gm.get("participants", {})
    organizer_raw = participants_raw.get("organizer", {})
    organizer_graph_id: str = (
        organizer_raw.get("identity", {}).get("user", {}).get("id")
        or current_user.graph_id  # fallback: the requester is the organizer
    )
    organizer_upn: str | None = organizer_raw.get("upn")

    try:
        organizer_profile = gc.get_user_by_id(organizer_upn or organizer_graph_id)
    except (TokenExpiredError, GraphClientError):
        organizer_profile = None  # non-fatal — use what we already have

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

    # ── Step 3: upsert organizer user → meeting → participants ────────────────
    organizer_user = _upsert_user(
        tenant_db,
        graph_id=organizer_graph_id,
        email=organizer_email,
        display_name=organizer_display_name,
    )
    tenant_db.flush()  # assigns organizer_user.id before Meeting FK references it

    meeting_row = _upsert_meeting(
        tenant_db,
        meeting_graph_id=meeting_graph_id,
        organizer_id=organizer_user.id,
        subject=subject,
        meeting_date=start_dt or datetime.now(timezone.utc),
        meeting_end_date=end_dt,
        duration_minutes=duration_minutes,
        join_url=join_url_from_graph,
    )
    tenant_db.flush()  # assigns meeting_row.id before participant FK references it

    _upsert_participant(
        tenant_db,
        meeting_id=meeting_row.id,
        user_id=organizer_user.id,
        role="organizer",
    )

    # Upsert attendees using data available in the meeting response.
    # We skip the per-attendee get_user_by_id call here to avoid N+1 Graph
    # requests in the route handler. Display names can be backfilled by a
    # background task. We require a graph_id so the FK is populated correctly.
    for attendee_raw in participants_raw.get("attendees", []):
        attendee_graph_id: str | None = (
            attendee_raw.get("identity", {}).get("user", {}).get("id")
        )
        if not attendee_graph_id:
            continue  # external/guest user with no Azure AD identity — skip

        attendee_upn: str = attendee_raw.get("upn") or ""
        attendee_user = _upsert_user(
            tenant_db,
            graph_id=attendee_graph_id,
            email=attendee_upn,
            display_name=attendee_upn or "Unknown Attendee",
        )
        tenant_db.flush()
        _upsert_participant(
            tenant_db,
            meeting_id=meeting_row.id,
            user_id=attendee_user.id,
            role="attendee",
        )

    tenant_db.flush()

    # ── Step 4: check whether the transcript is available ────────────────────
    try:
        transcripts = gc.get_transcripts(meeting_graph_id)
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
        # Teams has not finished processing the transcript yet.
        # Persist the meeting and participant rows, then tell the caller to retry.
        tenant_db.commit()
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
        vtt_content = gc.get_transcript_content(meeting_graph_id, transcript_id)
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Graph token expired. Re-authenticate via MSAL and retry.",
        ) from exc
    except GraphClientError as exc:
        logger.error(
            "Graph error fetching VTT | meeting_graph_id=%s | transcript_id=%s | "
            "tenant=%s | graph_status=%s",
            meeting_graph_id, transcript_id,
            current_user.tenant.org_name, exc.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Microsoft Graph is temporarily unavailable. Please try again later.",
        ) from exc

    # ── Step 6: look up credit pricing ────────────────────────────────────────
    pricing = (
        central_db.query(CreditPricing)
        .filter(CreditPricing.plan == current_user.tenant.plan)
        .first()
    )
    # Fall back to 1 credit/minute if no pricing row exists — should never happen
    # in production but prevents a hard failure during schema bootstrap.
    credits_per_minute: int = pricing.credits_per_minute if pricing else 1

    logger.info(
        "Running ingestion pipeline | meeting_graph_id=%s | plan=%s | "
        "credits_per_minute=%d | tenant=%s",
        meeting_graph_id, current_user.tenant.plan,
        credits_per_minute, current_user.tenant.org_name,
    )

    # ── Step 7: run the ingestion pipeline ────────────────────────────────────
    # run_ingestion_pipeline() never calls db.commit() — this route owns the
    # transaction. On any exception the pipeline sets meeting.status = "failed"
    # before re-raising, so we commit after catching to persist that status.
    try:
        run_ingestion_pipeline(
            meeting_id=meeting_row.id,
            vtt_content=vtt_content,
            db=tenant_db,
            credits_per_minute=credits_per_minute,
        )
    except ValueError as exc:
        # VTT had zero usable segments (empty or malformed content).
        # meeting.status is already "failed" — commit it so the UI shows the error.
        tenant_db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        # Embedding failure, DB write failure, or any other internal error.
        # meeting.status is already "failed" — commit it.
        tenant_db.commit()
        logger.error(
            "Ingestion pipeline error | meeting_graph_id=%s | tenant=%s | error=%s",
            meeting_graph_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion failed due to an internal error. The meeting has been marked as failed.",
        ) from exc

    # ── Step 8: commit and respond ────────────────────────────────────────────
    tenant_db.commit()

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
    """Parse a Graph API ISO 8601 timestamp (may end with Z) to an aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _compute_duration(start: datetime | None, end: datetime | None) -> int | None:
    """Return the meeting duration in whole minutes, or None if either bound is absent."""
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    return max(1, ceil(seconds / 60))


def _upsert_user(
    db: Session,
    *,
    graph_id: str,
    email: str,
    display_name: str,
) -> User:
    """
    Return the User row with this graph_id, creating it if it does not exist.
    On re-ingestion, refreshes email and display_name so stale data is corrected.
    Falls back so neither email nor display_name is ever empty in the DB.
    """
    user = db.query(User).filter(User.graph_id == graph_id).first()
    if user is None:
        user = User(
            graph_id=graph_id,
            email=email or graph_id,
            display_name=display_name or "Unknown",
            system_role="user",
            is_active=True,
        )
        db.add(user)
    else:
        if email:
            user.email = email
        if display_name:
            user.display_name = display_name
    return user


def _upsert_meeting(
    db: Session,
    *,
    meeting_graph_id: str,
    organizer_id: UUID,
    subject: str,
    meeting_date: datetime,
    meeting_end_date: datetime | None,
    duration_minutes: int | None,
    join_url: str,
) -> Meeting:
    """
    Return the Meeting row for this meeting_graph_id, creating it if needed.
    On re-ingestion, updates all mutable metadata fields. The status field is
    preserved — the pipeline is responsible for advancing it to ready|failed.
    """
    meeting = (
        db.query(Meeting)
        .filter(Meeting.meeting_graph_id == meeting_graph_id)
        .first()
    )
    if meeting is None:
        meeting = Meeting(
            meeting_graph_id=meeting_graph_id,
            organizer_id=organizer_id,
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
        meeting.meeting_subject = subject
        meeting.meeting_date = meeting_date
        meeting.meeting_end_date = meeting_end_date
        meeting.duration_minutes = duration_minutes
        meeting.join_url = join_url
        meeting.ingestion_source = "manual"
    return meeting


def _upsert_participant(
    db: Session,
    *,
    meeting_id: UUID,
    user_id: UUID,
    role: str,
    granted_by: UUID | None = None,
) -> None:
    """
    Create a MeetingParticipant row for (meeting_id, user_id) if one does not exist.
    No-op on re-ingestion — existing participant rows are left as-is.
    """
    exists = (
        db.query(MeetingParticipant)
        .filter(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.user_id == user_id,
        )
        .first()
    )
    if exists is None:
        db.add(MeetingParticipant(
            meeting_id=meeting_id,
            user_id=user_id,
            role=role,
            granted_by=granted_by,
        ))
