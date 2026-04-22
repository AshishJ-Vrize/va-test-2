# Scope A — FastAPI dependencies: get_current_user, get_tenant_db, require_feature
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 11 (dependency signatures and chaining)
#
# Singleton pattern: all process-wide objects (TokenVerifier, JWKSCache, Redis,
# DatabaseManager, TenantRegistry) are created by lifespan in main.py and stored
# on app.state. Dependencies here read from app.state via the injected Request.
#
# Dependency graph (per request, FastAPI caches each Depends result):
#
#   Request ─────────────────────────────────────────────────────────────────────┐
#   HTTPBearer ──────────────────────────────────────────────────────────────────┤
#   get_central_db ──────────────────────────────────────────────────────────────┤
#                                                                                ▼
#                                                               _get_token_data()
#                                                              /               \
#                                                   get_tenant_db()    get_current_user()
#                                                         |                    |
#                                                   (Session)          _upsert_user()

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import AuthError, CurrentUser, TenantForbiddenError, TenantInfo
from app.db.central.session import get_central_db
from app.db.registry import CachedTenant
from app.db.tenant.models import FeaturePermission, User

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_tenant_info(cached: CachedTenant) -> TenantInfo:
    return TenantInfo(
        id=cached.id,
        org_name=cached.org_name,
        db_host=cached.db_host,
        ms_tenant_id=cached.ms_tenant_id,
        status=cached.status,
        plan=cached.plan,
    )


def _upsert_user(
    claims: dict,
    tenant_info: TenantInfo,
    session: Session,
) -> CurrentUser:
    """
    Step 7 of the auth flow: look up the user in the tenant DB by graph_id.
    INSERT on first login, UPDATE last_login_at on every subsequent login.
    Also syncs display_name if it changed in Entra ID.
    """
    graph_id: str = claims["oid"]
    email: str | None = claims.get("preferred_username") or claims.get("email") or None
    name_claim: str | None = claims.get("name")
    display_name = name_claim or (email.split("@")[0] if email else graph_id)

    user = session.query(User).filter(User.graph_id == graph_id).first()
    now = datetime.now(timezone.utc)

    if user is None:
        log.info(
            "auth: first login — creating user record | "
            "oid=%s | email=%s | org=%s",
            graph_id, email, tenant_info.org_name,
        )
        user = User(
            graph_id=graph_id,
            email=email or graph_id,  # graph_id as fallback; email is non-nullable in DB
            display_name=display_name,
            system_role="user",
            is_active=True,
            last_login_at=now,
        )
        session.add(user)
        session.flush()  # populate user.id before reading it below
    else:
        if not user.is_active:
            log.warning(
                "auth: deactivated user blocked | "
                "oid=%s | user_id=%s | org=%s",
                graph_id, str(user.id), tenant_info.org_name,
            )
            raise HTTPException(
                status_code=403,
                detail="Your account has been deactivated. Contact your administrator.",
            )

        user.last_login_at = now

        if name_claim and user.display_name != name_claim:
            log.info(
                "auth: display_name changed in Entra ID — syncing | "
                "oid=%s | old=%r | new=%r",
                graph_id, user.display_name, name_claim,
            )
            user.display_name = name_claim

    log.debug(
        "auth: user resolved | user_id=%s | role=%s | org=%s",
        str(user.id), user.system_role, tenant_info.org_name,
    )

    return CurrentUser(
        id=user.id,
        graph_id=graph_id,
        tid=claims["tid"],
        email=email,
        display_name=user.display_name,
        system_role=user.system_role,
        is_active=user.is_active,
        tenant=tenant_info,
    )


# ── Internal: JWT validation + tenant resolution ──────────────────────────────

