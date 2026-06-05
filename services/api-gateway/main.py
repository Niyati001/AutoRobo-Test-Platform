"""
API Gateway — main entry point.

Responsibilities:
- Reverse-proxy /api/v1/* requests to appropriate backend services
- Request aggregation for fleet-overview and system-health
- JWT validation, rate limiting, circuit breaking (via middleware)
- WebSocket proxying to telemetry-service and diagnostics-service
- Prometheus metrics
- OpenAPI docs at /docs
"""

import asyncio
import os
import time
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
import structlog
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from middleware import (
    JWTAuthMiddleware,
    RateLimitMiddleware,
    CircuitBreakerMiddleware,
    RequestLoggingMiddleware,
    get_circuit_breaker,
    CBState,
)

# ---------------------------------------------------------------------------
# Structlog configuration
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Environment / configuration
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

SERVICE_URLS: Dict[str, str] = {
    "simulation-service":      os.getenv("SIMULATION_SERVICE_URL",   "http://simulation-service:8002"),
    "telemetry-service":       os.getenv("TELEMETRY_SERVICE_URL",    "http://telemetry-service:8001"),
    "validation-service":      os.getenv("VALIDATION_SERVICE_URL",   "http://validation-service:8003"),
    "fault-injection-service": os.getenv("FAULT_SERVICE_URL",        "http://fault-injection-service:8004"),
    "diagnostics-service":     os.getenv("DIAGNOSTICS_SERVICE_URL",  "http://diagnostics-service:8005"),
    "auth-service":            os.getenv("AUTH_SERVICE_URL",         "http://auth-service:8006"),
    "analytics-service":       os.getenv("ANALYTICS_SERVICE_URL",    "http://analytics-service:8007"),
    "notification-service":    os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8008"),
}

# Map URL path prefixes → service keys
PATH_PREFIX_TO_SERVICE: Dict[str, str] = {
    "auth":          "auth-service",
    "simulations":   "simulation-service",
    "telemetry":     "telemetry-service",
    "validations":   "validation-service",
    "faults":        "fault-injection-service",
    "diagnostics":   "diagnostics-service",
    "analytics":     "analytics-service",
    "notifications": "notification-service",
}

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
REQUESTS_TOTAL = Counter(
    "gateway_requests_total",
    "Total HTTP requests through the API gateway",
    ["method", "path", "status", "service"],
)
REQUEST_DURATION = Histogram(
    "gateway_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path", "service"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
UPSTREAM_ERRORS_TOTAL = Counter(
    "gateway_upstream_errors_total",
    "Total upstream 5xx errors",
    ["service"],
)

# ---------------------------------------------------------------------------
# HTTP client pool
# ---------------------------------------------------------------------------
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return _http_client


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("api_gateway_starting", services=list(SERVICE_URLS.keys()))
    get_http_client()
    yield
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    logger.info("api_gateway_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Autonomous Robotics Validation Platform — API Gateway",
    description=(
        "Reverse proxy and API gateway for the ARVP microservices platform. "
        "Provides JWT authentication, rate limiting, circuit breaking, "
        "and aggregated endpoints."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware — order matters: outermost wraps innermost.
# Processing order (request): Logging → CORS → CircuitBreaker → RateLimit → JWT
# ---------------------------------------------------------------------------
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CircuitBreakerMiddleware)
app.add_middleware(RateLimitMiddleware, redis_url=REDIS_URL)
app.add_middleware(JWTAuthMiddleware)


# ---------------------------------------------------------------------------
# Helper: forward request to upstream service
# ---------------------------------------------------------------------------

async def _forward_request(
    request: Request,
    service_name: str,
    upstream_path: str,
) -> Response:
    """Forward an incoming request to the target upstream service."""
    base_url = SERVICE_URLS.get(service_name)
    if base_url is None:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_name}")

    cb = get_circuit_breaker(service_name)
    if await cb.is_open():
        UPSTREAM_ERRORS_TOTAL.labels(service=service_name).inc()
        return JSONResponse(
            {"detail": f"Service '{service_name}' circuit is open"},
            status_code=503,
        )

    url = f"{base_url}{upstream_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # Forward all headers except Host; inject user context
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    headers["x-user-id"] = getattr(request.state, "user_id", "")
    headers["x-user-name"] = getattr(request.state, "username", "")
    headers["x-user-role"] = getattr(request.state, "role", "")
    headers["x-forwarded-for"] = request.client.host if request.client else ""
    headers["x-forwarded-proto"] = request.url.scheme

    body = await request.body()
    start = time.perf_counter()

    client = get_http_client()
    try:
        upstream_resp = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError as exc:
        await cb.record_failure()
        UPSTREAM_ERRORS_TOTAL.labels(service=service_name).inc()
        logger.error("upstream_connect_error", service=service_name, url=url, error=str(exc))
        return JSONResponse(
            {"detail": f"Cannot connect to service '{service_name}'"},
            status_code=503,
        )
    except httpx.TimeoutException as exc:
        await cb.record_failure()
        UPSTREAM_ERRORS_TOTAL.labels(service=service_name).inc()
        logger.error("upstream_timeout", service=service_name, url=url, error=str(exc))
        return JSONResponse(
            {"detail": f"Request to '{service_name}' timed out"},
            status_code=504,
        )

    duration = time.perf_counter() - start

    # Prometheus
    REQUEST_DURATION.labels(
        method=request.method,
        path=request.url.path,
        service=service_name,
    ).observe(duration)
    REQUESTS_TOTAL.labels(
        method=request.method,
        path=request.url.path,
        status=str(upstream_resp.status_code),
        service=service_name,
    ).inc()

    if upstream_resp.status_code >= 500:
        await cb.record_failure()
        UPSTREAM_ERRORS_TOTAL.labels(service=service_name).inc()
    else:
        await cb.record_success()

    # Strip hop-by-hop headers
    excluded_headers = {
        "connection", "keep-alive", "transfer-encoding",
        "te", "trailers", "upgrade", "content-encoding",
    }
    response_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in excluded_headers
    }

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Aggregated: GET /api/v1/fleet-overview
# ---------------------------------------------------------------------------
# NOTE: These dedicated routes MUST be registered before the catch-all proxy
# routes below, otherwise FastAPI matches the parameterised route first.

