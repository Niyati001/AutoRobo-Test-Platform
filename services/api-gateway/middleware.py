"""
API Gateway Middleware: JWT Auth, Rate Limiting, Circuit Breaker, Request Logging.
All implemented as Starlette BaseHTTPMiddleware subclasses.
"""

import time
import json
import uuid
import asyncio
from enum import Enum
from typing import Optional, Dict, Callable, Awaitable
from collections import defaultdict

import structlog
import redis.asyncio as aioredis
from jose import jwt, JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
import os
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Paths that do not require authentication
PUBLIC_PATHS = {
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/refresh",
}

# Rate limits per role (requests per minute)
RATE_LIMITS: Dict[str, int] = {
    "ADMIN": 100,
    "OPERATOR": 100,
    "VIEWER": 100,
    "CI_BOT": 1000,
    "anonymous": 20,
}

# Circuit breaker settings
CB_FAILURE_THRESHOLD = 3      # failures before OPEN
CB_RECOVERY_TIMEOUT = 10.0    # seconds before attempting HALF_OPEN
CB_SUCCESS_THRESHOLD = 2      # successes in HALF_OPEN before CLOSED


# ---------------------------------------------------------------------------
# Circuit Breaker State Machine
# ---------------------------------------------------------------------------

class CBState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Per-service circuit breaker. Thread-safe via asyncio lock."""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        self.state: CBState = CBState.CLOSED
        self.failure_count: int = 0
        self.success_count: int = 0
        self.last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    async def record_success(self) -> None:
        async with self._lock:
            if self.state == CBState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= CB_SUCCESS_THRESHOLD:
                    logger.info("circuit_breaker_closed", service=self.service_name)
                    self.state = CBState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
            elif self.state == CBState.CLOSED:
                self.failure_count = 0

    async def record_failure(self) -> None:
        async with self._lock:
            self.last_failure_time = time.monotonic()
            if self.state == CBState.HALF_OPEN:
                logger.warning("circuit_breaker_reopened", service=self.service_name)
                self.state = CBState.OPEN
                self.success_count = 0
            elif self.state == CBState.CLOSED:
                self.failure_count += 1
                if self.failure_count >= CB_FAILURE_THRESHOLD:
                    logger.error(
                        "circuit_breaker_opened",
                        service=self.service_name,
                        failures=self.failure_count,
                    )
                    self.state = CBState.OPEN

    async def is_open(self) -> bool:
        """Returns True if the circuit should block the request."""
        async with self._lock:
            if self.state == CBState.CLOSED:
                return False
            if self.state == CBState.OPEN:
                elapsed = time.monotonic() - self.last_failure_time
                if elapsed >= CB_RECOVERY_TIMEOUT:
                    logger.info("circuit_breaker_half_open", service=self.service_name)
                    self.state = CBState.HALF_OPEN
                    self.success_count = 0
                    return False  # allow one probe request
                return True
            # HALF_OPEN: allow requests through
            return False


# Global registry of circuit breakers, keyed by service hostname
_circuit_breakers: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(service_name: str) -> CircuitBreaker:
    if service_name not in _circuit_breakers:
        _circuit_breakers[service_name] = CircuitBreaker(service_name)
    return _circuit_breakers[service_name]


# ---------------------------------------------------------------------------
# Helper: resolve service name from path
# ---------------------------------------------------------------------------

def _service_from_path(path: str) -> Optional[str]:
    """Map a URL path prefix to an internal service hostname."""
    segments = path.lstrip("/").split("/")
    if len(segments) < 3:
        return None
    # /api/v1/<resource>
    resource = segments[2]
    mapping = {
        "auth": "auth-service",
        "simulations": "simulation-service",
        "telemetry": "telemetry-service",
        "validations": "validation-service",
        "faults": "fault-injection-service",
        "diagnostics": "diagnostics-service",
        "analytics": "analytics-service",
        "notifications": "notification-service",
    }
    return mapping.get(resource)


# ---------------------------------------------------------------------------
# JWTAuthMiddleware
# ---------------------------------------------------------------------------

class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates Bearer JWT tokens. On success, injects X-User-Id and X-User-Role
    headers into the forwarded request. Public paths bypass authentication.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Allow public paths without auth
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/docs"):
            return await call_next(request)

        # WebSocket upgrades: extract token from query param or header
        token: Optional[str] = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        # Also support ?token= for WebSocket connections
        if token is None:
            token = request.query_params.get("token")

        if token is None:
            return JSONResponse(
                {"detail": "Missing authentication token"}, status_code=401
            )

        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except JWTError as exc:
            logger.warning("jwt_validation_failed", error=str(exc))
            return JSONResponse(
                {"detail": "Invalid or expired token"}, status_code=401
            )

        user_id: str = payload.get("sub", "")
        username: str = payload.get("username", "")
        role: str = payload.get("role", "VIEWER")

        # Inject user context headers — Starlette MutableHeaders
        request.state.user_id = user_id
        request.state.username = username
        request.state.role = role

        # We must rebuild the scope to inject headers since Request is immutable
        headers = dict(request.headers)
        headers["x-user-id"] = user_id
        headers["x-user-name"] = username
        headers["x-user-role"] = role

        # Mutate the scope headers in place (ASGI scope is mutable)
        from starlette.datastructures import MutableHeaders
        mutable = MutableHeaders(scope=request.scope)
        mutable["x-user-id"] = user_id
        mutable["x-user-name"] = username
        mutable["x-user-role"] = role

        return await call_next(request)


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter backed by Redis ZADD/ZRANGEBYSCORE.
    Window: 60 seconds. Limits per role defined in RATE_LIMITS.
    """

    def __init__(self, app: ASGIApp, redis_url: str = "redis://redis:6379") -> None:
        super().__init__(app)
        self._redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = await aioredis.from_url(
                self._redis_url, encoding="utf-8", decode_responses=True
            )
        return self._redis

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/docs"):
            return await call_next(request)

        role: str = getattr(request.state, "role", "anonymous")
        user_id: str = getattr(request.state, "user_id", None)

        # Use client IP as fallback identifier for unauthenticated requests
        identifier = user_id or (request.client.host if request.client else "unknown")
        limit = RATE_LIMITS.get(role, RATE_LIMITS["anonymous"])

        now = time.time()
        window_start = now - 60.0
        key = f"ratelimit:{identifier}"

        try:
            redis = await self._get_redis()
            pipe = redis.pipeline()
            # Remove entries outside the sliding window
            pipe.zremrangebyscore(key, "-inf", window_start)
            # Count requests in the current window
            pipe.zcard(key)
            # Add current request timestamp (use unique member to avoid collisions)
            pipe.zadd(key, {f"{now}:{uuid.uuid4().hex}": now})
            # Set expiry on the key so it auto-cleans
            pipe.expire(key, 120)
            results = await pipe.execute()
            current_count: int = results[1]
        except Exception as exc:
            logger.error("rate_limit_redis_error", error=str(exc))
            # Fail open: if Redis is unavailable, allow the request
            return await call_next(request)

        if current_count >= limit:
            logger.warning(
                "rate_limit_exceeded",
                identifier=identifier,
                role=role,
                count=current_count,
                limit=limit,
            )
            return JSONResponse(
                {
                    "detail": "Rate limit exceeded",
                    "limit": limit,
                    "window_seconds": 60,
                    "retry_after": 60,
                },
                status_code=429,
                headers={"Retry-After": "60", "X-RateLimit-Limit": str(limit)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - current_count - 1))
        return response


