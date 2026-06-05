"""
Auth Service — FastAPI application.

Endpoints:
  POST /auth/register         — register new user
  POST /auth/login            — JWT + refresh token
  POST /auth/refresh          — refresh access token
  POST /auth/logout           — invalidate refresh token
  GET  /auth/me               — current user info
  PUT  /auth/me/password      — change password
  POST /auth/api-keys         — create API key
  GET  /auth/api-keys         — list API keys
  DELETE /auth/api-keys/{id}  — revoke API key
  GET  /auth/users            — list users (ADMIN)
  PUT  /auth/users/{id}/role  — update role (ADMIN)
  DELETE /auth/users/{id}     — deactivate user (ADMIN)

Port: 8006
"""

from __future__ import annotations

import os
import uuid
import json
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import structlog
import redis.asyncio as aioredis
import asyncpg
from fastapi import (
    Depends, FastAPI, Header, HTTPException, Request, Security, status
)
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

from models import (
    APIKeyCreate, APIKeyListResponse, APIKeyResponse,
    PasswordChange, RefreshRequest, RoleUpdate,
    TokenResponse, UserCreate, UserListResponse, UserLogin, UserResponse, UserRole,
)
from security import (
    create_access_token, create_refresh_token, decode_access_token,
    decode_refresh_token, generate_api_key, hash_password, verify_password,
    validate_password_strength, PasswordValidationError,
)

# ---------------------------------------------------------------------------
# Structlog
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://arvp:arvp_pass@postgres:5432/arvp_db",
)
# asyncpg DSN format (strip sqlalchemy prefix if present)
_PG_DSN = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379")
JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------
AUTH_ATTEMPTS = Counter(
    "auth_attempts_total",
    "Total authentication attempts",
    ["result"],   # success | failure
)
ACTIVE_SESSIONS = Gauge(
    "active_sessions_total",
    "Approximate number of active sessions (refresh tokens in Redis)",
)

# ---------------------------------------------------------------------------
# DB + Redis pools (module-level, initialized in lifespan)
# ---------------------------------------------------------------------------
_db_pool: Optional[asyncpg.Pool] = None
_redis: Optional[aioredis.Redis] = None


async def get_db() -> asyncpg.Pool:
    if _db_pool is None:
        raise RuntimeError("DB pool not initialized")
    return _db_pool


async def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis


# ---------------------------------------------------------------------------
# DB schema bootstrap
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        VARCHAR(64) UNIQUE NOT NULL,
    email           VARCHAR(254) UNIQUE NOT NULL,
    hashed_password VARCHAR(256) NOT NULL,
    role            VARCHAR(32) NOT NULL DEFAULT 'VIEWER',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash     VARCHAR(64) NOT NULL UNIQUE,
    name         VARCHAR(128) NOT NULL,
    permissions  JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ,
    last_used    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    action      VARCHAR(128) NOT NULL,
    resource    VARCHAR(128) NOT NULL,
    resource_id VARCHAR(256),
    ip_address  VARCHAR(64),
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details     JSONB NOT NULL DEFAULT '{}'
);
"""


async def _bootstrap_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("db_schema_bootstrapped")


async def _create_default_admin(pool: asyncpg.Pool) -> None:
    """Create admin/admin123 if no users exist."""
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count == 0:
            uid = str(uuid.uuid4())
            hashed = hash_password("admin123")
            await conn.execute(
                """INSERT INTO users (id, username, email, hashed_password, role, is_active)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                uid, "admin", "admin@arvp.local", hashed, "ADMIN", True,
            )
            logger.info("default_admin_created", user_id=uid)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_pool, _redis

    # Connect to PostgreSQL
    _db_pool = await asyncpg.create_pool(
        _PG_DSN,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    await _bootstrap_schema(_db_pool)
    await _create_default_admin(_db_pool)

    # Connect to Redis
    _redis = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    logger.info("auth_service_started")
    yield

    await _db_pool.close()
    await _redis.aclose()
    logger.info("auth_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ARVP Auth Service",
    description="Authentication, authorization, and API key management",
    version="1.0.0",
    lifespan=lifespan,
)

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

async def _get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    x_user_id: Optional[str] = Header(default=None, alias="x-user-id"),
    x_user_role: Optional[str] = Header(default=None, alias="x-user-role"),
) -> Dict[str, Any]:
    """
    Validates the Bearer JWT and returns the user payload dict.
    Also accepts x-user-id/x-user-role injected by the API Gateway.
    """
    # If gateway injected headers, trust them
    if x_user_id and x_user_role:
        return {"sub": x_user_id, "role": x_user_role}

    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    return payload