@app.get(
    "/api/v1/fleet-overview",
    summary="Aggregated fleet overview",
    tags=["Aggregation"],
    response_model=None,
)
async def fleet_overview(request: Request) -> JSONResponse:
    """
    Aggregates data from simulation-service, telemetry-service, and diagnostics-service
    to produce a unified fleet status overview.
    """
    client = get_http_client()
    user_headers = {
        "x-user-id":   getattr(request.state, "user_id", ""),
        "x-user-name": getattr(request.state, "username", ""),
        "x-user-role": getattr(request.state, "role", ""),
    }

    async def _safe_get(service: str, path: str) -> Dict[str, Any]:
        cb = get_circuit_breaker(service)
        if await cb.is_open():
            return {"error": f"circuit_open", "service": service}
        base = SERVICE_URLS[service]
        try:
            resp = await client.get(f"{base}{path}", headers=user_headers, timeout=10.0)
            if resp.status_code >= 500:
                await cb.record_failure()
                UPSTREAM_ERRORS_TOTAL.labels(service=service).inc()
                return {"error": f"upstream_{resp.status_code}", "service": service}
            await cb.record_success()
            return resp.json()
        except Exception as exc:
            await cb.record_failure()
            UPSTREAM_ERRORS_TOTAL.labels(service=service).inc()
            logger.error("fleet_overview_upstream_error", service=service, error=str(exc))
            return {"error": str(exc), "service": service}

    # Fan out in parallel
    sim_task = asyncio.create_task(_safe_get("simulation-service", "/simulations/status"))
    tel_task = asyncio.create_task(_safe_get("telemetry-service", "/telemetry/fleet/summary"))
    diag_task = asyncio.create_task(_safe_get("diagnostics-service", "/diagnostics/fleet/summary"))

    sim_data, tel_data, diag_data = await asyncio.gather(sim_task, tel_task, diag_task)

    return JSONResponse(
        {
            "fleet_overview": {
                "timestamp": time.time(),
                "simulation": sim_data,
                "telemetry": tel_data,
                "diagnostics": diag_data,
            }
        }
    )


