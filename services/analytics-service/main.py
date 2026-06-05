"""
Analytics Service — KPI aggregation, fleet performance trends, mission statistics.

Port: 8007
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import numpy as np
import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
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
log = structlog.get_logger("analytics-service")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://arvp:arvp_pass@postgres:5432/arvp_db"
)
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8007"))
ROBOT_SORTED_SET_PREFIX = "telemetry:robot:"

# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

ANALYTICS_QUERIES = Counter("analytics_queries_total", "Analytics queries executed", ["report_type"])

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FleetKPIs(BaseModel):
    timestamp: float
    window_seconds: float
    total_robots_observed: int
    avg_battery_level: float
    min_battery_level: float
    avg_velocity: float
    max_velocity: float
    avg_cpu_usage: float
    avg_cpu_temp: float
    avg_network_latency_ms: float
    avg_lidar_quality: float
    mission_completion_estimate: float
    uptime_fraction: float


class ValidationTrend(BaseModel):
    period: str
    total_runs: int
    pass_rate_avg: float
    pass_rate_trend: str  # improving / degrading / stable
    top_failing_tests: List[str]


class FaultAnalytics(BaseModel):
    period: str
    total_faults: int
    fault_type_distribution: Dict[str, int]
    mttr_seconds: float
    most_common_fault: str


class PlatformSummary(BaseModel):
    generated_at: float
    simulation_runs_total: int
    validation_runs_total: int
    fault_events_total: int
    fleet_kpis: Optional[FleetKPIs]
    validation_trend: Optional[ValidationTrend]
    fault_analytics: Optional[FaultAnalytics]


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

_redis: Optional[aioredis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    try:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        log.warning("redis_connect_failed", error=str(exc))
    log.info("analytics_service_started", port=SERVICE_PORT)
    yield
    if _redis:
        await _redis.aclose()
    await engine.dispose()
    log.info("analytics_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Analytics Service",
    version="1.0.0",
    description="KPI aggregation and performance analytics for the ARVP platform",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_robot_telemetry(robot_id: str, window_seconds: float) -> List[Dict[str, Any]]:
    if _redis is None:
        return []
    now = time.time()
    cutoff = now - window_seconds
    key = f"{ROBOT_SORTED_SET_PREFIX}{robot_id}"
    raw = await _redis.zrangebyscore(key, cutoff, now)
    samples = []
    for item in raw:
        try:
            samples.append(json.loads(item))
        except Exception:
            pass
    return samples


async def _get_all_robot_ids() -> List[str]:
    if _redis is None:
        return []
    keys = await _redis.keys(f"{ROBOT_SORTED_SET_PREFIX}*")
    return [k.replace(ROBOT_SORTED_SET_PREFIX, "") for k in keys]


def _compute_kpis(all_samples: List[Dict[str, Any]], window_seconds: float) -> FleetKPIs:
    if not all_samples:
        return FleetKPIs(
            timestamp=time.time(),
            window_seconds=window_seconds,
            total_robots_observed=0,
            avg_battery_level=0.0,
            min_battery_level=0.0,
            avg_velocity=0.0,
            max_velocity=0.0,
            avg_cpu_usage=0.0,
            avg_cpu_temp=0.0,
            avg_network_latency_ms=0.0,
            avg_lidar_quality=0.0,
            mission_completion_estimate=0.0,
            uptime_fraction=0.0,
        )

    batteries = [float(s.get("battery", {}).get("level", 0.8)) for s in all_samples]
    vels = [float(s.get("velocity", {}).get("linear", 0.0)) for s in all_samples]
    cpus = [float(s.get("diagnostics", {}).get("cpu_usage", 0.0)) for s in all_samples]
    temps = [float(s.get("diagnostics", {}).get("cpu_temp", 40.0)) for s in all_samples]
    latencies = [float(s.get("diagnostics", {}).get("network_latency_ms", 10.0)) for s in all_samples]
    lidars = [float(s.get("sensors", {}).get("lidar_quality", 1.0)) for s in all_samples]
    nav_states = [s.get("navigation", {}).get("state", "") for s in all_samples]

    moving_fraction = sum(1 for ns in nav_states if ns == "MOVING") / max(len(nav_states), 1)
    mission_complete = sum(1 for ns in nav_states if ns == "IDLE") / max(len(nav_states), 1)

    robot_ids = set(s.get("robot_id") for s in all_samples if s.get("robot_id"))

    return FleetKPIs(
        timestamp=time.time(),
        window_seconds=window_seconds,
        total_robots_observed=len(robot_ids),
        avg_battery_level=round(float(np.mean(batteries)), 4),
        min_battery_level=round(float(np.min(batteries)), 4),
        avg_velocity=round(float(np.mean(vels)), 4),
        max_velocity=round(float(np.max(vels)), 4),
        avg_cpu_usage=round(float(np.mean(cpus)), 4),
        avg_cpu_temp=round(float(np.mean(temps)), 2),
        avg_network_latency_ms=round(float(np.mean(latencies)), 2),
        avg_lidar_quality=round(float(np.mean(lidars)), 4),
        mission_completion_estimate=round(mission_complete, 4),
        uptime_fraction=round(moving_fraction + mission_complete, 4),
    )


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
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if (redis_ok or db_ok) else "degraded",
        "service": "analytics-service",
        "redis": "ok" if redis_ok else "error",
        "database": "ok" if db_ok else "error",
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/analytics/fleet/kpis", dependencies=[Depends(verify_jwt)])
async def fleet_kpis(
    window_seconds: float = Query(default=300.0, ge=10.0, le=86400.0),
) -> FleetKPIs:
    ANALYTICS_QUERIES.labels(report_type="fleet_kpis").inc()
    robot_ids = await _get_all_robot_ids()
    all_samples: List[Dict[str, Any]] = []
    for rid in robot_ids:
        samples = await _fetch_robot_telemetry(rid, window_seconds)
        all_samples.extend(samples)
    return _compute_kpis(all_samples, window_seconds)


@app.get("/analytics/fleet/robot/{robot_id}/kpis", dependencies=[Depends(verify_jwt)])
async def robot_kpis(
    robot_id: str,
    window_seconds: float = Query(default=300.0, ge=10.0, le=86400.0),
) -> FleetKPIs:
    ANALYTICS_QUERIES.labels(report_type="robot_kpis").inc()
    samples = await _fetch_robot_telemetry(robot_id, window_seconds)
    if not samples:
        raise HTTPException(status_code=404, detail=f"No telemetry for robot '{robot_id}'")
    return _compute_kpis(samples, window_seconds)


@app.get("/analytics/validations/trend", dependencies=[Depends(verify_jwt)])
async def validation_trend(
    period: str = Query(default="24h", pattern=r"^\d+[hd]$"),
) -> ValidationTrend:
    ANALYTICS_QUERIES.labels(report_type="validation_trend").inc()
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                text(
                    "SELECT status, pass_rate, results FROM validation_runs "
                    "ORDER BY started_at DESC LIMIT 100"
                )
            )
            data = rows.mappings().fetchall()
    except Exception:
        data = []

    if not data:
        return ValidationTrend(
            period=period,
            total_runs=0,
            pass_rate_avg=0.0,
            pass_rate_trend="stable",
            top_failing_tests=[],
        )

    pass_rates = [float(row.get("pass_rate", 0) or 0) for row in data]
    avg_pass_rate = float(np.mean(pass_rates)) if pass_rates else 0.0

    failing_tests: Dict[str, int] = {}
    for row in data:
        results_raw = row.get("results")
        if results_raw:
            results = json.loads(results_raw) if isinstance(results_raw, str) else results_raw
            for r in (results or []):
                if not r.get("passed", True):
                    name = r.get("test_name", "unknown")
                    failing_tests[name] = failing_tests.get(name, 0) + 1

    top_failing = sorted(failing_tests, key=lambda k: failing_tests[k], reverse=True)[:5]

    # Trend: compare first half vs second half
    if len(pass_rates) >= 4:
        mid = len(pass_rates) // 2
        early_avg = float(np.mean(pass_rates[mid:]))
        recent_avg = float(np.mean(pass_rates[:mid]))
        delta = recent_avg - early_avg
        trend = "improving" if delta > 0.05 else "degrading" if delta < -0.05 else "stable"
    else:
        trend = "stable"

    return ValidationTrend(
        period=period,
        total_runs=len(data),
        pass_rate_avg=round(avg_pass_rate, 4),
        pass_rate_trend=trend,
        top_failing_tests=top_failing,
    )


@app.get("/analytics/faults/summary", dependencies=[Depends(verify_jwt)])
async def fault_summary(
    period: str = Query(default="24h", pattern=r"^\d+[hd]$"),
) -> FaultAnalytics:
    ANALYTICS_QUERIES.labels(report_type="fault_summary").inc()
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                text(
                    "SELECT fault_type, severity, injected_at, resolved_at FROM fault_events "
                    "ORDER BY injected_at DESC LIMIT 500"
                )
            )
            data = rows.mappings().fetchall()
    except Exception:
        data = []

    if not data:
        return FaultAnalytics(
            period=period,
            total_faults=0,
            fault_type_distribution={},
            mttr_seconds=0.0,
            most_common_fault="none",
        )

    type_dist: Dict[str, int] = {}
    recovery_times = []
    for row in data:
        ft = row.get("fault_type", "UNKNOWN")
        type_dist[ft] = type_dist.get(ft, 0) + 1
        injected = row.get("injected_at")
        resolved = row.get("resolved_at")
        if injected and resolved:
            try:
                recovery_times.append(float(resolved) - float(injected))
            except (TypeError, ValueError):
                pass

    most_common = max(type_dist, key=lambda k: type_dist[k]) if type_dist else "none"
    mttr = float(np.mean(recovery_times)) if recovery_times else 0.0

    return FaultAnalytics(
        period=period,
        total_faults=len(data),
        fault_type_distribution=type_dist,
        mttr_seconds=round(mttr, 2),
        most_common_fault=most_common,
    )


@app.get("/analytics/platform/summary", dependencies=[Depends(verify_jwt)])
async def platform_summary() -> PlatformSummary:
    ANALYTICS_QUERIES.labels(report_type="platform_summary").inc()

    sim_total = val_total = fault_total = 0
    try:
        async with AsyncSessionLocal() as session:
            r1 = await session.execute(text("SELECT COUNT(*) FROM simulation_runs"))
            sim_total = r1.scalar() or 0
            r2 = await session.execute(text("SELECT COUNT(*) FROM validation_runs"))
            val_total = r2.scalar() or 0
            r3 = await session.execute(text("SELECT COUNT(*) FROM fault_events"))
            fault_total = r3.scalar() or 0
    except Exception:
        pass

    kpis = await fleet_kpis(window_seconds=300.0)
    trend = await validation_trend(period="24h")
    faults = await fault_summary(period="24h")

    return PlatformSummary(
        generated_at=time.time(),
        simulation_runs_total=sim_total,
        validation_runs_total=val_total,
        fault_events_total=fault_total,
        fleet_kpis=kpis,
        validation_trend=trend,
        fault_analytics=faults,
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
