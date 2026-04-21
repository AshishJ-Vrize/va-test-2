# Scope A — JWT verification, JWKS cache (Redis), CurrentUser + TenantInfo models
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 8 (verified JWT values) and Section 9 (data models)

import json
import logging
from typing import Callable
from uuid import UUID

import httpx
import redis as redis_lib
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError
from pydantic import BaseModel

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
JWKS_REDIS_KEY = "va:jwks_cache"
JWKS_TTL_SECONDS = 3600  # 1 hour — matches Microsoft's typical rotation window


# ── Data models ───────────────────────────────────────────────────────────────
# These are the two objects that flow through every authenticated request.
# TenantInfo is built from the central DB tenants row.
# CurrentUser is built after step 7 in deps.py (user upsert).
# Both are defined here so all Scope A files import from one place.

class TenantInfo(BaseModel):
    id: UUID          # tenants.id — internal UUID primary key
    org_name: str     # tenants.org_name — slug used in Key Vault secret name
    db_host: str      # tenants.db_host — Azure PostgreSQL Flexible Server hostname
    ms_tenant_id: str # tenants.ms_tenant_id — the JWT tid claim
    status: str       # always 'active' by the time CurrentUser is issued
    plan: str         # tenants.plan — trial / starter / pro / enterprise


class CurrentUser(BaseModel):
    id: UUID              # users.id in tenant DB — UUID primary key
    graph_id: str         # JWT oid claim — maps to users.graph_id
    tid: str              # JWT tid claim — same as tenant.ms_tenant_id
    email: str | None     # preferred_username or email claim; None if absent
    display_name: str     # name claim; falls back to email prefix if name absent
    system_role: str      # users.system_role — 'user' | 'admin' | 'compliance_officer'
    is_active: bool       # users.is_active
    tenant: TenantInfo    # resolved from central DB during JWT validation


# ── Custom exceptions ─────────────────────────────────────────────────────────
# Separate exception types let deps.py map failures to the right HTTP status
# without parsing string messages or catching broad Exception.

class AuthError(Exception):
    """401-class failure: token missing, malformed, expired, or fails signature check."""
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class TenantForbiddenError(Exception):
    """403-class failure: tenant exists in central DB but is not in 'active' status."""
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class UnknownSigningKeyError(AuthError):
    """401-class failure: kid in JWT header has no matching key in JWKS after refresh."""


# ── JWKS Cache ────────────────────────────────────────────────────────────────

