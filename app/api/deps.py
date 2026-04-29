# Scope A — FastAPI dependencies: get_current_user, get_tenant_db, require_feature
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 11 (dependency signatures and chaining)

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _upsert_user(
    claims: dict,
    tenant_info: TenantInfo,
    session: AsyncSession,
) -> CurrentUser:
    graph_id: str = claims["oid"]
    email: str | None = claims.get("preferred_username") or claims.get("email") or None
    name_claim: str | None = claims.get("name")
    display_name = name_claim or (email.split("@")[0] if email else graph_id)

    try:
        result = await session.execute(select(User).where(User.graph_id == graph_id))
        user = result.scalar_one_or_none()
    except Exception as exc:
        log.exception(
            "auth: DB error during user lookup | oid=%s | org=%s | error=%s",
            graph_id, tenant_info.org_name, exc,
        )
        raise HTTPException(
            status_code=503,
            detail="Authentication service temporarily unavailable. Please try again.",
        ) from exc

    now = datetime.now(timezone.utc)

    if user is None:
        log.info(
            "auth: first login — creating user record | oid=%s | email=%s | org=%s",
            graph_id, email, tenant_info.org_name,
        )
        user = User(
            graph_id=graph_id,
            email=email or graph_id,
            display_name=display_name,
            system_role="user",
            is_active=True,
            last_login_at=now,
        )
        session.add(user)
        try:
            await session.flush()
        except Exception as exc:
            log.exception(
                "auth: DB error creating user record | oid=%s | org=%s | error=%s",
                graph_id, tenant_info.org_name, exc,
            )
            raise HTTPException(
                status_code=503,
                detail="Authentication service temporarily unavailable. Please try again.",
            ) from exc
    else:
        if not user.is_active:
            log.warning(
                "auth: deactivated user blocked | oid=%s | user_id=%s | org=%s",
                graph_id, str(user.id), tenant_info.org_name,
            )
            raise HTTPException(
                status_code=403,
                detail="Your account has been deactivated. Contact your administrator.",
            )

        user.last_login_at = now

        if name_claim and user.display_name != name_claim:
            log.info(
                "auth: display_name changed — syncing | oid=%s | old=%r | new=%r",
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

async def _get_token_data(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    central_db: AsyncSession = Depends(get_central_db),
) -> tuple[dict, TenantInfo, CachedTenant]:
    if credentials is None:
        log.warning("auth: request received with no Authorization header")
        raise HTTPException(
            status_code=401,
            detail="Authorization header is required. Use 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tenant_registry = request.app.state.tenant_registry

    async def tenant_lookup(tid: str) -> TenantInfo | None:
        cached = tenant_registry.get(tid)
        if cached is not None:
            log.debug("auth: tenant served from registry cache | tid=%s", tid)
            return _to_tenant_info(cached)
        log.info("auth: tenant not in registry — fetching from central DB | tid=%s", tid)
        refreshed = await tenant_registry.refresh_one(tid, central_db)
        if refreshed is None:
            return None
        return _to_tenant_info(refreshed)

    verifier = request.app.state.token_verifier
    try:
        claims, tenant_info = await verifier.verify(credentials.credentials, tenant_lookup)
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
        log.error("auth: CachedTenant missing after validation | tid=%s", tid)
        raise HTTPException(status_code=503, detail="Tenant configuration temporarily unavailable.")

    return claims, tenant_info, cached_tenant


# ── Public dependencies ───────────────────────────────────────────────────────

async def get_tenant_db(
    request: Request,
    token_data: tuple = Depends(_get_token_data),
) -> AsyncGenerator[AsyncSession, None]:
    claims, _tenant_info, cached_tenant = token_data
    tid = claims["tid"]
    async with request.app.state.db_manager.get_session(tid, cached_tenant) as session:
        yield session


async def get_current_user(
    token_data: tuple = Depends(_get_token_data),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> CurrentUser:
    claims, tenant_info, _cached_tenant = token_data
    return await _upsert_user(claims, tenant_info, tenant_db)


async def require_super_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if current_user.system_role != "compliance_officer":
        log.warning(
            "auth: non-super-admin attempted super admin action | user_id=%s | role=%s | org=%s",
            str(current_user.id), current_user.system_role, current_user.tenant.org_name,
        )
        raise HTTPException(status_code=403, detail="This action requires super admin privileges.")
    return current_user


def require_feature(feature_key: str) -> Callable:
    async def _gate(
        current_user: CurrentUser = Depends(get_current_user),
        tenant_db: AsyncSession = Depends(get_tenant_db),
    ) -> CurrentUser:
        user_id_str = str(current_user.id)

        try:
            result = await tenant_db.execute(
                select(FeaturePermission).where(
                    FeaturePermission.target_type == "user",
                    FeaturePermission.target_id == user_id_str,
                    FeaturePermission.feature_key == feature_key,
                )
            )
            user_perm = result.scalar_one_or_none()
        except Exception as exc:
            log.exception(
                "feature gate: DB error on user-level permission check | "
                "user_id=%s | feature=%s | org=%s | error=%s",
                user_id_str, feature_key, current_user.tenant.org_name, exc,
            )
            raise HTTPException(
                status_code=503,
                detail="Feature access check temporarily unavailable. Please try again.",
            ) from exc

        if user_perm is not None:
            if user_perm.permission == "deny":
                raise HTTPException(
                    status_code=403,
                    detail=f"You do not have access to the '{feature_key}' feature.",
                )
            return current_user

        try:
            result = await tenant_db.execute(
                select(FeaturePermission).where(
                    FeaturePermission.target_type == "role",
                    FeaturePermission.target_id == current_user.system_role,
                    FeaturePermission.feature_key == feature_key,
                )
            )
            role_perm = result.scalar_one_or_none()
        except Exception as exc:
            log.exception(
                "feature gate: DB error on role-level permission check | "
                "user_id=%s | role=%s | feature=%s | org=%s | error=%s",
                user_id_str, current_user.system_role, feature_key,
                current_user.tenant.org_name, exc,
            )
            raise HTTPException(
                status_code=503,
                detail="Feature access check temporarily unavailable. Please try again.",
            ) from exc

        if role_perm is not None:
            if role_perm.permission == "deny":
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Your role ('{current_user.system_role}') does not have "
                        f"access to the '{feature_key}' feature."
                    ),
                )
            return current_user

        log.warning(
            "feature gate: default deny | user_id=%s | feature=%s | org=%s",
            user_id_str, feature_key, current_user.tenant.org_name,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Access to '{feature_key}' has not been granted for your account.",
        )

    return _gate