# ---------------------------------------------------------------------------
# CircuitBreakerMiddleware
# ---------------------------------------------------------------------------

class CircuitBreakerMiddleware(BaseHTTPMiddleware):
    """
    Per-service circuit breaker. Inspects response status codes and records
    5xx responses as failures. Opens the circuit after CB_FAILURE_THRESHOLD
    failures within CB_RECOVERY_TIMEOUT seconds.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        service = _service_from_path(request.url.path)
        if service is None:
            return await call_next(request)

        cb = get_circuit_breaker(service)

        if await cb.is_open():
            logger.error("circuit_open_reject", service=service, path=request.url.path)
            return JSONResponse(
                {
                    "detail": f"Service '{service}' is temporarily unavailable (circuit open)",
                    "service": service,
                },
                status_code=503,
                headers={"Retry-After": str(int(CB_RECOVERY_TIMEOUT))},
            )

        response = await call_next(request)

        if response.status_code >= 500:
            await cb.record_failure()
        else:
            await cb.record_success()

        return response


# ---------------------------------------------------------------------------
# RequestLoggingMiddleware
# ---------------------------------------------------------------------------

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every request with method, path, user, status code, and latency.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-Id", uuid.uuid4().hex)
        start = time.perf_counter()

        # Capture state after JWT middleware has run
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "request_unhandled_exception",
                method=request.method,
                path=request.url.path,
                error=str(exc),
                request_id=request_id,
            )
            raise

        latency_ms = (time.perf_counter() - start) * 1000

        user_id = getattr(request.state, "user_id", "anonymous")
        username = getattr(request.state, "username", "anonymous")
        role = getattr(request.state, "role", "anonymous")

        logger.info(
            "http_request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            query=str(request.query_params),
            status_code=response.status_code,
            latency_ms=round(latency_ms, 2),
            user_id=user_id,
            username=username,
            role=role,
            client_ip=request.client.host if request.client else "unknown",
        )

        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time"] = f"{latency_ms:.2f}ms"
        return response
