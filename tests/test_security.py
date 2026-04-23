"""
Tests for app/core/security.py

All external dependencies are mocked:
  - Redis client (JWKSCache)
  - httpx.get (JWKS endpoint)
  - jose.jwt functions (TokenVerifier)

Covers:
  - JWKSCache.get_key: Redis cache hit, cache miss + HTTP fetch,
    force-refresh on unknown kid, UnknownSigningKeyError
  - JWKSCache._load_keys: Redis hit, miss → HTTP fetch, HTTP error, network error
  - JWKSCache._find_key: key found, not found
  - TokenVerifier.verify: empty token, malformed token, missing tid,
    unknown tenant, suspended tenant, provisioning tenant,
    expired token, claims mismatch, wrong version, success
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.security import (
    AuthError,
    JWKSCache,
    TenantForbiddenError,
    TenantInfo,
    TokenVerifier,
    UnknownSigningKeyError,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

FAKE_JWKS = {
    "keys": [
        {"kid": "key-001", "kty": "RSA", "n": "abc", "e": "AQAB"},
        {"kid": "key-002", "kty": "RSA", "n": "def", "e": "AQAB"},
    ]
}


def _make_tenant_info(status: str = "active") -> TenantInfo:
    return TenantInfo(
        id=uuid4(),
        org_name="acme",
        db_host="pg-acme.postgres.database.azure.com",
        ms_tenant_id="tid-abc-123",
        status=status,
        plan="pro",
    )


def _make_redis(cached_value=None):
    """Return a mock async Redis client. cached_value=None simulates a cache miss."""
    redis = MagicMock()
    redis.get = AsyncMock(
        return_value=json.dumps(FAKE_JWKS).encode() if cached_value else None
    )
    redis.setex = AsyncMock(return_value=True)
    return redis


# ── JWKSCache._find_key ───────────────────────────────────────────────────────

class TestFindKey:
    def test_returns_matching_key(self):
        keys = FAKE_JWKS["keys"]
        result = JWKSCache._find_key(keys, "key-001")
        assert result is not None
        assert result["kid"] == "key-001"

    def test_returns_none_for_unknown_kid(self):
        keys = FAKE_JWKS["keys"]
        result = JWKSCache._find_key(keys, "key-999")
        assert result is None

    def test_returns_none_for_empty_list(self):
        result = JWKSCache._find_key([], "key-001")
        assert result is None

    def test_returns_second_key(self):
        keys = FAKE_JWKS["keys"]
        result = JWKSCache._find_key(keys, "key-002")
        assert result["kid"] == "key-002"


# ── JWKSCache._load_keys ──────────────────────────────────────────────────────

class TestLoadKeys:
    async def test_returns_from_redis_cache_on_hit(self):
        redis = _make_redis(cached_value=True)
        cache = JWKSCache(redis)
        keys = await cache._load_keys(force_refresh=False)
        assert len(keys) == 2
        redis.get.assert_called_once()

    def _mock_async_client(self, response=None, side_effect=None):
        """Build an AsyncClient context manager mock."""
        mock_http = MagicMock()
        if side_effect:
            mock_http.get = AsyncMock(side_effect=side_effect)
        else:
            mock_http.get = AsyncMock(return_value=response)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_http)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    async def test_fetches_from_microsoft_on_cache_miss(self):
        redis = _make_redis(cached_value=None)
        cache = JWKSCache(redis)
        mock_response = MagicMock()
        mock_response.json.return_value = FAKE_JWKS
        mock_response.raise_for_status.return_value = None

        with patch("app.core.security.httpx.AsyncClient",
                   return_value=self._mock_async_client(response=mock_response)):
            keys = await cache._load_keys(force_refresh=False)

        assert len(keys) == 2

    async def test_stores_fetched_keys_in_redis(self):
        redis = _make_redis(cached_value=None)
        cache = JWKSCache(redis)
        mock_response = MagicMock()
        mock_response.json.return_value = FAKE_JWKS
        mock_response.raise_for_status.return_value = None

        with patch("app.core.security.httpx.AsyncClient",
                   return_value=self._mock_async_client(response=mock_response)):
            await cache._load_keys(force_refresh=False)

        redis.setex.assert_called_once()

    async def test_force_refresh_skips_redis(self):
        redis = _make_redis(cached_value=True)
        cache = JWKSCache(redis)
        mock_response = MagicMock()
        mock_response.json.return_value = FAKE_JWKS
        mock_response.raise_for_status.return_value = None

        with patch("app.core.security.httpx.AsyncClient",
                   return_value=self._mock_async_client(response=mock_response)):
            await cache._load_keys(force_refresh=True)

        redis.get.assert_not_called()

    async def test_http_error_raises_auth_error(self):
        import httpx
        redis = _make_redis(cached_value=None)
        cache = JWKSCache(redis)
        mock_bad_response = MagicMock()
        mock_bad_response.status_code = 503

        with patch("app.core.security.httpx.AsyncClient",
                   return_value=self._mock_async_client(
                       side_effect=httpx.HTTPStatusError(
                           "503", request=MagicMock(), response=mock_bad_response
                       )
                   )):
            with pytest.raises(AuthError):
                await cache._load_keys(force_refresh=False)

    async def test_network_error_raises_auth_error(self):
        import httpx
        redis = _make_redis(cached_value=None)
        cache = JWKSCache(redis)

        with patch("app.core.security.httpx.AsyncClient",
                   return_value=self._mock_async_client(
                       side_effect=httpx.RequestError("connection refused", request=MagicMock())
                   )):
            with pytest.raises(AuthError):
                await cache._load_keys(force_refresh=False)


# ── JWKSCache.get_key ─────────────────────────────────────────────────────────

class TestGetKey:
    async def test_returns_key_on_cache_hit(self):
        redis = _make_redis(cached_value=True)
        cache = JWKSCache(redis)
        key = await cache.get_key("key-001")
        assert key["kid"] == "key-001"

    async def test_force_refreshes_on_unknown_kid(self):
        redis = _make_redis(cached_value=True)
        cache = JWKSCache(redis)

        updated_jwks = {"keys": [{"kid": "key-999", "kty": "RSA"}]}
        call_count = [0]

        async def fake_load(force_refresh):
            call_count[0] += 1
            if call_count[0] == 1:
                return FAKE_JWKS["keys"]
            return updated_jwks["keys"]

        cache._load_keys = fake_load
        key = await cache.get_key("key-999")
        assert key["kid"] == "key-999"

    async def test_raises_unknown_signing_key_error_when_kid_not_found_after_refresh(self):
        redis = _make_redis(cached_value=True)
        cache = JWKSCache(redis)
        cache._load_keys = AsyncMock(return_value=FAKE_JWKS["keys"])

        with pytest.raises(UnknownSigningKeyError):
            await cache.get_key("key-totally-unknown")

    def test_unknown_signing_key_error_is_auth_error_subclass(self):
        with pytest.raises(AuthError):
            raise UnknownSigningKeyError("unknown key")


# ── TokenVerifier.verify ──────────────────────────────────────────────────────

class TestTokenVerifier:
    def _make_verifier(self):
        jwks_cache = MagicMock(spec=JWKSCache)
        jwks_cache.get_key = AsyncMock(return_value={"kid": "key-001", "kty": "RSA"})
        return TokenVerifier(jwks_cache)

    async def _tenant_lookup(self, tid: str) -> TenantInfo | None:
        if tid == "tid-abc-123":
            return _make_tenant_info(status="active")
        return None

    async def test_empty_token_raises_auth_error(self):
        verifier = self._make_verifier()
        with pytest.raises(AuthError):
            await verifier.verify("", self._tenant_lookup)

    async def test_whitespace_only_token_raises_auth_error(self):
        verifier = self._make_verifier()
        with pytest.raises(AuthError):
            await verifier.verify("   ", self._tenant_lookup)

    async def test_malformed_token_raises_auth_error(self):
        verifier = self._make_verifier()
        with pytest.raises(AuthError):
            await verifier.verify("not.a.valid.jwt", self._tenant_lookup)

    async def test_missing_tid_raises_auth_error(self):
        verifier = self._make_verifier()
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims", return_value={"oid": "u1"}):
            with pytest.raises(AuthError, match="tid"):
                await verifier.verify("fake.jwt.token", self._tenant_lookup)

    async def test_unknown_tenant_raises_auth_error(self):
        verifier = self._make_verifier()
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims",
                   return_value={"tid": "unknown-tenant", "oid": "u1"}):
            with pytest.raises(AuthError):
                await verifier.verify("fake.jwt.token", self._tenant_lookup)

    async def test_suspended_tenant_raises_tenant_forbidden_error(self):
        verifier = self._make_verifier()
        async def suspended_lookup(tid):
            return _make_tenant_info(status="suspended")
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims",
                   return_value={"tid": "tid-abc-123", "oid": "u1"}):
            with pytest.raises(TenantForbiddenError):
                await verifier.verify("fake.jwt.token", suspended_lookup)

    async def test_provisioning_tenant_raises_tenant_forbidden_error(self):
        verifier = self._make_verifier()
        async def prov_lookup(tid):
            return _make_tenant_info(status="provisioning")
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims",
                   return_value={"tid": "tid-abc-123", "oid": "u1"}):
            with pytest.raises(TenantForbiddenError):
                await verifier.verify("fake.jwt.token", prov_lookup)

    async def test_expired_token_raises_auth_error(self):
        from jose.exceptions import ExpiredSignatureError
        verifier = self._make_verifier()
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims",
                   return_value={"tid": "tid-abc-123", "oid": "u1"}), \
             patch("app.core.security.jwt.decode", side_effect=ExpiredSignatureError("expired")):
            with pytest.raises(AuthError):
                await verifier.verify("expired.token.here", self._tenant_lookup)

    async def test_wrong_token_version_raises_auth_error(self):
        verifier = self._make_verifier()
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims",
                   return_value={"tid": "tid-abc-123", "oid": "u1"}), \
             patch("app.core.security.jwt.decode",
                   return_value={"tid": "tid-abc-123", "oid": "u1", "ver": "1.0"}):
            with pytest.raises(AuthError, match="version"):
                await verifier.verify("v1.token.here", self._tenant_lookup)

    async def test_success_returns_claims_and_tenant(self):
        verifier = self._make_verifier()
        claims = {"tid": "tid-abc-123", "oid": "user-oid", "ver": "2.0"}
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims",
                   return_value={"tid": "tid-abc-123", "oid": "user-oid"}), \
             patch("app.core.security.jwt.decode", return_value=claims):
            result_claims, result_tenant = await verifier.verify("valid.token", self._tenant_lookup)

        assert result_claims["oid"] == "user-oid"
        assert result_tenant.org_name == "acme"

    async def test_success_tenant_info_is_correct_type(self):
        verifier = self._make_verifier()
        claims = {"tid": "tid-abc-123", "oid": "u1", "ver": "2.0"}
        with patch("app.core.security.jwt.get_unverified_header", return_value={"kid": "k1"}), \
             patch("app.core.security.jwt.get_unverified_claims",
                   return_value={"tid": "tid-abc-123", "oid": "u1"}), \
             patch("app.core.security.jwt.decode", return_value=claims):
            _, tenant = await verifier.verify("valid.token", self._tenant_lookup)

        assert isinstance(tenant, TenantInfo)
        assert tenant.status == "active"
