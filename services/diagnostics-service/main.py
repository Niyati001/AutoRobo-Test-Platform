"""
Diagnostics Service — Z-score + CUSUM anomaly detection, root cause analysis,
fleet-level health summary.

Port: 8005
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Deque, Dict, List, Optional, Set

import numpy as np
import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field
from scipy import stats  # type: ignore[import]

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
log = structlog.get_logger("diagnostics-service")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8005"))
TELEMETRY_STREAM_KEY = "telemetry:stream"
ROBOT_SORTED_SET_PREFIX = "telemetry:robot:"

# CUSUM parameters
CUSUM_K = 0.5   # reference value (allowance)
CUSUM_H = 5.0   # decision interval (threshold)

# Anomaly thresholds
ZSCORE_THRESHOLD = 3.0
MIN_SAMPLES_FOR_DETECTION = 20

# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

ANOMALIES_DETECTED = Counter("diagnostics_anomalies_detected_total", "Anomalies detected", ["robot_id", "metric"])
ALERTS_ACTIVE = Gauge("diagnostics_alerts_active", "Currently active alerts")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AnomalyEvent(BaseModel):
    anomaly_id: str
    robot_id: str
    metric: str
    value: float
    z_score: float
    cusum_score: float
    severity: str  # LOW / MEDIUM / HIGH / CRITICAL
    timestamp: float
    description: str
    resolved: bool = False


class RCAResult(BaseModel):
    robot_id: str
    likely_cause: str
    confidence: float
    contributing_metrics: List[str]
    recommended_action: str
    analysis_timestamp: float


class FleetHealthSummary(BaseModel):
    timestamp: float
    total_robots: int
    healthy_robots: int
    degraded_robots: int
    critical_robots: int
    active_anomalies: int
    fleet_health_score: float
    robot_statuses: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Anomaly detection engine
# ---------------------------------------------------------------------------


class RobotDiagnosticsState:
    """Per-robot sliding window state for anomaly detection."""

    WINDOW_SIZE = 200

    def __init__(self, robot_id: str) -> None:
        self.robot_id = robot_id
        self.windows: Dict[str, Deque[float]] = {
            "battery_level": deque(maxlen=self.WINDOW_SIZE),
            "velocity_linear": deque(maxlen=self.WINDOW_SIZE),
            "cpu_usage": deque(maxlen=self.WINDOW_SIZE),
            "memory_usage": deque(maxlen=self.WINDOW_SIZE),
            "cpu_temp": deque(maxlen=self.WINDOW_SIZE),
            "lidar_quality": deque(maxlen=self.WINDOW_SIZE),
            "network_latency_ms": deque(maxlen=self.WINDOW_SIZE),
        }
        self.cusum_pos: Dict[str, float] = {k: 0.0 for k in self.windows}
        self.cusum_neg: Dict[str, float] = {k: 0.0 for k in self.windows}
        self.anomalies: List[AnomalyEvent] = []
        self.last_seen: float = time.time()
        self.latest_sample: Optional[Dict[str, Any]] = None

    def _extract(self, sample: Dict[str, Any]) -> Dict[str, float]:
        return {
            "battery_level": float(sample.get("battery", {}).get("level", 0.8)),
            "velocity_linear": float(sample.get("velocity", {}).get("linear", 0.0)),
            "cpu_usage": float(sample.get("diagnostics", {}).get("cpu_usage", 0.3)),
            "memory_usage": float(sample.get("diagnostics", {}).get("memory_usage", 0.3)),
            "cpu_temp": float(sample.get("diagnostics", {}).get("cpu_temp", 40.0)),
            "lidar_quality": float(sample.get("sensors", {}).get("lidar_quality", 1.0)),
            "network_latency_ms": float(sample.get("diagnostics", {}).get("network_latency_ms", 10.0)),
        }

    def update(self, sample: Dict[str, Any]) -> List[AnomalyEvent]:
        self.last_seen = time.time()
        self.latest_sample = sample
        values = self._extract(sample)
        new_anomalies: List[AnomalyEvent] = []

        for metric, value in values.items():
            self.windows[metric].append(value)
            window = list(self.windows[metric])

            if len(window) < MIN_SAMPLES_FOR_DETECTION:
                continue

            arr = np.array(window, dtype=float)
            mean = float(np.mean(arr[:-1]))  # exclude latest
            std = float(np.std(arr[:-1])) or 0.01

            z_score = (value - mean) / std

            # CUSUM update
            self.cusum_pos[metric] = max(0.0, self.cusum_pos[metric] + z_score - CUSUM_K)
            self.cusum_neg[metric] = max(0.0, self.cusum_neg[metric] - z_score - CUSUM_K)
            cusum_score = max(self.cusum_pos[metric], self.cusum_neg[metric])

            anomaly_detected = abs(z_score) > ZSCORE_THRESHOLD or cusum_score > CUSUM_H
            if not anomaly_detected:
                continue

            severity = (
                "CRITICAL" if abs(z_score) > 6.0 or cusum_score > 15.0
                else "HIGH" if abs(z_score) > 4.5 or cusum_score > 10.0
                else "MEDIUM" if abs(z_score) > ZSCORE_THRESHOLD
                else "LOW"
            )

            event = AnomalyEvent(
                anomaly_id=f"{self.robot_id}_{metric}_{int(time.time()*1000)}",
                robot_id=self.robot_id,
                metric=metric,
                value=round(value, 4),
                z_score=round(z_score, 3),
                cusum_score=round(cusum_score, 3),
                severity=severity,
                timestamp=float(sample.get("timestamp", time.time())),
                description=f"Anomaly in {metric}: value={value:.3f}, z={z_score:.2f}, cusum={cusum_score:.2f}",
            )
            self.anomalies.append(event)
            new_anomalies.append(event)
            ANOMALIES_DETECTED.labels(robot_id=self.robot_id, metric=metric).inc()
            # Reset CUSUM after alert to avoid alert storm
            self.cusum_pos[metric] = 0.0
            self.cusum_neg[metric] = 0.0

        return new_anomalies

    def run_rca(self) -> RCAResult:
        """Identify the most likely root cause from recent anomalies."""
        recent = [a for a in self.anomalies[-20:] if not a.resolved]
        if not recent:
            return RCAResult(
                robot_id=self.robot_id,
                likely_cause="No active anomalies detected",
                confidence=1.0,
                contributing_metrics=[],
                recommended_action="Continue normal operation.",
                analysis_timestamp=time.time(),
            )

        metric_counts: Dict[str, int] = {}
        for a in recent:
            metric_counts[a.metric] = metric_counts.get(a.metric, 0) + 1

        top_metric = max(metric_counts, key=lambda k: metric_counts[k])
        top_count = metric_counts[top_metric]
        confidence = min(top_count / max(len(recent), 1), 1.0)

        cause_map = {
            "battery_level": ("Low battery / rapid discharge", "Schedule robot for charging immediately."),
            "cpu_usage": ("CPU overload / runaway process", "Restart navigation stack; check for stuck planners."),
            "cpu_temp": ("Thermal throttling", "Reduce workload; check cooling system."),
            "memory_usage": ("Memory leak", "Restart robot software stack."),
            "lidar_quality": ("LiDAR sensor degradation", "Clean LiDAR lens; check for physical obstructions."),
            "velocity_linear": ("Erratic motion / motor fault", "Inspect motors and wheel encoders."),
            "network_latency_ms": ("Network congestion", "Check WiFi channel; consider QoS policies."),
        }
        cause, action = cause_map.get(top_metric, ("Unknown anomaly pattern", "Inspect robot manually."))

        return RCAResult(
            robot_id=self.robot_id,
            likely_cause=cause,
            confidence=round(confidence, 3),
            contributing_metrics=list(metric_counts.keys()),
            recommended_action=action,
            analysis_timestamp=time.time(),
        )

    def health_status(self) -> str:
        recent_critical = sum(1 for a in self.anomalies[-10:] if not a.resolved and a.severity == "CRITICAL")
        recent_high = sum(1 for a in self.anomalies[-10:] if not a.resolved and a.severity == "HIGH")
        if recent_critical > 0:
            return "CRITICAL"
        if recent_high >= 2:
            return "DEGRADED"
        return "HEALTHY"


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_redis: Optional[aioredis.Redis] = None
_robot_states: Dict[str, RobotDiagnosticsState] = {}
_anomaly_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
_ws_subscribers: Set[WebSocket] = set()
_consumer_task: Optional[asyncio.Task] = None


async def _consume_telemetry() -> None:
    if _redis is None:
        return
    last_id = "$"  # only new messages
    # Wait briefly for stream to exist
    await asyncio.sleep(2.0)
    log.info("diagnostics_consumer_started")

    while True:
        try:
            messages = await _redis.xread({TELEMETRY_STREAM_KEY: last_id}, count=50, block=200)
            if not messages:
                await asyncio.sleep(0.05)
                continue
            for _stream, entries in messages:
                for msg_id, fields in entries:
                    last_id = msg_id
                    payload_str = fields.get("payload") or fields.get("data")
                    if not payload_str:
                        continue
                    try:
                        sample = json.loads(payload_str)
                    except Exception:
                        continue

                    robot_id = sample.get("robot_id", "unknown")
                    if robot_id not in _robot_states:
                        _robot_states[robot_id] = RobotDiagnosticsState(robot_id)
                    new_anomalies = _robot_states[robot_id].update(sample)

                    for anomaly in new_anomalies:
                        ALERTS_ACTIVE.inc()
                        # Publish anomaly to subscribers
                        event_json = anomaly.model_dump_json()
                        # Push to Redis for notification-service
                        if _redis:
                            await _redis.publish("diagnostics:anomalies", event_json)
                        # Fan out to WebSocket subscribers
                        dead = set()
                        for ws in _ws_subscribers.copy():
                            try:
                                await ws.send_text(event_json)
                            except Exception:
                                dead.add(ws)
                        _ws_subscribers -= dead

        except aioredis.RedisError as exc:
            log.warning("diagnostics_redis_error", error=str(exc))
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("diagnostics_consumer_error", error=str(exc))
            await asyncio.sleep(0.5)

    log.info("diagnostics_consumer_stopped")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis, _consumer_task
    _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    _consumer_task = asyncio.create_task(_consume_telemetry())
    log.info("diagnostics_service_started", port=SERVICE_PORT)
    yield
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    if _redis:
        await _redis.aclose()
    log.info("diagnostics_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Diagnostics Service",
    version="1.0.0",
    description="Real-time anomaly detection and root cause analysis for robot fleets",
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
    return {
        "status": "ok" if redis_ok else "degraded",
        "service": "diagnostics-service",
        "redis": "ok" if redis_ok else "error",
        "monitored_robots": len(_robot_states),
        "active_ws": len(_ws_subscribers),
    }


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/diagnostics/fleet/summary", dependencies=[Depends(verify_jwt)])
async def fleet_summary() -> FleetHealthSummary:
    robot_statuses = []
    healthy = degraded = critical = 0
    total_anomalies = 0

    for robot_id, state in _robot_states.items():
        hs = state.health_status()
        active_anomalies = sum(1 for a in state.anomalies[-20:] if not a.resolved)
        total_anomalies += active_anomalies
        if hs == "HEALTHY":
            healthy += 1
        elif hs == "DEGRADED":
            degraded += 1
        else:
            critical += 1

        robot_statuses.append({
            "robot_id": robot_id,
            "health_status": hs,
            "active_anomalies": active_anomalies,
            "last_seen": state.last_seen,
            "battery": (state.latest_sample or {}).get("battery", {}).get("level"),
            "nav_state": (state.latest_sample or {}).get("navigation", {}).get("state"),
        })

    total = len(_robot_states)
    health_score = (healthy + 0.5 * degraded) / max(total, 1)

    return FleetHealthSummary(
        timestamp=time.time(),
        total_robots=total,
        healthy_robots=healthy,
        degraded_robots=degraded,
        critical_robots=critical,
        active_anomalies=total_anomalies,
        fleet_health_score=round(health_score, 3),
        robot_statuses=robot_statuses,
    )


@app.get("/diagnostics/{robot_id}/anomalies", dependencies=[Depends(verify_jwt)])
async def get_anomalies(
    robot_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    unresolved_only: bool = Query(default=False),
) -> dict:
    state = _robot_states.get(robot_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Robot '{robot_id}' not monitored")
    anomalies = state.anomalies[-limit:]
    if unresolved_only:
        anomalies = [a for a in anomalies if not a.resolved]
    return {"robot_id": robot_id, "count": len(anomalies), "anomalies": [a.model_dump() for a in anomalies]}


@app.get("/diagnostics/{robot_id}/rca", dependencies=[Depends(verify_jwt)])
async def root_cause_analysis(robot_id: str) -> RCAResult:
    state = _robot_states.get(robot_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Robot '{robot_id}' not monitored")
    return state.run_rca()


@app.patch("/diagnostics/{robot_id}/anomalies/{anomaly_id}/resolve", dependencies=[Depends(verify_jwt)])
async def resolve_anomaly(robot_id: str, anomaly_id: str) -> dict:
    state = _robot_states.get(robot_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Robot '{robot_id}' not found")
    for anomaly in state.anomalies:
        if anomaly.anomaly_id == anomaly_id:
            anomaly.resolved = True
            ALERTS_ACTIVE.dec()
            return {"status": "resolved", "anomaly_id": anomaly_id}
    raise HTTPException(status_code=404, detail=f"Anomaly '{anomaly_id}' not found")


# ---------------------------------------------------------------------------
# WebSocket: live anomaly feed
# ---------------------------------------------------------------------------

@app.websocket("/ws/diagnostics/{robot_id}")
async def ws_diagnostics(websocket: WebSocket, robot_id: str) -> None:
    await websocket.accept()
    _ws_subscribers.add(websocket)
    log.info("diag_ws_connected", robot_id=robot_id)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_subscribers.discard(websocket)
        log.info("diag_ws_disconnected", robot_id=robot_id)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=False)
