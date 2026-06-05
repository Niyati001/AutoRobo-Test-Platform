"""
Telemetry Service — ingests robot telemetry from Redis streams, persists to PostgreSQL,
serves REST queries and live WebSocket feeds.

Port: 8001
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("telemetry-service")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://arvp:arvp_pass@postgres:5432/arvp_db"
)
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8001"))

TELEMETRY_STREAM_KEY = "telemetry:stream"
ROBOT_SORTED_SET_PREFIX = "telemetry:robot:"
ROBOT_SORTED_SET_TTL = 3600  # keep 1 hour of data in sorted set

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

SAMPLES_INGESTED = Counter("telemetry_samples_ingested_total", "Total telemetry samples ingested", ["robot_id"])
SAMPLES_PERSISTED = Counter("telemetry_samples_persisted_total", "Total samples persisted to DB")
ACTIVE_WS_CONNECTIONS = Gauge("telemetry_active_ws_connections", "Active WebSocket connections")
INGESTION_LAG = Histogram(
    "telemetry_ingestion_lag_seconds",
    "Lag between telemetry timestamp and ingestion time",
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_redis: Optional[aioredis.Redis] = None
_ws_subscribers: Dict[str, Set[WebSocket]] = {}  # robot_id -> set of WebSocket
_latest_telemetry: Dict[str, Dict[str, Any]] = {}  # robot_id -> latest sample
_ingestion_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Redis stream consumer
# ---------------------------------------------------------------------------

async def _consume_telemetry_stream() -> None:
    """Continuously reads the telemetry Redis stream and fans out to DB + WebSocket subscribers."""
    if _redis is None:
        return
    last_id = "0"  # start from beginning; in production use '$' for latest only
    log.info("telemetry_consumer_started", stream=TELEMETRY_STREAM_KEY)

    while True:
        try:
            # XREAD with block=100ms so we don't burn CPU when idle
            messages = await _redis.xread(
                {TELEMETRY_STREAM_KEY: last_id}, count=100, block=100
            )
            if not messages:
                await asyncio.sleep(0.01)
                continue

            for _stream, entries in messages:
                for msg_id, fields in entries:
                    last_id = msg_id
                    payload_str = fields.get("payload") or fields.get("data")
                    if not payload_str:
                        continue
                    try:
                        sample: Dict[str, Any] = json.loads(payload_str)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    robot_id = sample.get("robot_id", "unknown")
                    ts = float(sample.get("timestamp", time.time()))
                    lag = time.time() - ts
                    INGESTION_LAG.observe(lag)
                    SAMPLES_INGESTED.labels(robot_id=robot_id).inc()

                    # Cache latest
                    _latest_telemetry[robot_id] = sample

                    # Persist to Redis sorted set for range queries
                    set_key = f"{ROBOT_SORTED_SET_PREFIX}{robot_id}"
                    await _redis.zadd(set_key, {payload_str: ts})
                    # Trim to keep last ROBOT_SORTED_SET_TTL seconds of data
                    cutoff = time.time() - ROBOT_SORTED_SET_TTL
                    await _redis.zremrangebyscore(set_key, "-inf", cutoff)

                    # Fan out to WebSocket subscribers for this robot
                    subs = _ws_subscribers.get(robot_id, set())
                    if subs:
                        dead = set()
                        for ws in subs.copy():
                            try:
                                await ws.send_text(payload_str)
                            except Exception:
                                dead.add(ws)
                        _ws_subscribers[robot_id] -= dead

                    # Also fan out to "all" subscribers
                    subs_all = _ws_subscribers.get("*", set())
                    if subs_all:
                        dead_all = set()
                        for ws in subs_all.copy():
                            try:
                                await ws.send_text(payload_str)
                            except Exception:
                                dead_all.add(ws)
                        _ws_subscribers["*"] -= dead_all

        except aioredis.RedisError as exc:
            log.warning("redis_stream_error", error=str(exc))
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("consumer_unexpected_error", error=str(exc))
            await asyncio.sleep(0.5)

    log.info("telemetry_consumer_stopped")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis, _ingestion_task
    _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("telemetry_service_started", port=SERVICE_PORT)

    _ingestion_task = asyncio.create_task(_consume_telemetry_stream())
    yield

    if _ingestion_task:
        _ingestion_task.cancel()
        try:
            await _ingestion_task
        except asyncio.CancelledError:
            pass
    if _redis:
        await _redis.aclose()
    await engine.dispose()
    log.info("telemetry_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Telemetry Service",
    version="1.0.0",
    description="Real-time telemetry ingestion, storage, and streaming",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

security = HTTPBearer(auto_error=False)
SKIP_AUTH = {"/health", "/metrics", "/docs", "/openapi.json", "/redoc"}


async def verify_jwt(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if request.url.path in SKIP_AUTH:
        return {}
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        return jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict:
    redis_ok = False
    if _redis:
        try:
            await _redis.ping()
            redis_ok = True
        except Exception:
            pass
    stream_len = 0
    if redis_ok and _redis:
        try:
            stream_len = await _redis.xlen(TELEMETRY_STREAM_KEY)
        except Exception:
            pass
    return {
        "status": "ok" if redis_ok else "degraded",
        "service": "telemetry-service",
        "redis": "ok" if redis_ok else "error",
        "stream_length": stream_len,
        "known_robots": len(_latest_telemetry),
        "active_ws_connections": sum(len(s) for s in _ws_subscribers.values()),
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/telemetry/{robot_id}/latest", dependencies=[Depends(verify_jwt)])
async def get_latest(robot_id: str) -> dict:
    """Get the most recent telemetry sample for a robot."""
    sample = _latest_telemetry.get(robot_id)
    if sample is None:
        raise HTTPException(status_code=404, detail=f"No telemetry found for robot '{robot_id}'")
    return sample


@app.get("/telemetry/{robot_id}/history", dependencies=[Depends(verify_jwt)])
async def get_history(
    robot_id: str,
    window_seconds: float = Query(default=60.0, ge=1.0, le=3600.0),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict:
    """Get telemetry history for a robot over a time window."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    now = time.time()
    cutoff = now - window_seconds
    key = f"{ROBOT_SORTED_SET_PREFIX}{robot_id}"
    raw = await _redis.zrangebyscore(key, cutoff, now, start=0, num=limit)
    samples = []
    for item in raw:
        try:
            samples.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "robot_id": robot_id,
        "window_seconds": window_seconds,
        "sample_count": len(samples),
        "samples": samples,
    }


