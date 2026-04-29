# Scope A — JWT verification, JWKS cache (Redis), CurrentUser + TenantInfo models
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 8 (verified JWT values) and Section 9 (data models)

import json
import logging
from collections.abc import Awaitable
from typing import Callable
from uuid import UUID

import httpx
import redis.asyncio as redis_lib
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError
from pydantic import BaseModel

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
JWKS_REDIS_KEY = "va:jwks_cache"
JWKS_TTL_SECONDS = 3600  # 1 hour — matches Microsoft's typical rotation window


# ── Data models ───────────────────────────────────────────────────────────────

class TenantInfo(BaseModel):
    id: UUID
    org_name: str
    db_host: str
    ms_tenant_id: str
    status: str
    plan: str


class CurrentUser(BaseModel):
    id: UUID
    graph_id: str
    tid: str
    email: str | None
    display_name: str
    system_role: str
    is_active: bool
    tenant: TenantInfo


# ── Custom exceptions ─────────────────────────────────────────────────────────

class AuthError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class TenantForbiddenError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class UnknownSigningKeyError(AuthError):
    pass


# ── JWKS Cache ────────────────────────────────────────────────────────────────

class JWKSCache:
    """
    Redis-backed JWKS key cache with two-layer rotation handling.

    Layer 1 — Redis (TTL 1 hour): async Redis read per request.
    Layer 2 — Force-refresh on unknown kid: re-fetches once before rejecting.

    All methods are async — uses httpx.AsyncClient and redis.asyncio.
    """

    def __init__(self, redis_client: redis_lib.Redis) -> None:
        self._redis = redis_client
        self._log = logging.getLogger(f"{__name__}.JWKSCache")

    async def get_key(self, kid: str) -> dict:
        keys = await self._load_keys(force_refresh=False)
        matched = self._find_key(keys, kid)

        if matched is None:
            self._log.warning(
                "JWKS: kid not found in cached keys, forcing refresh | kid=%s", kid
            )
            keys = await self._load_keys(force_refresh=True)
            matched = self._find_key(keys, kid)

        if matched is None:
            self._log.error(
                "JWKS: kid not found after force-refresh | kid=%s", kid
            )
            raise UnknownSigningKeyError(
                f"Signing key kid={kid!r} was not found in JWKS even after "
                "a fresh fetch from Microsoft. The token has been rejected."
            )

        return matched

    async def _load_keys(self, force_refresh: bool) -> list[dict]:
        if not force_refresh:
            try:
                cached_raw = await self._redis.get(JWKS_REDIS_KEY)
            except Exception as exc:
                self._log.warning(
                    "JWKS: Redis GET failed — falling through to Microsoft fetch | error=%s", exc
                )
                cached_raw = None
            if cached_raw:
                self._log.debug("JWKS: served from Redis cache")
                return json.loads(cached_raw)["keys"]
            self._log.info("JWKS: Redis cache miss — fetching from Microsoft")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(JWKS_URL, timeout=10.0)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._log.error(
                "JWKS: HTTP error | status=%s", exc.response.status_code
            )
            raise AuthError(
                f"JWKS endpoint returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            self._log.error("JWKS: network error | error=%s", exc)
            raise AuthError(
                f"Failed to reach Microsoft JWKS endpoint: {exc}."
            ) from exc

        data = response.json()
        key_count = len(data.get("keys", []))
        try:
            await self._redis.setex(JWKS_REDIS_KEY, JWKS_TTL_SECONDS, json.dumps(data))
            self._log.info(
                "JWKS: fetched and cached | key_count=%d | ttl=%ds",
                key_count, JWKS_TTL_SECONDS,
            )
        except Exception as exc:
            self._log.warning(
                "JWKS: Redis SETEX failed — keys fetched but not cached | error=%s", exc
            )
        return data["keys"]

    @staticmethod
    def _find_key(keys: list[dict], kid: str) -> dict | None:
        for key in keys:
            if key.get("kid") == kid:
                return key
        return None


# ── Token Verifier ────────────────────────────────────────────────────────────

class TokenVerifier:
    """
    Executes JWT validation steps 1–6 from CONTEXT.md Section 8.
    Step 7 (user upsert) belongs in deps.py — has access to tenant DB session.

    tenant_lookup is an async callable: tid → TenantInfo | None.
    """

    def __init__(self, jwks_cache: JWKSCache) -> None:
        self._jwks = jwks_cache
        self._settings = get_settings()
        self._log = logging.getLogger(f"{__name__}.TokenVerifier")

    async def verify(
        self,
        raw_token: str,
        tenant_lookup: Callable[[str], Awaitable[TenantInfo | None]],
    ) -> tuple[dict, TenantInfo]:
        # ── Step 1: guard against empty input ─────────────────────────────────
        if not raw_token or not raw_token.strip():
            self._log.warning("JWT step 1 failed: empty or missing token")
            raise AuthError("Authorization token is missing or empty.")

        # ── Step 2: decode WITHOUT signature verification ─────────────────────
        try:
            unverified_header = jwt.get_unverified_header(raw_token)
            unverified_claims = jwt.get_unverified_claims(raw_token)
        except JWTError as exc:
            self._log.warning("JWT step 2 failed: malformed | error=%s", exc)
            raise AuthError(f"Token is malformed: {exc}") from exc

        tid = unverified_claims.get("tid", "").strip()
        kid = unverified_header.get("kid", "").strip()

        if not tid:
            raise AuthError("Token is missing the 'tid' claim.")
        if not kid:
            raise AuthError("Token header is missing the 'kid' field.")

        self._log.debug("JWT step 2 OK | tid=%s | kid=%s", tid, kid)

        # ── Step 3: tenant lookup ─────────────────────────────────────────────
        tenant_info = await tenant_lookup(tid)

        if tenant_info is None:
            self._log.warning("JWT step 3 failed: unregistered tenant | tid=%s", tid)
            raise AuthError(
                f"Tenant with tid={tid!r} is not registered on this platform."
            )

        if tenant_info.status in ("suspended", "deprovisioned"):
            self._log.warning(
                "JWT step 3 failed: tenant %s | tid=%s", tenant_info.status, tid
            )
            raise TenantForbiddenError(
                f"Tenant '{tenant_info.org_name}' is currently {tenant_info.status}."
            )

        if tenant_info.status == "provisioning":
            raise TenantForbiddenError(
                f"Tenant '{tenant_info.org_name}' is still being provisioned."
            )

        # ── Step 4: fetch matching JWKS key ───────────────────────────────────
        signing_key = await self._jwks.get_key(kid)

        # ── Step 5: full signature + claims verification ───────────────────────
        expected_issuer = f"https://login.microsoftonline.com/{tid}/v2.0"

        try:
            claims = jwt.decode(
                raw_token,
                signing_key,
                algorithms=["RS256"],
                audience=self._settings.AZURE_CLIENT_ID,
                issuer=expected_issuer,
                options={"verify_exp": True},
            )
        except ExpiredSignatureError as exc:
            self._log.warning("JWT step 5 failed: expired | tid=%s", tid)
            raise AuthError("Token has expired. Please re-authenticate.") from exc
        except JWTClaimsError as exc:
            self._log.warning("JWT step 5 failed: claims | tid=%s | error=%s", tid, exc)
            raise AuthError(f"Token claims did not pass validation: {exc}") from exc
        except JWTError as exc:
            self._log.warning("JWT step 5 failed: signature | tid=%s | error=%s", tid, exc)
            raise AuthError(f"Token signature verification failed: {exc}") from exc

        # ── Step 6: version check ─────────────────────────────────────────────
        token_ver = claims.get("ver", "")
        if token_ver != "2.0":
            raise AuthError(
                f"Token version {token_ver!r} is not supported. Requires Entra ID v2.0."
            )

        self._log.info(
            "JWT validation successful | tid=%s | oid=%s | org=%s",
            tid, claims.get("oid"), tenant_info.org_name,
        )

        return claims, tenant_info