class JWKSCache:
    """
    Redis-backed JWKS key cache with two-layer rotation handling.

    Layer 1 — Redis (TTL 1 hour): one Redis read per request instead of one
    HTTP call to Microsoft. Fast path for the 99.9% case.

    Layer 2 — Force-refresh on unknown kid: if a token arrives signed with a
    key not in the cache, the cache may be stale due to Microsoft key rotation.
    We re-fetch once before rejecting. This limits the blast radius of a rotation
    event to a single failed request per process rather than an outage.

    Lifecycle: instantiated in FastAPI lifespan, injected into TokenVerifier.
    Thread-safety: redis.Redis is thread-safe; _load_keys has no shared mutable state.
    """

    def __init__(self, redis_client: redis_lib.Redis) -> None:
        self._redis = redis_client
        self._log = logging.getLogger(f"{__name__}.JWKSCache")

    def get_key(self, kid: str) -> dict:
        """
        Returns the JWKS key dict whose 'kid' field matches the given kid.

        Tries the Redis cache first. If the kid is not found in cached keys,
        forces a fresh fetch from Microsoft before raising UnknownSigningKeyError.
        This handles JWKS key rotation without requiring a service restart.
        """
        keys = self._load_keys(force_refresh=False)
        matched = self._find_key(keys, kid)

        if matched is None:
            # kid absent from cache — Microsoft may have rotated keys.
            # Force one fresh fetch before deciding the key is truly unknown.
            self._log.warning(
                "JWKS: kid not found in cached keys, forcing refresh | kid=%s", kid
            )
            keys = self._load_keys(force_refresh=True)
            matched = self._find_key(keys, kid)

        if matched is None:
            self._log.error(
                "JWKS: kid not found after force-refresh — token likely tampered or "
                "signed by an unrecognised key | kid=%s", kid
            )
            raise UnknownSigningKeyError(
                f"Signing key kid={kid!r} was not found in JWKS even after "
                "a fresh fetch from Microsoft. The token has been rejected."
            )

        return matched

    def _load_keys(self, force_refresh: bool) -> list[dict]:
        """
        Returns the list of JWKS key objects.

        If force_refresh is False, checks Redis first.
        On a cache miss (or force_refresh=True), fetches from Microsoft's JWKS
        endpoint and stores the result in Redis with a 1-hour TTL.
        """
        if not force_refresh:
            cached_raw = self._redis.get(JWKS_REDIS_KEY)
            if cached_raw:
                self._log.debug("JWKS: served from Redis cache | key=%s", JWKS_REDIS_KEY)
                return json.loads(cached_raw)["keys"]
            self._log.info("JWKS: Redis cache miss — fetching from Microsoft | url=%s", JWKS_URL)

        try:
            response = httpx.get(JWKS_URL, timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._log.error(
                "JWKS: HTTP error fetching keys | url=%s | status=%s",
                JWKS_URL, exc.response.status_code,
            )
            raise AuthError(
                f"JWKS endpoint returned HTTP {exc.response.status_code}. "
                "Cannot validate token signature."
            ) from exc
        except httpx.RequestError as exc:
            self._log.error(
                "JWKS: network error fetching keys | url=%s | error=%s", JWKS_URL, exc
            )
            raise AuthError(
                f"Failed to reach Microsoft JWKS endpoint: {exc}. "
                "Cannot validate token signature."
            ) from exc

        data = response.json()
        key_count = len(data.get("keys", []))

        self._redis.setex(JWKS_REDIS_KEY, JWKS_TTL_SECONDS, json.dumps(data))
        self._log.info(
            "JWKS: fetched and cached | key_count=%d | ttl=%ds", key_count, JWKS_TTL_SECONDS
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
    Step 7 (user upsert, last_login_at update) is NOT here — it belongs in
    deps.py get_current_user, which has access to the tenant DB session.

    Lifecycle: instantiated in FastAPI lifespan after JWKSCache is ready,
    stored in app.state.token_verifier, accessed via a FastAPI dependency.

    tenant_lookup design: TokenVerifier does not import from app.db to avoid
    coupling security infrastructure to the DB team's models. The caller (deps.py)
    passes a callable that accepts a tid string and returns TenantInfo or None.
    This makes TokenVerifier independently testable with a mock lookup.
    """

    def __init__(self, jwks_cache: JWKSCache) -> None:
        self._jwks = jwks_cache
        self._settings = get_settings()
        self._log = logging.getLogger(f"{__name__}.TokenVerifier")

    def verify(
        self,
        raw_token: str,
        tenant_lookup: Callable[[str], TenantInfo | None],
    ) -> tuple[dict, TenantInfo]:
        """
        Validates a raw Bearer token string end-to-end.

        Args:
            raw_token:      Token string extracted from Authorization header,
                            with the 'Bearer ' prefix already stripped.
            tenant_lookup:  Callable provided by deps.py. Takes a tid string,
                            returns TenantInfo if that tenant is registered,
                            None if unknown.

        Returns:
            (claims, tenant_info) where claims is the verified JWT payload dict
            and tenant_info is the resolved TenantInfo for this tenant.

        Raises:
            AuthError:             401-class — bad token, unknown tenant, bad
                                   signature, expired, wrong version.
            TenantForbiddenError:  403-class — tenant found but not 'active'.
        """

        # ── Step 1: guard against empty input ─────────────────────────────────
        if not raw_token or not raw_token.strip():
            self._log.warning("JWT step 1 failed: empty or missing token")
            raise AuthError("Authorization token is missing or empty.")

        # ── Step 2: decode WITHOUT signature verification ─────────────────────
        # We need tid (to look up the tenant) and kid (to find the signing key)
        # before we can do full verification. python-jose lets us read these
        # without touching the signature.
        try:
            unverified_header = jwt.get_unverified_header(raw_token)
            unverified_claims = jwt.get_unverified_claims(raw_token)
        except JWTError as exc:
            self._log.warning("JWT step 2 failed: token is malformed | error=%s", exc)
            raise AuthError(f"Token is malformed and cannot be decoded: {exc}") from exc

        tid = unverified_claims.get("tid", "").strip()
        kid = unverified_header.get("kid", "").strip()

        if not tid:
            self._log.warning("JWT step 2 failed: tid claim absent | kid=%s", kid)
            raise AuthError(
                "Token is missing the 'tid' (tenant ID) claim. "
                "Only Entra ID v2.0 tokens from registered tenants are accepted."
            )

        if not kid:
            self._log.warning("JWT step 2 failed: kid header absent | tid=%s", tid)
            raise AuthError(
                "Token header is missing the 'kid' (key ID) field. "
                "The token cannot be matched to a signing key."
            )

        self._log.debug("JWT step 2 OK | tid=%s | kid=%s", tid, kid)

        # ── Step 3: tenant lookup ─────────────────────────────────────────────
        # The lookup hits an in-memory TenantRegistry cache in normal operation.
        # Only on a registry miss does it query the central DB.
        tenant_info = tenant_lookup(tid)

        if tenant_info is None:
            self._log.warning(
                "JWT step 3 failed: unregistered tenant | tid=%s", tid
            )
            raise AuthError(
                f"Tenant with tid={tid!r} is not registered on this platform. "
                "Contact your administrator if you believe this is an error."
            )

        if tenant_info.status in ("suspended", "deprovisioned"):
            self._log.warning(
                "JWT step 3 failed: tenant not active | tid=%s | org=%s | status=%s",
                tid, tenant_info.org_name, tenant_info.status,
            )
            raise TenantForbiddenError(
                f"Tenant '{tenant_info.org_name}' is currently {tenant_info.status}. "
                "Access is not permitted. Contact your platform administrator."
            )

        if tenant_info.status == "provisioning":
            self._log.warning(
                "JWT step 3 failed: tenant provisioning | tid=%s | org=%s",
                tid, tenant_info.org_name,
            )
            raise TenantForbiddenError(
                f"Tenant '{tenant_info.org_name}' is still being provisioned. "
                "Please try again in a few minutes."
            )

        self._log.debug("JWT step 3 OK | tid=%s | org=%s | status=%s",
                        tid, tenant_info.org_name, tenant_info.status)

        # ── Step 4: fetch matching JWKS key ───────────────────────────────────
        # JWKSCache handles Redis hit/miss and force-refresh on unknown kid.
        # Raises UnknownSigningKeyError (subclass of AuthError) if key not found.
        signing_key = self._jwks.get_key(kid)

        # ── Step 5: full signature + claims verification ───────────────────────
        # iss is constructed per-token because each tenant has a different issuer.
        # Hardcoding iss would reject every token except the platform's own tenant.
        # See CONTEXT.md Section 8 "Why iss cannot be a static string".
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
            self._log.warning(
                "JWT step 5 failed: token expired | tid=%s | kid=%s", tid, kid
            )
            raise AuthError(
                "Token has expired. Please re-authenticate and try again."
            ) from exc
        except JWTClaimsError as exc:
            self._log.warning(
                "JWT step 5 failed: claims mismatch | tid=%s | kid=%s | error=%s",
                tid, kid, exc,
            )
            raise AuthError(
                f"Token claims did not pass validation: {exc}. "
                "Ensure the token was issued for this application."
            ) from exc
        except JWTError as exc:
            self._log.warning(
                "JWT step 5 failed: signature or structure error | "
                "tid=%s | kid=%s | error=%s", tid, kid, exc,
            )
            raise AuthError(
                f"Token signature verification failed: {exc}. "
                "The token may have been tampered with."
            ) from exc

        # ── Step 6: version check ─────────────────────────────────────────────
        # We only accept v2.0 tokens. v1.0 tokens have a different claim set
        # (e.g., no 'oid' claim in the same position) and are not supported.
        token_ver = claims.get("ver", "")
        if token_ver != "2.0":
            self._log.warning(
                "JWT step 6 failed: unsupported token version | "
                "tid=%s | ver=%s", tid, token_ver,
            )
            raise AuthError(
                f"Token version {token_ver!r} is not supported. "
                "This API requires Entra ID v2.0 tokens. "
                "Check your MSAL authority configuration."
            )

        self._log.info(
            "JWT validation successful | tid=%s | oid=%s | org=%s",
            tid, claims.get("oid"), tenant_info.org_name,
        )

        return claims, tenant_info