@app.get("/telemetry/fleet/summary", dependencies=[Depends(verify_jwt)])
async def fleet_summary() -> dict:
    """Return latest telemetry summary for all known robots."""
    robots = []
    for robot_id, sample in _latest_telemetry.items():
        robots.append({
            "robot_id": robot_id,
            "timestamp": sample.get("timestamp"),
            "state": sample.get("navigation", {}).get("state"),
            "battery": sample.get("battery", {}).get("level"),
            "position": sample.get("position"),
            "velocity_linear": sample.get("velocity", {}).get("linear"),
        })
    return {
        "total_robots": len(robots),
        "timestamp": time.time(),
        "robots": robots,
    }


@app.post("/telemetry/ingest", dependencies=[Depends(verify_jwt)], status_code=status.HTTP_202_ACCEPTED)
async def ingest_telemetry(payload: Dict[str, Any]) -> dict:
    """Manually push a telemetry sample (primarily for testing)."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    robot_id = payload.get("robot_id", "unknown")
    payload.setdefault("timestamp", time.time())
    data = json.dumps(payload)
    await _redis.xadd(TELEMETRY_STREAM_KEY, {"payload": data})
    return {"status": "accepted", "robot_id": robot_id}


# ---------------------------------------------------------------------------
# WebSocket: live telemetry stream per robot
# ---------------------------------------------------------------------------

@app.websocket("/ws/telemetry/{robot_id}")
async def ws_telemetry(websocket: WebSocket, robot_id: str) -> None:
    """Live WebSocket feed of telemetry for a specific robot (or '*' for all)."""
    await websocket.accept()
    ACTIVE_WS_CONNECTIONS.inc()
    if robot_id not in _ws_subscribers:
        _ws_subscribers[robot_id] = set()
    _ws_subscribers[robot_id].add(websocket)
    log.info("ws_connected", robot_id=robot_id)

    # Send current latest as first message if available
    latest = _latest_telemetry.get(robot_id)
    if latest:
        try:
            await websocket.send_text(json.dumps(latest))
        except Exception:
            pass

    try:
        while True:
            # Keep connection alive; messages are pushed by ingestion loop
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws_error", robot_id=robot_id, error=str(exc))
    finally:
        _ws_subscribers.get(robot_id, set()).discard(websocket)
        ACTIVE_WS_CONNECTIONS.dec()
        log.info("ws_disconnected", robot_id=robot_id)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
