"""
Validation Service — FastAPI microservice for running statistical validation test suites.

Port: 8003
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from models import (
    BaselineSetRequest,
    ComparisonResult,
    ValidationBaseline,
    ValidationConfig,
    ValidationListResponse,
    ValidationReport,
    ValidationRun,
    ValidationRunStatus,
    ValidationTestType,
)
from test_runner import ValidationTestRunner

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
log = structlog.get_logger("validation-service")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://arvp:arvp_pass@postgres:5432/arvp_db"
)
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8003"))

TELEMETRY_STREAM_KEY = "telemetry:stream"
TELEMETRY_ROBOT_KEY = "telemetry:robot:{robot_id}"

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

VALIDATION_RUNS_STARTED = Counter("validation_runs_started_total", "Validation runs started")
VALIDATION_RUNS_COMPLETED = Counter(
    "validation_runs_completed_total", "Validation runs completed", ["result"]
)
ACTIVE_RUNS = Gauge("validation_active_runs", "Currently running validations")
TESTS_EXECUTED = Counter(
    "validation_tests_executed_total", "Individual tests executed", ["test_name", "passed"]
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
_active_runs: Dict[str, ValidationRun] = {}
_baselines: Dict[str, ValidationBaseline] = {}
_runner = ValidationTestRunner()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("validation_service_started", port=SERVICE_PORT)
    yield
    if _redis:
        await _redis.aclose()
    await engine.dispose()
    log.info("validation_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Validation Service",
    version="1.0.0",
    description="Statistical validation test suites for autonomous robot fleets",
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
# Telemetry helpers
# ---------------------------------------------------------------------------

async def _fetch_telemetry(robot_id: str, window_seconds: float) -> List[Dict[str, Any]]:
    """Fetch recent telemetry for a robot from Redis."""
    if _redis is None:
        return []
    try:
        # Read from sorted set (score = timestamp)
        now = time.time()
        cutoff = now - window_seconds
        key = TELEMETRY_ROBOT_KEY.format(robot_id=robot_id)
        raw = await _redis.zrangebyscore(key, cutoff, now)
        samples = []
        for item in raw:
            try:
                samples.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                pass
        return samples
    except Exception as exc:
        log.warning("telemetry_fetch_failed", robot_id=robot_id, error=str(exc))
        return []


async def _fetch_fleet_telemetry(
    fleet_ids: List[str], window_seconds: float
) -> Dict[str, List[Dict[str, Any]]]:
    result = {}
    for rid in fleet_ids:
        result[rid] = await _fetch_telemetry(rid, window_seconds)
    return result


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _persist_run(run: ValidationRun) -> None:
    try:
        async with AsyncSessionLocal() as session:
            exists = await session.execute(
                text("SELECT id FROM validation_runs WHERE run_id = :run_id"),
                {"run_id": run.run_id},
            )
            row = exists.fetchone()
            results_json = json.dumps([r.model_dump(mode="json") for r in run.results])
            if row:
                await session.execute(
                    text(
                        "UPDATE validation_runs SET status=:status, pass_rate=:pass_rate, "
                        "overall_passed=:overall_passed, results=:results::jsonb, "
                        "ended_at=TO_TIMESTAMP(:ended_at) WHERE run_id=:run_id"
                    ),
                    {
                        "run_id": run.run_id,
                        "status": run.status.value,
                        "pass_rate": run.pass_rate,
                        "overall_passed": run.overall_passed,
                        "results": results_json,
                        "ended_at": run.ended_at.timestamp() if run.ended_at else None,
                    },
                )
            else:
                await session.execute(
                    text(
                        "INSERT INTO validation_runs "
                        "(id, run_id, robot_id, fleet_ids, test_suite, config, status, results, "
                        "pass_rate, overall_passed, started_at) VALUES "
                        "(:id, :run_id, :robot_id, :fleet_ids::jsonb, :test_suite::jsonb, "
                        ":config::jsonb, :status, :results::jsonb, :pass_rate, :overall_passed, "
                        "TO_TIMESTAMP(:started_at))"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "run_id": run.run_id,
                        "robot_id": run.robot_id,
                        "fleet_ids": json.dumps(run.fleet_ids),
                        "test_suite": json.dumps([t.value for t in run.test_suite]),
                        "config": run.config.model_dump_json(),
                        "status": run.status.value,
                        "results": results_json,
                        "pass_rate": run.pass_rate,
                        "overall_passed": run.overall_passed,
                        "started_at": run.started_at.timestamp(),
                    },
                )
            await session.commit()
    except Exception as exc:
        log.error("db_persist_failed", run_id=run.run_id, error=str(exc))


# ---------------------------------------------------------------------------
# Core validation executor
# ---------------------------------------------------------------------------

async def _execute_validation(run: ValidationRun) -> None:
    """Run all tests and populate run.results in place."""
    cfg = run.config
    window = cfg.telemetry_window_seconds

    # Gather telemetry
    if cfg.robot_id:
        single_telemetry = await _fetch_telemetry(cfg.robot_id, window)
        fleet_telemetry: Dict[str, List] = {cfg.robot_id: single_telemetry}
    else:
        fleet_telemetry = await _fetch_fleet_telemetry(cfg.fleet_ids, window)

    # Synthetic telemetry fallback for POC when Redis is empty
    for rid, samples in fleet_telemetry.items():
        if not samples:
            fleet_telemetry[rid] = _generate_synthetic_telemetry(rid, int(window * 10))

    primary_telemetry = (
        fleet_telemetry.get(cfg.robot_id or cfg.fleet_ids[0], [])
        if fleet_telemetry else []
    )

    for test_type in run.test_suite:
        result = None
        try:
            if test_type == ValidationTestType.NAVIGATION_STABILITY:
                result = _runner.test_navigation_stability(primary_telemetry)

            elif test_type == ValidationTestType.COLLISION_PREVENTION:
                result = _runner.test_collision_prevention(primary_telemetry)

            elif test_type == ValidationTestType.TELEMETRY_INTEGRITY:
                result = _runner.test_telemetry_integrity(primary_telemetry)

            elif test_type == ValidationTestType.BATTERY_ENDURANCE:
                result = _runner.test_battery_endurance(primary_telemetry)

            elif test_type == ValidationTestType.FLEET_COORDINATION:
                result = _runner.test_fleet_coordination(fleet_telemetry)

            elif test_type == ValidationTestType.LOAD_STRESS:
                result = _runner.test_load_stress(primary_telemetry)

            elif test_type == ValidationTestType.RECOVERY_BEHAVIOR:
                fault_event: Dict[str, Any] = {
                    "severity": "MEDIUM",
                    "injected_at": time.time() - 60,
                }
                result = _runner.test_recovery_behavior(fault_event, primary_telemetry[-20:])

            elif test_type == ValidationTestType.REROUTING:
                obstacle_event: Dict[str, Any] = {
                    "injected_at": time.time() - 30,
                }
                result = _runner.test_rerouting_validation(
                    robot_id=cfg.robot_id or (cfg.fleet_ids[0] if cfg.fleet_ids else "unknown"),
                    obstacle_injection_event=obstacle_event,
                    telemetry_after_injection=primary_telemetry[-30:],
                    original_eta_seconds=60.0,
                )

            if result:
                result.passed = result.score >= cfg.pass_threshold
                run.results.append(result)
                TESTS_EXECUTED.labels(
                    test_name=test_type.value,
                    passed=str(result.passed),
                ).inc()

        except Exception as exc:
            log.error("test_execution_error", test=test_type.value, error=str(exc))

    run.compute_pass_rate()


def _generate_synthetic_telemetry(robot_id: str, count: int) -> List[Dict[str, Any]]:
    """Generate synthetic telemetry samples for POC when no live data is available."""
    import random
    import math
    samples = []
    now = time.time()
    x, y = 12.5, 12.5
    battery = 0.85
    for i in range(count):
        ts = now - (count - i) * 0.1
        x += random.gauss(0.05, 0.02)
        y += random.gauss(0.05, 0.02)
        battery -= random.uniform(0.0001, 0.0003)
        battery = max(battery, 0.1)
        samples.append({
            "robot_id": robot_id,
            "timestamp": ts,
            "position": {"x": x, "y": y, "theta": random.uniform(-math.pi, math.pi)},
            "velocity": {
                "linear": random.uniform(0.1, 0.8),
                "angular": random.gauss(0.0, 0.05),
            },
            "battery": {"level": battery, "voltage": 24.0 + battery * 4.0, "charging": False},
            "motors": {
                "left_rpm": random.uniform(40, 80),
                "right_rpm": random.uniform(40, 80),
                "left_current": random.uniform(0.5, 2.0),
                "right_current": random.uniform(0.5, 2.0),
            },
            "sensors": {
                "lidar_quality": random.uniform(0.7, 1.0),
                "imu_calibrated": True,
                "obstacle_detected": False,
                "distance_to_nearest_obstacle": random.uniform(1.0, 5.0),
            },
            "navigation": {
                "state": random.choice(["MOVING", "MOVING", "MOVING", "IDLE"]),
                "path_length": random.uniform(5.0, 20.0),
                "eta_seconds": random.uniform(10.0, 60.0),
            },
            "diagnostics": {
                "cpu_usage": random.uniform(0.1, 0.6),
                "memory_usage": random.uniform(0.2, 0.5),
                "cpu_temp": random.uniform(35.0, 65.0),
                "network_latency_ms": random.uniform(2.0, 30.0),
                "uptime_seconds": i * 0.1,
            },
        })
    return samples


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
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if (redis_ok and db_ok) else "degraded",
        "service": "validation-service",
        "redis": "ok" if redis_ok else "error",
        "database": "ok" if db_ok else "error",
        "active_runs": len(_active_runs),
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post(
    "/validations",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_jwt)],
)
async def start_validation(config: ValidationConfig) -> dict:
    """Start a validation run asynchronously."""
    import asyncio
    run = ValidationRun(
        run_id=str(uuid.uuid4()),
        robot_id=config.robot_id,
        fleet_ids=config.fleet_ids,
        test_suite=config.test_suite,
        config=config,
        status=ValidationRunStatus.RUNNING,
        baseline_run_id=config.baseline_run_id,
    )
    _active_runs[run.run_id] = run
    VALIDATION_RUNS_STARTED.inc()
    ACTIVE_RUNS.inc()
    await _persist_run(run)

    # Run validation in background task
    async def _bg_run() -> None:
        try:
            await _execute_validation(run)
            run.status = ValidationRunStatus.COMPLETED
            from datetime import datetime
            run.ended_at = datetime.utcnow()
            VALIDATION_RUNS_COMPLETED.labels(
                result="passed" if run.overall_passed else "failed"
            ).inc()
        except Exception as exc:
            log.error("validation_bg_error", run_id=run.run_id, error=str(exc))
            run.status = ValidationRunStatus.FAILED
        finally:
            ACTIVE_RUNS.dec()
            _active_runs.pop(run.run_id, None)
            _active_runs[run.run_id] = run
            await _persist_run(run)

    asyncio.create_task(_bg_run())
    log.info("validation_started", run_id=run.run_id)
    return {"run_id": run.run_id, "status": "RUNNING"}


@app.get("/validations/{run_id}", dependencies=[Depends(verify_jwt)])
async def get_validation(run_id: str) -> ValidationRun:
    run = _active_runs.get(run_id)
    if run:
        return run
    # Try DB
    try:
        async with AsyncSessionLocal() as session:
            row = await session.execute(
                text("SELECT * FROM validation_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            data = row.mappings().fetchone()
            if data:
                cfg_data = json.loads(data["config"]) if isinstance(data["config"], str) else data["config"]
                config = ValidationConfig(**cfg_data)
                results_raw = json.loads(data["results"]) if isinstance(data["results"], str) else data["results"] or []
                from models import TestResult
                results = [TestResult(**r) for r in results_raw]
                ts_raw = data.get("test_suite")
                ts = json.loads(ts_raw) if isinstance(ts_raw, str) else ts_raw or []
                return ValidationRun(
                    run_id=data["run_id"],
                    robot_id=data.get("robot_id"),
                    fleet_ids=json.loads(data["fleet_ids"]) if isinstance(data["fleet_ids"], str) else data.get("fleet_ids") or [],
                    test_suite=[ValidationTestType(t) for t in ts],
                    config=config,
                    status=ValidationRunStatus(data["status"]),
                    results=results,
                    pass_rate=float(data.get("pass_rate", 0)),
                    overall_passed=bool(data.get("overall_passed", False)),
                )
    except Exception as exc:
        log.error("db_fetch_failed", run_id=run_id, error=str(exc))
    raise HTTPException(status_code=404, detail=f"Validation run '{run_id}' not found")


@app.get("/validations/{run_id}/report", dependencies=[Depends(verify_jwt)])
async def get_report(run_id: str) -> ValidationReport:
    run = await get_validation(run_id)
    passed = sum(1 for r in run.results if r.passed)
    failed = len(run.results) - passed
    recommendations = []
    if run.pass_rate < 0.5:
        recommendations.append("Critical failures detected — review telemetry and increase logging verbosity.")
    for r in run.results:
        if not r.passed:
            recommendations.append(f"{r.test_name.value}: score={r.score:.2f} — {r.error_message or 'review test details'}")
    if not recommendations:
        recommendations.append("All tests passed. Consider raising pass_threshold for stricter validation.")
    duration = None
    if run.ended_at and run.started_at:
        duration = (run.ended_at - run.started_at).total_seconds()
    return ValidationReport(
        run_id=run.run_id,
        robot_id=run.robot_id,
        fleet_ids=run.fleet_ids,
        status=run.status,
        overall_passed=run.overall_passed,
        pass_rate=run.pass_rate,
        pass_threshold=run.config.pass_threshold,
        total_tests=len(run.results),
        passed_tests=passed,
        failed_tests=failed,
        results=run.results,
        started_at=run.started_at,
        ended_at=run.ended_at,
        duration_seconds=duration,
        regression_detected=run.regression_detected,
        regression_details=run.regression_details,
        baseline_run_id=run.baseline_run_id,
        summary=(
            f"{'PASSED' if run.overall_passed else 'FAILED'}: "
            f"{passed}/{len(run.results)} tests passed "
            f"(score threshold: {run.config.pass_threshold:.0%})"
        ),
        recommendations=recommendations,
    )


@app.get("/validations", dependencies=[Depends(verify_jwt)])
async def list_validations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ValidationListResponse:
    items = list(_active_runs.values())
    total = len(items)
    start = (page - 1) * page_size
    return ValidationListResponse(
        items=items[start: start + page_size],
        total=total,
        page=page,
        page_size=page_size,
        has_more=(start + page_size) < total,
    )


@app.post("/validations/baselines", dependencies=[Depends(verify_jwt)])
async def set_baseline(req: BaselineSetRequest) -> ValidationBaseline:
    run = await get_validation(req.run_id)
    baseline = ValidationBaseline(
        run_id=run.run_id,
        robot_id=run.robot_id,
        fleet_ids=run.fleet_ids,
        pass_rate=run.pass_rate,
        test_scores={r.test_name.value: r.score for r in run.results},
        description=req.description,
        tags=req.tags,
    )
    _baselines[baseline.baseline_id] = baseline
    log.info("baseline_set", baseline_id=baseline.baseline_id, run_id=run.run_id)
    return baseline


@app.get("/validations/baselines/{baseline_id}", dependencies=[Depends(verify_jwt)])
async def get_baseline(baseline_id: str) -> ValidationBaseline:
    b = _baselines.get(baseline_id)
    if not b:
        raise HTTPException(status_code=404, detail=f"Baseline '{baseline_id}' not found")
    return b


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
