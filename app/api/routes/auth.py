from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.security import CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class AuthMeResponse(BaseModel):
    id: UUID
    email: str | None
    display_name: str
    system_role: str
    is_active: bool
    org_name: str
    plan: str


@router.post("/me", response_model=AuthMeResponse)
async def auth_me(
    current_user: CurrentUser = Depends(get_current_user),
) -> AuthMeResponse:
    logger.info(
        "auth/me | user_id=%s | role=%s | org=%s",
        current_user.id, current_user.system_role, current_user.tenant.org_name,
    )
    return AuthMeResponse(
        id=current_user.id,
        email=current_user.email,
        display_name=current_user.display_name,
        system_role=current_user.system_role,
        is_active=current_user.is_active,
        org_name=current_user.tenant.org_name,
        plan=current_user.tenant.plan,
    )
