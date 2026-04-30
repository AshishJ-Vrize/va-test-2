"""
Tests for app/services/graph/webhook.py

All external calls are mocked:
  - get_access_token_app (MSAL token acquisition)
  - GraphClient.post / patch / delete (Graph API calls)
  - workers.celery_app (Celery — pending Workers team delivery, mocked via sys.modules)
  - app.db.central.models.Tenant (DB model, mocked inline)

Covers:
  - _is_duplicate: first call returns False, second call returns True
  - _prune_seen: removes entries outside the dedup window
  - register_webhook: success, re-raises TokenExpiredError, re-raises GraphClientError
  - renew_webhook: success, re-raises errors
  - delete_webhook: success, re-raises errors
  - handle_notification: valid notification dispatched, clientState mismatch skipped,
    missing tenantId/resource skipped, unknown tenant skipped, inactive tenant skipped,
    duplicate skipped, multiple notifications mixed result
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Set env vars before module-level imports trigger get_settings() ──────────
# webhook.py calls get_settings() at module level, so we must set env vars
# before importing it — the autouse fixture only runs per-test, too late here.
_TEST_ENV = {
    "AZURE_CLIENT_ID": "test-client-id",
    "AZURE_CLIENT_SECRET": "test-client-secret",
    "AZURE_TENANT_ID": "test-tenant-id",
    "CENTRAL_DB_URL": "postgresql+psycopg2://user:pass@localhost/central?sslmode=require",
    "TENANT_DB_USER": "tenant_user",
    "AZURE_KEYVAULT_URL": "https://test-vault.vault.azure.net",
    "REDIS_URL": "rediss://:pass@localhost:6380/0",
    "WEBHOOK_BASE_URL": "https://test.example.com",
    "WEBHOOK_CLIENT_STATE": "test-webhook-secret",
    "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
    "AZURE_OPENAI_API_KEY": "test-openai-key",
    "AZURE_OPENAI_DEPLOYMENT_EMBEDDING": "text-embedding-3-small",
    "AZURE_OPENAI_DEPLOYMENT_LLM": "gpt-4o",
    "AZURE_TEXT_ANALYTICS_ENDPOINT": "https://test.cognitiveservices.azure.com",
    "AZURE_TEXT_ANALYTICS_KEY": "test-text-analytics-key",
    "TENANT_DB_PASSWORD": "test-password",
    "SENTRY_DSN": "",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

# ── Mock workers.celery_app before importing webhook.py ─────────────────────
# workers/celery_app.py is pending the Workers team. We install a fake module
# so handle_notification's deferred import doesn't raise ImportError.
_fake_celery = SimpleNamespace(celery_app=MagicMock())
sys.modules.setdefault("workers", SimpleNamespace(celery_app=_fake_celery))
sys.modules.setdefault("workers.celery_app", _fake_celery)
sys.modules.setdefault("workers.tasks", SimpleNamespace())
sys.modules.setdefault("workers.tasks.ingestion", SimpleNamespace())

import app.services.graph.webhook as wh  # noqa: E402  must come after sys.modules patch


# ── Autouse fixture: clear dedup store before every test ─────────────────────
# pytest-asyncio supports async fixtures but NOT async setup_method.
# This fixture replaces the per-class async setup_method pattern.

@pytest.fixture(autouse=True)
async def clear_seen():
    async with wh._seen_lock:
        wh._seen.clear()


# ── _is_duplicate ─────────────────────────────────────────────────────────────

class TestIsDuplicate:

    async def test_first_call_returns_false(self):
        assert await wh._is_duplicate("tenant:call-001") is False

    async def test_second_call_returns_true(self):
        await wh._is_duplicate("tenant:call-002")
        assert await wh._is_duplicate("tenant:call-002") is True

    async def test_different_keys_are_independent(self):
        await wh._is_duplicate("tenant:call-a")
        assert await wh._is_duplicate("tenant:call-b") is False

    async def test_expired_entry_is_not_duplicate(self):
        key = "tenant:old-call"
        past = datetime.now(timezone.utc) - timedelta(seconds=wh._DEDUP_WINDOW_SECONDS + 1)
        async with wh._seen_lock:
            wh._seen[key] = past
        # Should be pruned and treated as a new entry
        assert await wh._is_duplicate(key) is False


# ── _prune_seen ───────────────────────────────────────────────────────────────

class TestPruneSeen:
    async def test_removes_expired_entries(self):
        now = datetime.now(timezone.utc)
        past = now - timedelta(seconds=wh._DEDUP_WINDOW_SECONDS + 1)
        async with wh._seen_lock:
            wh._seen["old-key"] = past
            wh._prune_seen(now)
            assert "old-key" not in wh._seen

    async def test_keeps_fresh_entries(self):
        now = datetime.now(timezone.utc)
        fresh = now - timedelta(seconds=5)
        async with wh._seen_lock:
            wh._seen["fresh-key"] = fresh
            wh._prune_seen(now)
            assert "fresh-key" in wh._seen


# ── register_webhook ──────────────────────────────────────────────────────────

class TestRegisterWebhook:
    async def test_returns_subscription_dict(self):
        sub = {"id": "sub-123", "expirationDateTime": "2026-04-23T10:00:00Z"}
        with patch("app.services.graph.webhook.get_access_token_app", return_value="token"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.post = AsyncMock(return_value=sub)
            result = await wh.register_webhook("tid-123", "acme", "https://app.example.com/webhook")
        assert result["id"] == "sub-123"

    async def test_posts_to_subscriptions_endpoint(self):
        sub = {"id": "sub-1"}
        with patch("app.services.graph.webhook.get_access_token_app", return_value="token"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.post = AsyncMock(return_value=sub)
            await wh.register_webhook("tid", "org", "https://example.com/hook")
        call_path = MockClient.return_value.post.call_args[0][0]
        assert call_path == "/subscriptions"

    async def test_body_includes_client_state(self):
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.post = AsyncMock(return_value={"id": "s1"})
            await wh.register_webhook("tid", "org", "https://example.com/hook")
        body = MockClient.return_value.post.call_args[0][1]
        assert "clientState" in body

    async def test_body_includes_change_type_created(self):
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.post = AsyncMock(return_value={"id": "s1"})
            await wh.register_webhook("tid", "org", "https://example.com/hook")
        body = MockClient.return_value.post.call_args[0][1]
        assert body["changeType"] == "created"

    async def test_reraises_token_expired_error(self):
        from app.services.graph.exceptions import TokenExpiredError
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.post = AsyncMock(side_effect=TokenExpiredError("expired"))
            with pytest.raises(TokenExpiredError):
                await wh.register_webhook("tid", "org", "https://example.com/hook")

    async def test_reraises_graph_client_error(self):
        from app.services.graph.exceptions import GraphClientError
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.post = AsyncMock(side_effect=GraphClientError("fail", 500))
            with pytest.raises(GraphClientError):
                await wh.register_webhook("tid", "org", "https://example.com/hook")


# ── renew_webhook ─────────────────────────────────────────────────────────────

class TestRenewWebhook:
    async def test_returns_updated_subscription(self):
        updated = {"id": "sub-1", "expirationDateTime": "2026-04-24T10:00:00Z"}
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.patch = AsyncMock(return_value=updated)
            result = await wh.renew_webhook("tid", "org", "sub-1")
        assert result["expirationDateTime"] == "2026-04-24T10:00:00Z"

    async def test_patches_correct_endpoint(self):
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.patch = AsyncMock(return_value={"id": "sub-1"})
            await wh.renew_webhook("tid", "org", "sub-1")
        call_path = MockClient.return_value.patch.call_args[0][0]
        assert call_path == "/subscriptions/sub-1"

    async def test_body_contains_expiration_date_time(self):
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.patch = AsyncMock(return_value={"id": "sub-1"})
            await wh.renew_webhook("tid", "org", "sub-1")
        body = MockClient.return_value.patch.call_args[0][1]
        assert "expirationDateTime" in body

    async def test_reraises_graph_client_error(self):
        from app.services.graph.exceptions import GraphClientError
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.patch = AsyncMock(side_effect=GraphClientError("err", 503))
            with pytest.raises(GraphClientError):
                await wh.renew_webhook("tid", "org", "sub-1")


# ── delete_webhook ────────────────────────────────────────────────────────────

class TestDeleteWebhook:
    async def test_returns_none_on_success(self):
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.delete = AsyncMock(return_value=None)
            result = await wh.delete_webhook("tid", "org", "sub-1")
        assert result is None

    async def test_deletes_correct_endpoint(self):
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.delete = AsyncMock(return_value=None)
            await wh.delete_webhook("tid", "org", "sub-abc")
        call_path = MockClient.return_value.delete.call_args[0][0]
        assert call_path == "/subscriptions/sub-abc"

    async def test_reraises_graph_client_error(self):
        from app.services.graph.exceptions import GraphClientError
        with patch("app.services.graph.webhook.get_access_token_app", return_value="tok"), \
             patch("app.services.graph.webhook.GraphClient") as MockClient:
            MockClient.return_value.delete = AsyncMock(side_effect=GraphClientError("err", 404))
            with pytest.raises(GraphClientError):
                await wh.delete_webhook("tid", "org", "sub-1")


# ── handle_notification ───────────────────────────────────────────────────────

class TestHandleNotification:
    def _make_db(self, tenant=None):
        mock_tenant = tenant or MagicMock(
            ms_tenant_id="tid-abc",
            org_name="acme",
            status="active",
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_tenant
        db = MagicMock()
        db.execute = AsyncMock(return_value=mock_result)
        return db

    def _valid_notification(self, tenant_id="tid-abc", resource="communications/callRecords/call-001"):
        return {
            "value": [{
                "clientState": wh.settings.WEBHOOK_CLIENT_STATE,
                "tenantId": tenant_id,
                "resource": resource,
            }]
        }

    async def test_valid_notification_accepted(self):
        db = self._make_db()
        result = await wh.handle_notification(self._valid_notification(), db)
        assert result["accepted"] == 1
        assert result["skipped"] == 0

    async def test_wrong_client_state_is_skipped(self):
        db = self._make_db()
        payload = {"value": [{"clientState": "wrong-secret", "tenantId": "tid", "resource": "x/y/z"}]}
        result = await wh.handle_notification(payload, db)
        assert result["accepted"] == 0
        assert result["skipped"] == 1

    async def test_missing_tenant_id_is_skipped(self):
        db = self._make_db()
        payload = {"value": [{
            "clientState": wh.settings.WEBHOOK_CLIENT_STATE,
            "tenantId": "",
            "resource": "communications/callRecords/call-001",
        }]}
        result = await wh.handle_notification(payload, db)
        assert result["skipped"] == 1

    async def test_missing_resource_is_skipped(self):
        db = self._make_db()
        payload = {"value": [{
            "clientState": wh.settings.WEBHOOK_CLIENT_STATE,
            "tenantId": "tid-abc",
            "resource": "",
        }]}
        result = await wh.handle_notification(payload, db)
        assert result["skipped"] == 1

    async def test_unknown_tenant_is_skipped(self):
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        db = MagicMock()
        db.execute = AsyncMock(return_value=mock_result)
        result = await wh.handle_notification(self._valid_notification(), db)
        assert result["skipped"] == 1

    async def test_inactive_tenant_is_skipped(self):
        inactive_tenant = MagicMock(status="suspended", org_name="acme")
        db = self._make_db(tenant=inactive_tenant)
        result = await wh.handle_notification(self._valid_notification(), db)
        assert result["skipped"] == 1

    async def test_duplicate_notification_is_skipped(self):
        db = self._make_db()
        # First call: accepted
        await wh.handle_notification(self._valid_notification(), db)
        # Second call with same resource: duplicate
        result = await wh.handle_notification(self._valid_notification(), db)
        assert result["skipped"] == 1

    async def test_dispatches_celery_task(self):
        db = self._make_db()
        with patch.object(_fake_celery.celery_app, "send_task") as mock_send:
            await wh.handle_notification(self._valid_notification(), db)
        mock_send.assert_called_once()
        task_name = mock_send.call_args[0][0]
        assert "ingest_meeting_task" in task_name

    async def test_empty_value_list_returns_zero_counts(self):
        db = self._make_db()
        result = await wh.handle_notification({"value": []}, db)
        assert result == {"accepted": 0, "skipped": 0}

    async def test_multiple_notifications_counted_independently(self):
        db = self._make_db()
        payload = {
            "value": [
                {
                    "clientState": wh.settings.WEBHOOK_CLIENT_STATE,
                    "tenantId": "tid-abc",
                    "resource": "communications/callRecords/call-A",
                },
                {
                    "clientState": "wrong",   # will be skipped
                    "tenantId": "tid-abc",
                    "resource": "communications/callRecords/call-B",
                },
            ]
        }
        result = await wh.handle_notification(payload, db)
        assert result["accepted"] == 1
        assert result["skipped"] == 1
