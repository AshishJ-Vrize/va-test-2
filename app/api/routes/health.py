from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    central_db: str
    redis: str


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    central_db_status = "ok"
    redis_status = "ok"

    try:
        async with request.app.state.central_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Health: central DB check failed | error=%s", exc)
        central_db_status = "error"

    try:
        await request.app.state.redis.ping()
    except Exception as exc:
        logger.error("Health: Redis check failed | error=%s", exc)
        redis_status = "error"

    overall = "ok" if central_db_status == "ok" and redis_status == "ok" else "degraded"

    return HealthResponse(
        status=overall,
        central_db=central_db_status,
        redis=redis_status,
    )
