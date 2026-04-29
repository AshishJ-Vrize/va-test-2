# Webhook route handlers — Graph subscription management + notification ingestion
# Owner: Graph + Routes team (route shell); webhook team owns services/graph/webhook.py
# Depends on: app/api/deps.py — get_current_user (DELIVERED)
#             app/db/central/session.py — get_central_db (DELIVERED)
#             app/core/security.py — CurrentUser (DELIVERED)
# See docs/webhook_dependencies.md for full dependency tracking.

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.core.security import CurrentUser
from app.services.graph.exceptions import GraphClientError, TokenExpiredError
from app.services.graph import webhook as webhook_service
from app.db.central.session import get_central_db
from app.api.deps import get_current_user

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["webhook"])


# ── Request / Response schemas ────────────────────────────────────────────────

class RegisterWebhookResponse(BaseModel):
    subscription_id: str
    expiration_date_time: str
    notification_url: str


class RenewWebhookResponse(BaseModel):
    subscription_id: str
    expiration_date_time: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/call-records",
    summary="Graph change notification receiver",
    response_class=PlainTextResponse,
    status_code=200,
)
async def receive_call_records(
    request: Request,
    validationToken: str | None = Query(default=None),
    db: AsyncSession = Depends(get_central_db),
):
    """
    Receives Graph change notifications for callRecords subscriptions.

    Two modes:
    - Validation handshake: Graph sends GET/POST with ?validationToken=<token>.
      Must echo the token as plain text with Content-Type: text/plain within 10 s.
    - Live notification: Graph POSTs a JSON body with a "value" list of
      notification objects. We validate, deduplicate, and dispatch to Celery.

    This endpoint has NO authentication — Graph does not send a Bearer token.
    Security is provided by validating the clientState secret on every notification.
    """
    # ── Validation handshake ──────────────────────────────────────────────────
    if validationToken:
        logger.info("Graph subscription validation handshake received")
        return PlainTextResponse(content=validationToken, status_code=200)

    # ── Live notification ─────────────────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        logger.warning("Webhook received non-JSON body")
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")

    try:
        result = await webhook_service.handle_notification(payload, db)
    except Exception as exc:
        # Graph will retry if we return non-200. Log and return 200 so Graph
        # doesn't flood us with retries for a batch-level unexpected error.
        logger.exception("Unexpected error in handle_notification | error=%s", exc)
        return PlainTextResponse(content="", status_code=200)

    logger.info(
        "Notification batch processed | accepted=%d | skipped=%d",
        result["accepted"], result["skipped"],
    )

    # Graph requires 200 regardless of per-notification outcome.
    return PlainTextResponse(content="", status_code=200)


@router.post(
    "/register",
    response_model=RegisterWebhookResponse,
    summary="Register a callRecords webhook for the authenticated admin's tenant",
)
async def register_webhook(
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Creates a Graph callRecords subscription for the calling admin's tenant.

    - Tenant is identified via the admin's JWT: no body fields needed.
    - Requires system_role == 'admin' (enforced by require_admin dependency).
    - Subscription expires in ~23 hours; use /renew/{subscription_id} to extend.
    """
    notification_url = f"{settings.WEBHOOK_BASE_URL}/api/v1/webhook/call-records"
    try:
        subscription = await webhook_service.register_webhook(
            ms_tenant_id=current_user.tenant.ms_tenant_id,
            org_name=current_user.tenant.org_name,
            notification_url=notification_url,
        )
    except TokenExpiredError:
        raise HTTPException(
            status_code=502,
            detail="App token rejected by Graph (401). Admin consent may be missing for this tenant.",
        )
    except GraphClientError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail=f"Graph API error: {exc.message}",
        )
    return RegisterWebhookResponse(
        subscription_id=subscription["id"],
        expiration_date_time=subscription["expirationDateTime"],
        notification_url=notification_url,
    )


@router.post(
    "/renew/{subscription_id}",
    response_model=RenewWebhookResponse,
    summary="Renew an existing callRecords webhook subscription",
)
async def renew_webhook(
    subscription_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Extends a Graph subscription expiry by 23 hours.

    - Tenant identified from the admin's JWT.
    - Requires system_role == 'admin'.
    """
    try:
        updated = await webhook_service.renew_webhook(
            ms_tenant_id=current_user.tenant.ms_tenant_id,
            org_name=current_user.tenant.org_name,
            subscription_id=subscription_id,
        )
    except TokenExpiredError:
        raise HTTPException(
            status_code=502,
            detail="App token rejected by Graph (401). Admin consent may be missing for this tenant.",
        )
    except GraphClientError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail=f"Graph API error: {exc.message}",
        )
    return RenewWebhookResponse(
        subscription_id=updated["id"],
        expiration_date_time=updated["expirationDateTime"],
    )


@router.delete(
    "/{subscription_id}",
    status_code=204,
    summary="Delete a callRecords webhook subscription",
)
async def delete_webhook(
    subscription_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Deletes a Graph subscription for the calling admin's tenant.

    - Tenant identified from the admin's JWT.
    - Requires system_role == 'admin'.
    - Returns 204 No Content on success.
    """
    try:
        await webhook_service.delete_webhook(
            ms_tenant_id=current_user.tenant.ms_tenant_id,
            org_name=current_user.tenant.org_name,
            subscription_id=subscription_id,
        )
    except TokenExpiredError:
        raise HTTPException(
            status_code=502,
            detail="App token rejected by Graph (401). Admin consent may be missing for this tenant.",
        )
    except GraphClientError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail=f"Graph API error: {exc.message}",
        )