def _get_token_data(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    central_db: Session = Depends(get_central_db),
) -> tuple[dict, TenantInfo, CachedTenant]:
    """
    Validates the Bearer token and resolves the tenant.
    FastAPI caches this result per request — JWT is validated once even if
    multiple downstream dependencies all depend on this function.

    Reads TokenVerifier and TenantRegistry from app.state (set by lifespan).
    Returns (claims, tenant_info, cached_tenant).
    """
    if credentials is None:
        log.warning("auth: request received with no Authorization header")
        raise HTTPException(
            status_code=401,
            detail="Authorization header is required. Use 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tenant_registry = request.app.state.tenant_registry

    def tenant_lookup(tid: str) -> TenantInfo | None:
        # Fast path: in-memory TenantRegistry (lock-free read, no DB round-trip)
        cached = tenant_registry.get(tid)
        if cached is not None:
            log.debug("auth: tenant served from registry cache | tid=%s", tid)
            return _to_tenant_info(cached)

        # Slow path: tenant not in cache — refresh from central DB
        log.info(
            "auth: tenant not in registry — fetching from central DB | tid=%s", tid
        )
        refreshed = tenant_registry.refresh_one(tid, central_db)
        if refreshed is None:
            return None
        return _to_tenant_info(refreshed)

    verifier = request.app.state.token_verifier
    try:
        claims, tenant_info = verifier.verify(credentials.credentials, tenant_lookup)
    except TenantForbiddenError as exc:
        log.warning("auth: tenant forbidden | detail=%s", exc.message)
        raise HTTPException(status_code=403, detail=exc.message)
    except AuthError as exc:
        log.warning("auth: token rejected | detail=%s", exc.message)
        raise HTTPException(
            status_code=401,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        )

    tid = claims["tid"]
    cached_tenant = tenant_registry.get(tid)
    if cached_tenant is None:
        # Should be impossible: tenant_lookup just put it in the registry.
        log.error(
            "auth: CachedTenant missing after successful token validation | tid=%s", tid
        )
        raise HTTPException(
            status_code=503,
            detail="Tenant configuration is temporarily unavailable. Please retry.",
        )

    return claims, tenant_info, cached_tenant


# ── Public dependencies ───────────────────────────────────────────────────────

def get_tenant_db(
    request: Request,
    token_data: tuple = Depends(_get_token_data),
) -> Generator[Session, None, None]:
    """
    FastAPI dependency — yields a tenant-scoped SQLAlchemy session.
    Commit, rollback, and close are all handled by DatabaseManager.get_session().
    Key Vault password rotation (28P01) is handled there transparently.

    Usage:
        db: Session = Depends(get_tenant_db)
    """
    claims, _tenant_info, cached_tenant = token_data
    tid = claims["tid"]
    yield from request.app.state.db_manager.get_session(tid, cached_tenant)


def get_current_user(
    token_data: tuple = Depends(_get_token_data),
    tenant_db: Session = Depends(get_tenant_db),
) -> CurrentUser:
    """
    FastAPI dependency — validates JWT, resolves tenant, upserts user.
    FastAPI's Depends caching ensures _get_token_data() and get_tenant_db()
    each run once per request even when declared in multiple dependencies.

    Usage:
        current_user: CurrentUser = Depends(get_current_user)
    """
    claims, tenant_info, _cached_tenant = token_data
    return _upsert_user(claims, tenant_info, tenant_db)


def require_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """
    FastAPI dependency — raises 403 if the user is not an admin.

    Usage:
        current_user: CurrentUser = Depends(require_admin)
    """
    if current_user.system_role != "admin":
        log.warning(
            "auth: non-admin attempted admin action | "
            "user_id=%s | role=%s | org=%s",
            str(current_user.id), current_user.system_role, current_user.tenant.org_name,
        )
        raise HTTPException(
            status_code=403,
            detail="This action requires administrator privileges.",
        )
    return current_user


def require_feature(feature_key: str) -> Callable:
    """
    Returns a FastAPI dependency that raises 403 if the current user cannot
    access the named feature.

    Evaluation order (first match wins):
      1. User-specific row in feature_permissions (target_type='user')
      2. Role-level row (target_type='role')
      3. Default: deny

    Valid feature_key values (enforced by DB check constraint):
      chat | rules_management | insights_view | sentiment_view |
      video_analytics | compliance_dashboard | user_management

    Usage:
        @router.get("/chat", dependencies=[Depends(require_feature("chat"))])
    """

    def _gate(
        current_user: CurrentUser = Depends(get_current_user),
        tenant_db: Session = Depends(get_tenant_db),
    ) -> CurrentUser:
        user_id_str = str(current_user.id)

        # 1. User-specific permission — overrides everything
        user_perm: FeaturePermission | None = (
            tenant_db.query(FeaturePermission)
            .filter(
                FeaturePermission.target_type == "user",
                FeaturePermission.target_id == user_id_str,
                FeaturePermission.feature_key == feature_key,
            )
            .first()
        )
        if user_perm is not None:
            if user_perm.permission == "deny":
                log.warning(
                    "feature gate: user-level deny | "
                    "user_id=%s | feature=%s | org=%s",
                    user_id_str, feature_key, current_user.tenant.org_name,
                )
                raise HTTPException(
                    status_code=403,
                    detail=f"You do not have access to the '{feature_key}' feature.",
                )
            log.debug(
                "feature gate: user-level allow | user_id=%s | feature=%s",
                user_id_str, feature_key,
            )
            return current_user

        # 2. Role-level permission
        role_perm: FeaturePermission | None = (
            tenant_db.query(FeaturePermission)
            .filter(
                FeaturePermission.target_type == "role",
                FeaturePermission.target_id == current_user.system_role,
                FeaturePermission.feature_key == feature_key,
            )
            .first()
        )
        if role_perm is not None:
            if role_perm.permission == "deny":
                log.warning(
                    "feature gate: role-level deny | "
                    "user_id=%s | role=%s | feature=%s | org=%s",
                    user_id_str, current_user.system_role, feature_key,
                    current_user.tenant.org_name,
                )
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Your role ('{current_user.system_role}') does not have "
                        f"access to the '{feature_key}' feature."
                    ),
                )
            log.debug(
                "feature gate: role-level allow | user_id=%s | role=%s | feature=%s",
                user_id_str, current_user.system_role, feature_key,
            )
            return current_user

        # 3. Default: deny — no permission row means access is not granted
        log.warning(
            "feature gate: default deny (no permission row found) | "
            "user_id=%s | role=%s | feature=%s | org=%s",
            user_id_str, current_user.system_role, feature_key,
            current_user.tenant.org_name,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"Access to the '{feature_key}' feature has not been granted "
                "for your account. Contact your administrator."
            ),
        )

    return _gate