async def _require_admin(user: Dict = Depends(_get_current_user)) -> Dict:
    if user.get("role") != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


# ---------------------------------------------------------------------------
# Audit logging helper
# ---------------------------------------------------------------------------

async def _audit(
    pool: asyncpg.Pool,
    user_id: Optional[str],
    action: str,
    resource: str,
    resource_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    details: Optional[Dict] = None,
) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO audit_log
                   (id, user_id, action, resource, resource_id, ip_address, timestamp, details)
                   VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7)""",
                str(uuid.uuid4()),
                user_id,
                action,
                resource,
                resource_id,
                ip_address,
                json.dumps(details or {}),
            )
    except Exception as exc:
        logger.error("audit_log_error", error=str(exc))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------

@app.post("/auth/register", response_model=UserResponse, status_code=201, tags=["Auth"])
async def register(
    request: Request,
    body: UserCreate,
    current_user: Optional[Dict] = Depends(_get_current_user),
) -> UserResponse:
    """
    Register a new user.
    - ADMIN/OPERATOR roles can only be assigned by an existing ADMIN.
    - VIEWER role is open to anyone.
    """
    db = await get_db()

    # Role restriction
    if body.role in (UserRole.ADMIN, UserRole.OPERATOR):
        if not current_user or current_user.get("role") != UserRole.ADMIN:
            raise HTTPException(
                status_code=403,
                detail="Only ADMIN users can create ADMIN or OPERATOR accounts",
            )

    try:
        validate_password_strength(body.password)
    except PasswordValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    hashed = hash_password(body.password)
    uid = str(uuid.uuid4())

    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO users (id, username, email, hashed_password, role, is_active, created_at)
                   VALUES ($1, $2, $3, $4, $5, TRUE, NOW())
                   RETURNING id, username, email, role, is_active, created_at, last_login""",
                uid, body.username, body.email, hashed, body.role.value,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail="Username or email already exists",
        ) from exc

    await _audit(
        db,
        current_user.get("sub") if current_user else None,
        "USER_REGISTER",
        "users",
        uid,
        _client_ip(request),
        {"username": body.username, "role": body.role.value},
    )

    return UserResponse(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        role=UserRole(row["role"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
        last_login=row["last_login"],
    )


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

@app.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(request: Request, body: UserLogin) -> TokenResponse:
    db = await get_db()
    redis = await get_redis()

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE username = $1", body.username
        )

    if row is None or not row["is_active"]:
        AUTH_ATTEMPTS.labels(result="failure").inc()
        await _audit(db, None, "LOGIN_FAILED", "auth", body.username, _client_ip(request))
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(body.password, row["hashed_password"]):
        AUTH_ATTEMPTS.labels(result="failure").inc()
        await _audit(
            db, str(row["id"]), "LOGIN_FAILED", "auth", str(row["id"]), _client_ip(request)
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Update last_login
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_login = NOW() WHERE id = $1", row["id"]
        )

    user_id = str(row["id"])
    payload = {"sub": user_id, "username": row["username"], "role": row["role"]}
    access_token = create_access_token(payload)
    refresh_token = create_refresh_token(user_id)

    # Store refresh token in Redis: key = refresh:{user_id}:{token_hash}
    import hashlib
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()[:16]
    redis_key = f"refresh:{user_id}:{token_hash}"
    await redis.setex(redis_key, 7 * 24 * 3600, refresh_token)

    ACTIVE_SESSIONS.inc()
    AUTH_ATTEMPTS.labels(result="success").inc()
    await _audit(
        db, user_id, "LOGIN_SUCCESS", "auth", user_id, _client_ip(request)
    )

    user_resp = UserResponse(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        role=UserRole(row["role"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
        last_login=row["last_login"],
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=JWT_EXPIRE_MINUTES * 60,
        user=user_resp,
    )


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------

@app.post("/auth/refresh", response_model=TokenResponse, tags=["Auth"])
async def refresh_token(request: Request, body: RefreshRequest) -> TokenResponse:
    db = await get_db()
    redis = await get_redis()

    try:
        user_id = decode_refresh_token(body.refresh_token)
    except (JWTError, ValueError) as exc:
        raise HTTPException(status_code=401, detail=f"Invalid refresh token: {exc}") from exc

    # Check blacklist
    if await redis.get(f"blacklist:refresh:{body.refresh_token}"):
        raise HTTPException(status_code=401, detail="Refresh token has been revoked")

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1 AND is_active = TRUE", user_id
        )

    if row is None:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    payload = {"sub": user_id, "username": row["username"], "role": row["role"]}
    new_access = create_access_token(payload)
    new_refresh = create_refresh_token(user_id)

    # Blacklist old refresh token
    await redis.setex(
        f"blacklist:refresh:{body.refresh_token}",
        7 * 24 * 3600,
        "1",
    )

    # Store new refresh token
    import hashlib
    token_hash = hashlib.sha256(new_refresh.encode()).hexdigest()[:16]
    await redis.setex(f"refresh:{user_id}:{token_hash}", 7 * 24 * 3600, new_refresh)

    user_resp = UserResponse(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        role=UserRole(row["role"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
        last_login=row["last_login"],
    )
    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        token_type="bearer",
        expires_in=JWT_EXPIRE_MINUTES * 60,
        user=user_resp,
    )


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------

@app.post("/auth/logout", status_code=204, tags=["Auth"])
async def logout(
    request: Request,
    body: RefreshRequest,
    current_user: Dict = Depends(_get_current_user),
) -> Response:
    redis = await get_redis()
    db = await get_db()

    # Blacklist refresh token
    await redis.setex(
        f"blacklist:refresh:{body.refresh_token}",
        7 * 24 * 3600,
        "1",
    )
    ACTIVE_SESSIONS.dec()
    await _audit(
        db,
        current_user.get("sub"),
        "LOGOUT",
        "auth",
        current_user.get("sub"),
        _client_ip(request),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

@app.get("/auth/me", response_model=UserResponse, tags=["Auth"])
async def get_me(current_user: Dict = Depends(_get_current_user)) -> UserResponse:
    db = await get_db()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1", current_user["sub"]
        )
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        role=UserRole(row["role"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
        last_login=row["last_login"],
    )


# ---------------------------------------------------------------------------
# PUT /auth/me/password
# ---------------------------------------------------------------------------

@app.put("/auth/me/password", status_code=204, tags=["Auth"])
async def change_password(
    request: Request,
    body: PasswordChange,
    current_user: Dict = Depends(_get_current_user),
) -> Response:
    db = await get_db()
    user_id = current_user["sub"]

    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

    if row is None or not verify_password(body.current_password, row["hashed_password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    try:
        validate_password_strength(body.new_password)
    except PasswordValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_hash = hash_password(body.new_password)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hashed_password = $1 WHERE id = $2", new_hash, user_id
        )

    await _audit(db, user_id, "PASSWORD_CHANGE", "users", user_id, _client_ip(request))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /auth/api-keys
# ---------------------------------------------------------------------------

@app.post("/auth/api-keys", response_model=APIKeyResponse, status_code=201, tags=["API Keys"])
async def create_api_key(
    request: Request,
    body: APIKeyCreate,
    current_user: Dict = Depends(_get_current_user),
) -> APIKeyResponse:
    db = await get_db()
    user_id = current_user["sub"]

    raw_key, key_hash = generate_api_key()
    key_id = str(uuid.uuid4())
    expires_at = None
    if body.expires_days is not None:
        expires_at = datetime.now(tz=timezone.utc) + timedelta(days=body.expires_days)

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO api_keys (id, user_id, key_hash, name, permissions, created_at, expires_at)
               VALUES ($1, $2, $3, $4, $5, NOW(), $6)
               RETURNING id, name, permissions, created_at, expires_at, last_used""",
            key_id, user_id, key_hash, body.name, json.dumps(body.permissions), expires_at,
        )

    await _audit(
        db, user_id, "API_KEY_CREATE", "api_keys", key_id, _client_ip(request),
        {"name": body.name},
    )

    return APIKeyResponse(
        id=row["id"],
        name=row["name"],
        permissions=json.loads(row["permissions"]) if isinstance(row["permissions"], str) else row["permissions"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_used=row["last_used"],
        raw_key=raw_key,  # Only returned once
    )


# ---------------------------------------------------------------------------
# GET /auth/api-keys
# ---------------------------------------------------------------------------

@app.get("/auth/api-keys", response_model=APIKeyListResponse, tags=["API Keys"])
async def list_api_keys(
    current_user: Dict = Depends(_get_current_user),
) -> APIKeyListResponse:
    db = await get_db()
    user_id = current_user["sub"]

    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, permissions, created_at, expires_at, last_used "
            "FROM api_keys WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )

    keys = [
        APIKeyResponse(
            id=r["id"],
            name=r["name"],
            permissions=json.loads(r["permissions"]) if isinstance(r["permissions"], str) else r["permissions"],
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            last_used=r["last_used"],
        )
        for r in rows
    ]
    return APIKeyListResponse(api_keys=keys, total=len(keys))


# ---------------------------------------------------------------------------
# DELETE /auth/api-keys/{key_id}
# ---------------------------------------------------------------------------

@app.delete("/auth/api-keys/{key_id}", status_code=204, tags=["API Keys"])
async def revoke_api_key(
    request: Request,
    key_id: str,
    current_user: Dict = Depends(_get_current_user),
) -> Response:
    db = await get_db()
    user_id = current_user["sub"]

    async with db.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM api_keys WHERE id = $1 AND user_id = $2",
            key_id, user_id,
        )

    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="API key not found")

    await _audit(db, user_id, "API_KEY_REVOKE", "api_keys", key_id, _client_ip(request))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GET /auth/users (ADMIN)
# ---------------------------------------------------------------------------

@app.get("/auth/users", response_model=UserListResponse, tags=["User Management"])
async def list_users(
    _admin: Dict = Depends(_require_admin),
    offset: int = 0,
    limit: int = 50,
) -> UserListResponse:
    db = await get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, username, email, role, is_active, created_at, last_login "
            "FROM users ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM users")

    users = [
        UserResponse(
            id=r["id"],
            username=r["username"],
            email=r["email"],
            role=UserRole(r["role"]),
            is_active=r["is_active"],
            created_at=r["created_at"],
            last_login=r["last_login"],
        )
        for r in rows
    ]
    return UserListResponse(users=users, total=total)


# ---------------------------------------------------------------------------
# PUT /auth/users/{user_id}/role (ADMIN)
# ---------------------------------------------------------------------------

@app.put("/auth/users/{user_id}/role", response_model=UserResponse, tags=["User Management"])
async def update_user_role(
    request: Request,
    user_id: str,
    body: RoleUpdate,
    admin: Dict = Depends(_require_admin),
) -> UserResponse:
    db = await get_db()

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE users SET role = $1 WHERE id = $2
               RETURNING id, username, email, role, is_active, created_at, last_login""",
            body.role.value, user_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    await _audit(
        db, admin["sub"], "USER_ROLE_UPDATE", "users", user_id, _client_ip(request),
        {"new_role": body.role.value},
    )
    return UserResponse(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        role=UserRole(row["role"]),
        is_active=row["is_active"],
        created_at=row["created_at"],
        last_login=row["last_login"],
    )


# ---------------------------------------------------------------------------
# DELETE /auth/users/{user_id} (ADMIN — soft delete / deactivate)
# ---------------------------------------------------------------------------

@app.delete("/auth/users/{user_id}", status_code=204, tags=["User Management"])
async def deactivate_user(
    request: Request,
    user_id: str,
    admin: Dict = Depends(_require_admin),
) -> Response:
    db = await get_db()

    # Prevent self-deactivation
    if user_id == admin["sub"]:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    async with db.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET is_active = FALSE WHERE id = $1", user_id
        )

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="User not found")

    await _audit(
        db, admin["sub"], "USER_DEACTIVATE", "users", user_id, _client_ip(request)
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Health & Metrics
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    db_ok = False
    redis_ok = False
    try:
        pool = await get_db()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass

    overall = "healthy" if (db_ok and redis_ok) else "degraded"
    return JSONResponse(
        {"status": overall, "service": "auth-service", "db": db_ok, "redis": redis_ok},
        status_code=200 if overall == "healthy" else 503,
    )


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8006, reload=False)
