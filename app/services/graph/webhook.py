# Webhook service — Graph subscription management + notification handling
# Owner: Webhook team
# Depends on: app/services/graph/client.py (DELIVERED)
#             app/services/graph/exceptions.py (DELIVERED)
#             app/config/settings.py (DELIVERED)
#             app/db/central/models.py — Tenant model (PENDING — DB team)
#             workers/celery_app.py — celery_app instance (PENDING — Workers team)
#             workers/tasks/ingestion.py — ingest_meeting_task (PENDING — Workers team)
# See docs/webhook_dependencies.md for full dependency tracking.

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.config.settings import get_settings
from app.services.graph.client import GraphClient, get_access_token_app
from app.services.graph.exceptions import GraphClientError, TokenExpiredError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Deduplication ─────────────────────────────────────────────────────────────
# Graph delivers each notification from multiple IPs simultaneously.
# We track (resource_id, change_type) pairs we've already dispatched to Celery
# within a short window so we don't enqueue the same meeting twice.
# TTL is enforced by _prune_seen(); memory footprint is O(burst_size).

_seen_lock = threading.Lock()
_seen: dict[str, datetime] = {}          # key → first-seen UTC datetime
_DEDUP_WINDOW_SECONDS = 30


def _is_duplicate(key: str) -> bool:
    """Return True if key was seen within the dedup window; record it otherwise."""
    now = datetime.now(timezone.utc)
    with _seen_lock:
        _prune_seen(now)
        if key in _seen:
            return True
        _seen[key] = now
        return False


def _prune_seen(now: datetime) -> None:
    """Remove entries older than the dedup window. Must be called under _seen_lock."""
    cutoff = now - timedelta(seconds=_DEDUP_WINDOW_SECONDS)
    expired = [k for k, ts in _seen.items() if ts < cutoff]
    for k in expired:
        del _seen[k]


# ── Subscription management ───────────────────────────────────────────────────

def register_webhook(
    ms_tenant_id: str,
    org_name: str,
    notification_url: str,
) -> dict:
    """
    Create a Graph callRecords subscription for the given customer tenant.

    Args:
        ms_tenant_id:     Customer's Azure AD tenant GUID (from JWT tid claim).
        org_name:         Customer's org slug (used only for log messages here).
        notification_url: Public HTTPS URL Graph will POST notifications to.
                          Caller builds this from settings.WEBHOOK_BASE_URL.

    Returns:
        The full subscription dict returned by Graph (id, expirationDateTime, …).

    Raises:
        GraphClientError: Graph rejected the request or a network error occurred.
    """
    access_token = get_access_token_app(ms_tenant_id)
    client = GraphClient(access_token)

    expiration = (
        datetime.now(timezone.utc) + timedelta(hours=23)
    ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    body = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": "communications/callRecords",
        "expirationDateTime": expiration,
        "clientState": settings.WEBHOOK_CLIENT_STATE,
    }

    logger.info(
        "Registering callRecords webhook | org=%s | tenant=%s | url=%s",
        org_name, ms_tenant_id, notification_url,
    )

    try:
        subscription = client.post("/subscriptions", body)
    except TokenExpiredError as exc:
        logger.error(
            "register_webhook: app token rejected (401) — admin consent may be missing | "
            "org=%s | tenant=%s | error=%s",
            org_name, ms_tenant_id, exc,
        )
        raise
    except GraphClientError as exc:
        logger.error(
            "register_webhook: Graph API error | org=%s | tenant=%s | "
            "status=%s | error=%s",
            org_name, ms_tenant_id, exc.status_code, exc.message,
        )
        raise

    logger.info(
        "Webhook registered | org=%s | subscription_id=%s | expires=%s",
        org_name, subscription.get("id"), subscription.get("expirationDateTime"),
    )

    return subscription


