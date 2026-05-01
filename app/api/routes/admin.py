from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_db, require_super_admin
from app.core.security import CurrentUser
from app.db.tenant.models import Meeting, MeetingParticipant, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_MAX_SUPER_ADMINS = 3


# ── Schemas ───────────────────────────────────────────────────────────────────

class MeetingAdminItem(BaseModel):
    id: UUID
    meeting_graph_id: str
    organizer_name: str | None
    organizer_email: str | None
    meeting_subject: str
    status: str
    ingestion_source: str


class GrantAccessRequest(BaseModel):
    user_graph_id: str


class UserItem(BaseModel):
    id: UUID
    graph_id: str
    email: str
    display_name: str
    system_role: str
    is_active: bool


class PatchUserRequest(BaseModel):
    is_active: bool | None = None
    system_role: str | None = None  # accepts "super_admin" or "user" from frontend


# ── Meetings ──────────────────────────────────────────────────────────────────

@router.get("/meetings", response_model=list[MeetingAdminItem])
async def list_all_meetings(
    current_user: CurrentUser = Depends(require_super_admin),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> list[MeetingAdminItem]:
    try:
        rows = (await tenant_db.execute(
            select(Meeting).order_by(Meeting.meeting_date.desc())
        )).scalars().all()
    except Exception as exc:
        logger.exception(
            "admin: DB error listing all meetings | user_id=%s | org=%s | error=%s",
            current_user.id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to retrieve meetings. Please try again later.",
        ) from exc

    return [
        MeetingAdminItem(
            id=m.id,
            meeting_graph_id=m.meeting_graph_id,
            organizer_name=m.organizer_name,
            organizer_email=m.organizer_email,
            meeting_subject=m.meeting_subject,
            status=m.status,
            ingestion_source=m.ingestion_source,
        )
        for m in rows
    ]


@router.post("/meetings/{meeting_id}/grant", status_code=status.HTTP_201_CREATED)
async def grant_meeting_access(
    meeting_id: UUID,
    body: GrantAccessRequest,
    current_user: CurrentUser = Depends(require_super_admin),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> dict:
    try:
        meeting = (await tenant_db.execute(
            select(Meeting).where(Meeting.id == meeting_id)
        )).scalar_one_or_none()
    except Exception as exc:
        logger.exception(
            "admin: DB error fetching meeting for grant | meeting_id=%s | org=%s | error=%s",
            meeting_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Service temporarily unavailable.") from exc

    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")

    try:
        existing = (await tenant_db.execute(
            select(MeetingParticipant).where(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.participant_graph_id == body.user_graph_id,
            )
        )).scalar_one_or_none()
    except Exception as exc:
        logger.exception(
            "admin: DB error checking existing participant | meeting_id=%s | org=%s | error=%s",
            meeting_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Service temporarily unavailable.") from exc

    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"User already has role '{existing.role}' for this meeting.",
        )

    tenant_db.add(MeetingParticipant(
        meeting_id=meeting_id,
        participant_graph_id=body.user_graph_id,
        role="granted",
        granted_by=current_user.id,
    ))

    logger.info(
        "admin: meeting access granted | meeting_id=%s | to=%s | by=%s | org=%s",
        meeting_id, body.user_graph_id, current_user.id, current_user.tenant.org_name,
    )
    return {"detail": "Access granted."}


@router.delete("/meetings/{meeting_id}/grant/{user_graph_id}",
               status_code=status.HTTP_200_OK)
async def revoke_meeting_access(
    meeting_id: UUID,
    user_graph_id: str,
    current_user: CurrentUser = Depends(require_super_admin),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> dict:
    try:
        participant = (await tenant_db.execute(
            select(MeetingParticipant).where(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.participant_graph_id == user_graph_id,
                MeetingParticipant.role == "granted",
            )
        )).scalar_one_or_none()
    except Exception as exc:
        logger.exception(
            "admin: DB error fetching participant for revoke | meeting_id=%s | org=%s | error=%s",
            meeting_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Service temporarily unavailable.") from exc

    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No granted access found for this user on this meeting.",
        )

    await tenant_db.delete(participant)
    logger.info(
        "admin: meeting access revoked | meeting_id=%s | from=%s | by=%s | org=%s",
        meeting_id, user_graph_id, current_user.id, current_user.tenant.org_name,
    )
    return {"detail": "Access revoked."}


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserItem])
async def list_users(
    current_user: CurrentUser = Depends(require_super_admin),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> list[UserItem]:
    try:
        rows = (await tenant_db.execute(
            select(User).order_by(User.display_name)
        )).scalars().all()
    except Exception as exc:
        logger.exception(
            "admin: DB error listing users | user_id=%s | org=%s | error=%s",
            current_user.id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Failed to retrieve users. Please try again later.") from exc

    return [
        UserItem(
            id=u.id,
            graph_id=u.graph_id,
            email=u.email,
            display_name=u.display_name,
            system_role="super_admin" if u.system_role == "compliance_officer" else u.system_role,
            is_active=u.is_active,
        )
        for u in rows
    ]


@router.patch("/users/{user_id}", response_model=UserItem)
async def patch_user(
    user_id: UUID,
    body: PatchUserRequest,
    current_user: CurrentUser = Depends(require_super_admin),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> UserItem:
    try:
        user = (await tenant_db.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
    except Exception as exc:
        logger.exception(
            "admin: DB error fetching user for patch | user_id=%s | org=%s | error=%s",
            user_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Service temporarily unavailable.") from exc

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if body.is_active is not None:
        user.is_active = body.is_active

    if body.system_role is not None:
        db_role = "compliance_officer" if body.system_role == "super_admin" else body.system_role

        if db_role == user.system_role:
            pass  # no change needed

        elif user.system_role == "compliance_officer" and db_role != "compliance_officer":
            # Demotion request — log and accept gracefully
            logger.warning(
                "admin: super_admin demotion requested | target_user_id=%s | "
                "requested_by=%s | org=%s — requires platform team action",
                user_id, current_user.id, current_user.tenant.org_name,
            )
            return UserItem(
                id=user.id,
                graph_id=user.graph_id,
                email=user.email,
                display_name=user.display_name,
                system_role="super_admin",
                is_active=user.is_active,
            )

        elif db_role == "compliance_officer":
            # Promotion — enforce max 3
            try:
                count = (await tenant_db.execute(
                    select(func.count()).where(User.system_role == "compliance_officer")
                )).scalar_one()
            except Exception as exc:
                logger.exception(
                    "admin: DB error counting super admins | org=%s | error=%s",
                    current_user.tenant.org_name, exc,
                )
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                    detail="Service temporarily unavailable.") from exc

            if count >= _MAX_SUPER_ADMINS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Maximum of {_MAX_SUPER_ADMINS} super admins allowed per tenant.",
                )
            user.system_role = db_role

        else:
            user.system_role = db_role

    logger.info(
        "admin: user updated | target_user_id=%s | by=%s | org=%s",
        user_id, current_user.id, current_user.tenant.org_name,
    )

    return UserItem(
        id=user.id,
        graph_id=user.graph_id,
        email=user.email,
        display_name=user.display_name,
        system_role="super_admin" if user.system_role == "compliance_officer" else user.system_role,
        is_active=user.is_active,
    )


class UserMeetingItem(BaseModel):
    id: UUID
    meeting_graph_id: str
    organizer_name: str | None
    organizer_email: str | None
    meeting_subject: str
    meeting_date: str
    status: str
    role: str


@router.get("/users/{user_id}/meetings", response_model=list[UserMeetingItem])
async def list_user_meetings(
    user_id: UUID,
    current_user: CurrentUser = Depends(require_super_admin),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> list[UserMeetingItem]:
    try:
        user = (await tenant_db.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
    except Exception as exc:
        logger.exception(
            "admin: DB error fetching user for meetings list | user_id=%s | org=%s | error=%s",
            user_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Service temporarily unavailable.") from exc

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    try:
        rows = (await tenant_db.execute(
            select(Meeting, MeetingParticipant.role)
            .join(MeetingParticipant, Meeting.id == MeetingParticipant.meeting_id)
            .where(MeetingParticipant.participant_graph_id == user.graph_id)
            .order_by(Meeting.meeting_date.desc())
        )).all()
    except Exception as exc:
        logger.exception(
            "admin: DB error listing meetings for user | user_id=%s | org=%s | error=%s",
            user_id, current_user.tenant.org_name, exc,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Failed to retrieve meetings. Please try again later.") from exc

    return [
        UserMeetingItem(
            id=meeting.id,
            meeting_graph_id=meeting.meeting_graph_id,
            organizer_name=meeting.organizer_name,
            organizer_email=meeting.organizer_email,
            meeting_subject=meeting.meeting_subject,
            meeting_date=meeting.meeting_date.isoformat(),
            status=meeting.status,
            role=role,
        )
        for meeting, role in rows
    ]
