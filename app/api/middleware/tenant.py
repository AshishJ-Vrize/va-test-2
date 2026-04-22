# Scope A — Request tracing middleware: request_id generation, structured access
#            logging, unverified tid extraction for log context.
# Owner: Graph + Routes team
# Reference: CONTEXT.md Section 12 (middleware behaviour)
#
# What this middleware does on every request:
#   1. Generate a unique request_id (UUID4)
#   2. Peek at the JWT tid claim — unverified, for log context only
#   3. Log request start
#   4. Call the next handler (route or next middleware)
#   5. Log request completion: method, path, status, duration_ms, tid
#   6. Add X-Request-ID to the response headers
#
# What this middleware does NOT do:
#   - Validate the JWT (no signature check, no DB lookup — that is deps.py)
#   - Block any requests (no auth logic)
#   - Route by tenant (that is get_tenant_db in deps.py)
#
# tid peek safety: an attacker can put any tid in an unsigned token. We only
# use the peeked tid in log lines — it has no effect on auth or data access.
# A wrong tid in logs is harmless; the real check happens in TokenVerifier.

from __future__ import annotations

import logging
import time
import uuid

from jose import jwt as jose_jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger(__name__)


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """
    Attaches a unique request_id to every request and logs a structured
    access line on completion. Adds X-Request-ID to every response.

    Registered in main.py:
        app.add_middleware(RequestTracingMiddleware)
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())
        tid = self._peek_tid(request)

        # Make request_id available to route handlers and deps if needed
        request.state.request_id = request_id
        request.state.tid_hint = tid  # unverified — for logging only

        log.info(
            "request started | request_id=%s | method=%s | path=%s | tid=%s",
            request_id, request.method, request.url.path, tid or "unknown",
        )

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.error(
                "request failed with unhandled exception | "
                "request_id=%s | method=%s | path=%s | tid=%s | duration_ms=%d",
                request_id, request.method, request.url.path,
                tid or "unknown", duration_ms,
                exc_info=True,
            )
            raise

        duration_ms = int((time.perf_counter() - start) * 1000)

        log.info(
            "request completed | request_id=%s | method=%s | path=%s | "
            "status=%d | tid=%s | duration_ms=%d",
            request_id, request.method, request.url.path,
            response.status_code, tid or "unknown", duration_ms,
        )

        response.headers["X-Request-ID"] = request_id
        return response

    @staticmethod
    def _peek_tid(request: Request) -> str | None:
        """
        Reads the tid claim from the Authorization header without verifying
        the signature. Used for log context only — not for any security decision.
        Returns None if the header is absent, malformed, or has no tid claim.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        raw_token = auth_header[len("Bearer "):]
        try:
            claims = jose_jwt.get_unverified_claims(raw_token)
            return claims.get("tid")
        except Exception:
            return None