def renew_webhook(
    ms_tenant_id: str,
    org_name: str,
    subscription_id: str,
) -> dict:
    """
    Extend an existing Graph callRecords subscription by 23 hours.

    Graph subscriptions expire after ~24 hours. Callers should call this
    before expiry (e.g. via a scheduled job).

    Args:
        ms_tenant_id:    Customer's Azure AD tenant GUID.
        org_name:        Customer's org slug (log messages only).
        subscription_id: The Graph subscription ID to renew.

    Returns:
        The updated subscription dict from Graph.

    Raises:
        GraphClientError: Graph rejected the renewal or a network error occurred.
    """
    access_token = get_access_token_app(ms_tenant_id)
    client = GraphClient(access_token)

    new_expiration = (
        datetime.now(timezone.utc) + timedelta(hours=23)
    ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    body = {"expirationDateTime": new_expiration}

    logger.info(
        "Renewing webhook | org=%s | subscription_id=%s", org_name, subscription_id
    )

    try:
        updated = client.patch(f"/subscriptions/{subscription_id}", body)
    except TokenExpiredError as exc:
        logger.error(
            "renew_webhook: app token rejected (401) — admin consent may be missing | "
            "org=%s | subscription_id=%s | error=%s",
            org_name, subscription_id, exc,
        )
        raise
    except GraphClientError as exc:
        logger.error(
            "renew_webhook: Graph API error | org=%s | subscription_id=%s | "
            "status=%s | error=%s",
            org_name, subscription_id, exc.status_code, exc.message,
        )
        raise

    logger.info(
        "Webhook renewed | org=%s | subscription_id=%s | new_expires=%s",
        org_name, subscription_id, updated.get("expirationDateTime"),
    )

    return updated


def delete_webhook(
    ms_tenant_id: str,
    org_name: str,
    subscription_id: str,
) -> None:
    """
    Delete a Graph callRecords subscription.

    Args:
        ms_tenant_id:    Customer's Azure AD tenant GUID.
        org_name:        Customer's org slug (log messages only).
        subscription_id: The Graph subscription ID to delete.

    Raises:
        GraphClientError: Graph rejected the deletion or a network error occurred.
    """
    access_token = get_access_token_app(ms_tenant_id)
    client = GraphClient(access_token)

    logger.info(
        "Deleting webhook | org=%s | subscription_id=%s", org_name, subscription_id
    )

    try:
        client.delete(f"/subscriptions/{subscription_id}")
    except TokenExpiredError as exc:
        logger.error(
            "delete_webhook: app token rejected (401) — admin consent may be missing | "
            "org=%s | subscription_id=%s | error=%s",
            org_name, subscription_id, exc,
        )
        raise
    except GraphClientError as exc:
        logger.error(
            "delete_webhook: Graph API error | org=%s | subscription_id=%s | "
            "status=%s | error=%s",
            org_name, subscription_id, exc.status_code, exc.message,
        )
        raise

    logger.info(
        "Webhook deleted | org=%s | subscription_id=%s", org_name, subscription_id
    )


# ── Notification handling ─────────────────────────────────────────────────────

def handle_notification(payload: dict, db: "Session") -> dict:
    """
    Process a Graph change notification payload for callRecords.

    Flow:
        1. Validate clientState on every notification item.
        2. Extract tenantId + resource (call chain ID) from each notification.
        3. Look up the tenant in the central DB by ms_tenant_id.
        4. Deduplicate — Graph sends the same event from multiple IPs.
        5. Dispatch ingest_meeting_task to Celery by task name string.

    Args:
        payload: Parsed JSON body from Graph — contains a "value" list of
                 notification objects.
        db:      Central DB SQLAlchemy Session (injected by FastAPI dependency
                 get_central_db — PENDING DB team).

    Returns:
        {"accepted": <int>, "skipped": <int>} summary of how many notifications
        were dispatched vs skipped (invalid clientState, unknown tenant, dedup).

    Design notes:
        - We import Tenant and celery_app inside the function body so that this
          module can be imported and unit-tested before the DB team and Workers
          team deliver their files. A top-level import would crash at startup.
        - celery_app.send_task dispatches by task name string — no direct import
          of ingest_meeting_task — keeping team boundaries clean.
        - The function always returns 200-class data; individual notification
          failures are logged and counted as "skipped" rather than raising, so
          Graph does not keep retrying a batch because one item was bad.
    """
    # Deferred imports — these files are PENDING from other teams.
    # Replace with top-level imports once DB team and Workers team deliver.
    from app.db.central.models import Tenant          # noqa: PLC0415  PENDING DB team
    from workers.celery_app import celery_app          # noqa: PLC0415  PENDING Workers team

    notifications = payload.get("value", [])
    accepted = 0
    skipped = 0

    for notification in notifications:
        try:
            # ── Step 1: validate clientState ──────────────────────────────────
            client_state = notification.get("clientState", "")
            if client_state != settings.WEBHOOK_CLIENT_STATE:
                logger.warning(
                    "Notification rejected: clientState mismatch | "
                    "received=%r | expected=<redacted>",
                    client_state,
                )
                skipped += 1
                continue

            # ── Step 2: extract tenantId + resource ───────────────────────────
            # resource looks like "communications/callRecords/<callChainId>"
            notification_tenant_id = notification.get("tenantId", "").strip()
            resource: str = notification.get("resource", "")

            if not notification_tenant_id or not resource:
                logger.warning(
                    "Notification rejected: missing tenantId or resource | "
                    "tenantId=%r | resource=%r",
                    notification_tenant_id, resource,
                )
                skipped += 1
                continue

            # callChainId is the last path segment of the resource URL
            call_chain_id = resource.split("/")[-1]

            if not call_chain_id:
                logger.warning(
                    "Notification rejected: could not extract call_chain_id | "
                    "resource=%r",
                    resource,
                )
                skipped += 1
                continue

            # ── Step 3: tenant lookup ─────────────────────────────────────────
            tenant = db.query(Tenant).filter(
                Tenant.ms_tenant_id == notification_tenant_id
            ).first()

            if tenant is None:
                logger.warning(
                    "Notification skipped: unknown tenant | tenantId=%s",
                    notification_tenant_id,
                )
                skipped += 1
                continue

            if tenant.status != "active":
                logger.warning(
                    "Notification skipped: tenant not active | "
                    "tenantId=%s | org=%s | status=%s",
                    notification_tenant_id, tenant.org_name, tenant.status,
                )
                skipped += 1
                continue

            # ── Step 4: deduplication ─────────────────────────────────────────
            dedup_key = f"{notification_tenant_id}:{call_chain_id}"
            if _is_duplicate(dedup_key):
                logger.debug(
                    "Notification deduplicated | org=%s | call_chain_id=%s",
                    tenant.org_name, call_chain_id,
                )
                skipped += 1
                continue

            # ── Step 5: dispatch to Celery ────────────────────────────────────
            # send_task by name string — does NOT import the task function.
            # Task name must match the registered name in workers/tasks/ingestion.py.
            celery_app.send_task(
                "workers.tasks.ingestion.ingest_meeting_task",
                args=[call_chain_id, tenant.org_name],
            )

            logger.info(
                "Dispatched ingest_meeting_task | org=%s | call_chain_id=%s",
                tenant.org_name, call_chain_id,
            )
            accepted += 1

        except Exception as exc:
            logger.exception(
                "Unexpected error processing notification — skipping | "
                "notification=%r | error=%s",
                notification, exc,
            )
            skipped += 1

    logger.info(
        "handle_notification complete | accepted=%d | skipped=%d",
        accepted, skipped,
    )

    return {"accepted": accepted, "skipped": skipped}
