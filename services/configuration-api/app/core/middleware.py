"""Request-scoped middleware: correlation IDs and HTTP metrics."""

from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from shared.logging import get_logger, log_context

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind a request ID to the logging context for the request's lifetime.

    An inbound ``X-Request-ID`` is honoured so a trace can span the frontend
    and the API; otherwise one is generated. The value is echoed back on the
    response, which is what makes a support ticket actionable.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id

        with log_context(request_id=request_id):
            response = await call_next(request)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured line per request and record latency metrics."""

    def __init__(self, app, metrics) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = time.perf_counter() - started
            # The route template, not the concrete path, keeps metric
            # cardinality bounded when paths contain IDs.
            route = _route_template(request)
            self._metrics.http_requests_total.labels(
                method=request.method, route=route, status="500"
            ).inc()
            self._metrics.http_latency.labels(method=request.method, route=route).observe(elapsed)
            logger.exception(
                "http_request_failed",
                extra={
                    "http_method": request.method,
                    "http_route": route,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            raise

        elapsed = time.perf_counter() - started
        route = _route_template(request)
        self._metrics.http_requests_total.labels(
            method=request.method, route=route, status=str(response.status_code)
        ).inc()
        self._metrics.http_latency.labels(method=request.method, route=route).observe(elapsed)

        # Probes fire every few seconds; logging them at info level buries
        # everything else.
        level = 10 if request.url.path in ("/health", "/ready", "/metrics") else 20
        logger.log(
            level,
            "http_request",
            extra={
                "http_method": request.method,
                "http_route": route,
                "http_path": request.url.path,
                "http_status": response.status_code,
                "duration_ms": round(elapsed * 1000, 2),
            },
        )
        return response


#: Label used when no route matched. Falling back to the raw path here would
#: let anyone create unbounded Prometheus time series just by requesting
#: random URLs, which is how a monitoring stack gets taken down by its own
#: metrics.
UNMATCHED_ROUTE = "__unmatched__"


def _route_template(request: Request) -> str:
    """Return the matched route pattern, or a constant when nothing matched."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if path else UNMATCHED_ROUTE