# ---------------------------------------------------------------------------
# Aggregated: GET /api/v1/system-health
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/system-health",
    summary="Health status of all backend services",
    tags=["Aggregation"],
    response_model=None,
)
async def system_health(request: Request) -> JSONResponse:
    """
    Probes /health on all 8 backend services and returns a consolidated health report.
    """
    client = get_http_client()

    async def _probe_service(name: str, base_url: str) -> Dict[str, Any]:
        cb = get_circuit_breaker(name)
        circuit_state = cb.state.value
        start = time.perf_counter()
        try:
            resp = await client.get(f"{base_url}/health", timeout=5.0)
            latency_ms = (time.perf_counter() - start) * 1000
            healthy = resp.status_code < 400
            if resp.status_code >= 500:
                await cb.record_failure()
            else:
                await cb.record_success()
            try:
                body = resp.json()
            except Exception:
                body = {}
            return {
                "service": name,
                "status": "healthy" if healthy else "degraded",
                "http_status": resp.status_code,
                "latency_ms": round(latency_ms, 2),
                "circuit_breaker": circuit_state,
                "details": body,
            }
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            await cb.record_failure()
            return {
                "service": name,
                "status": "unreachable",
                "error": str(exc),
                "latency_ms": round(latency_ms, 2),
                "circuit_breaker": circuit_state,
            }

    tasks = [
        asyncio.create_task(_probe_service(name, url))
        for name, url in SERVICE_URLS.items()
    ]
    results = await asyncio.gather(*tasks)

    healthy_count = sum(1 for r in results if r.get("status") == "healthy")
    overall = (
        "healthy" if healthy_count == len(results)
        else "degraded" if healthy_count > 0
        else "critical"
    )

    return JSONResponse(
        {
            "overall": overall,
            "timestamp": time.time(),
            "services_healthy": healthy_count,
            "services_total": len(results),
            "services": results,
        },
        status_code=200 if overall != "critical" else 503,
    )


# ---------------------------------------------------------------------------
# Generic catch-all proxy: /api/v1/{service_prefix}/{rest:path}
# ---------------------------------------------------------------------------
# These MUST be registered AFTER the dedicated routes above so that FastAPI
# matches /api/v1/system-health and /api/v1/fleet-overview exactly first.

@app.api_route(
    "/api/v1/{service_prefix}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_request_root(request: Request, service_prefix: str) -> Response:
    """Handles /api/v1/{service} (no trailing path component)."""
    service_name = PATH_PREFIX_TO_SERVICE.get(service_prefix)
    if service_name is None:
        raise HTTPException(status_code=404, detail=f"No service mapped for prefix '{service_prefix}'")
    return await _forward_request(request, service_name, f"/{service_prefix}")


@app.api_route(
    "/api/v1/{service_prefix}/{rest_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_request(request: Request, service_prefix: str, rest_path: str) -> Response:
    service_name = PATH_PREFIX_TO_SERVICE.get(service_prefix)
    if service_name is None:
        raise HTTPException(status_code=404, detail=f"No service mapped for prefix '{service_prefix}'")
    upstream_path = f"/{service_prefix}/{rest_path}" if rest_path else f"/{service_prefix}"
    return await _forward_request(request, service_name, upstream_path)


# ---------------------------------------------------------------------------
# WebSocket proxy: /ws/telemetry/{robot_id}
# ---------------------------------------------------------------------------

@app.websocket("/ws/telemetry/{robot_id}")
async def ws_proxy_telemetry(websocket: WebSocket, robot_id: str) -> None:
    """Proxy WebSocket connections to the telemetry service."""
    await _ws_proxy(
        websocket,
        f"ws://telemetry-service:8001/ws/telemetry/{robot_id}",
    )


@app.websocket("/ws/diagnostics/{robot_id}")
async def ws_proxy_diagnostics(websocket: WebSocket, robot_id: str) -> None:
    """Proxy WebSocket connections to the diagnostics service."""
    await _ws_proxy(
        websocket,
        f"ws://diagnostics-service:8005/ws/diagnostics/{robot_id}",
    )


async def _ws_proxy(client_ws: WebSocket, upstream_url: str) -> None:
    """
    Bidirectional WebSocket proxy. Connects to upstream and relays messages
    in both directions until either side disconnects.
    """
    import websockets  # type: ignore

    await client_ws.accept()
    logger.info("ws_proxy_connecting", upstream=upstream_url)

    try:
        async with websockets.connect(upstream_url) as upstream_ws:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        data = await client_ws.receive_text()
                        await upstream_ws.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning("ws_client_to_upstream_error", error=str(exc))

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream_ws:
                        await client_ws.send_text(
                            message if isinstance(message, str) else message.decode()
                        )
                except Exception as exc:
                    logger.warning("ws_upstream_to_client_error", error=str(exc))

            await asyncio.gather(client_to_upstream(), upstream_to_client())

    except Exception as exc:
        logger.error("ws_proxy_error", upstream=upstream_url, error=str(exc))
        try:
            await client_ws.close(code=1011, reason="Upstream connection failed")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Health check & Prometheus metrics
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
async def health_check() -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "api-gateway", "port": 8000})


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=True,
    )
    return JSONResponse(
        {"detail": "Internal server error", "type": type(exc).__name__},
        status_code=500,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
