from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_db
from app.core.security import CurrentUser
from app.db.tenant.models import Meeting, MeetingParticipant

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])


class MeetingItem(BaseModel):
    id: UUID
    meeting_graph_id: str
    organizer_name: str | None
    organizer_email: str | None
    meeting_subject: str
    meeting_date: datetime
    meeting_end_date: datetime | None
    duration_minutes: int | None
    status: str
    ingestion_source: str
    your_role: str


@router.get("", response_model=list[MeetingItem])
async def list_meetings(
    filter: Literal["participated", "granted", "both"] = "participated",
    current_user: CurrentUser = Depends(get_current_user),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> list[MeetingItem]:
    conditions = [MeetingParticipant.participant_graph_id == current_user.graph_id]
    if filter == "participated":
        conditions.append(MeetingParticipant.role.in_(["organizer", "attendee"]))
    elif filter == "granted":
        conditions.append(MeetingParticipant.role == "granted")

    stmt = (
        select(Meeting, MeetingParticipant.role)
        .join(MeetingParticipant, Meeting.id == MeetingParticipant.meeting_id)
        .where(*conditions)
        .order_by(Meeting.meeting_date.desc())
    )

    try:
        rows = (await tenant_db.execute(stmt)).all()
    except Exception as exc:
        logger.exception(
            "meetings: DB error listing meetings | user_id=%s | org=%s | error=%s",
            current_user.id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to retrieve meetings. Please try again later.",
        ) from exc

    return [
        MeetingItem(
            id=meeting.id,
            meeting_graph_id=meeting.meeting_graph_id,
            organizer_name=meeting.organizer_name,
            organizer_email=meeting.organizer_email,
            meeting_subject=meeting.meeting_subject,
            meeting_date=meeting.meeting_date,
            meeting_end_date=meeting.meeting_end_date,
            duration_minutes=meeting.duration_minutes,
            status=meeting.status,
            ingestion_source=meeting.ingestion_source,
            your_role=role,
        )
        for meeting, role in rows
    ]
