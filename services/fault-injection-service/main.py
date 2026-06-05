"""
Fault Injection Service — FastAPI application.

Port: 8004
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from redis.asyncio import Redis, from_url as redis_from_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import Response

from fault_engine import FaultInjectionEngine
from models import (
    CampaignStatus,
    FaultCampaign,
    FaultCampaignStep,
    FaultConfig,
    FaultEvent,
    FaultListResponse,
    FaultResolveRequest,
    FaultSchedule,
    FaultSeverity,
    FaultTemplate,
    FaultType,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://arvp:arvp_pass@postgres:5432/arvp_db")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
SERVICE_PORT = int(os.getenv("PORT", "8004"))

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pre-built fault templates
# ---------------------------------------------------------------------------
_FAULT_TEMPLATES: Dict[str, FaultTemplate] = {}


def _build_templates() -> None:
    # 1. Warehouse fire drill — E-stop all robots
    _FAULT_TEMPLATES["warehouse_fire_drill"] = FaultTemplate(
        template_name="warehouse_fire_drill",
        display_name="Warehouse Fire Drill",
        description="Triggers E-STOP on the target robot simulating emergency evacuation protocol",
        category="warehouse",
        campaign=FaultCampaign(
            name="Warehouse Fire Drill",
            steps=[
                FaultCampaignStep(
                    fault_config=FaultConfig(
                        robot_id="amr-001",
                        fault_type=FaultType.ESTOP,
                        severity=FaultSeverity.CRITICAL,
                        duration_seconds=60,
                    ),
                    delay_before_seconds=0,
                )
            ],
        ),
    )

    # 2. Robot jam — navigation blockage
    _FAULT_TEMPLATES["robot_jam"] = FaultTemplate(
        template_name="robot_jam",
        display_name="Robot Traffic Jam",
        description="Injects navigation blockage to simulate a physical obstacle blocking robot paths",
        category="navigation",
        campaign=FaultCampaign(
            name="Robot Traffic Jam",
            steps=[
                FaultCampaignStep(
                    fault_config=FaultConfig(
                        robot_id="amr-001",
                        fault_type=FaultType.NAVIGATION_BLOCKAGE,
                        severity=FaultSeverity.HIGH,
                        parameters={"obstacle_x": 10.0, "obstacle_y": 5.0, "obstacle_radius": 2.0},
                        duration_seconds=120,
                    ),
                    delay_before_seconds=0,
                )
            ],
        ),
    )

    # 3. Battery drain test
    _FAULT_TEMPLATES["battery_drain_test"] = FaultTemplate(
        template_name="battery_drain_test",
        display_name="Battery Drain Endurance Test",
        description="Accelerates battery discharge to validate low-battery behaviour and charging protocols",
        category="battery",
        campaign=FaultCampaign(
            name="Battery Drain Test",
            steps=[
                FaultCampaignStep(
                    fault_config=FaultConfig(
                        robot_id="amr-001",
                        fault_type=FaultType.BATTERY_DRAIN,
                        severity=FaultSeverity.MEDIUM,
                        parameters={"drain_rate_per_second": 0.003, "min_battery_level": 0.05},
                        duration_seconds=300,
                    ),
                    delay_before_seconds=0,
                )
            ],
        ),
    )

    # 4. Sensor degradation cascade
    _FAULT_TEMPLATES["sensor_degradation_cascade"] = FaultTemplate(
        template_name="sensor_degradation_cascade",
        display_name="Sensor Degradation Cascade",
        description="Gradually degrades LiDAR then adds sensor noise to test multi-sensor fusion robustness",
        category="sensor",
        campaign=FaultCampaign(
            name="Sensor Degradation Cascade",
            steps=[
                FaultCampaignStep(
                    fault_config=FaultConfig(
                        robot_id="amr-001",
                        fault_type=FaultType.LIDAR_DEGRADATION,
                        severity=FaultSeverity.HIGH,
                        parameters={"degradation_factor": 0.5, "ramp_seconds": 15},
                        duration_seconds=90,
                    ),
                    delay_before_seconds=0,
                ),
                FaultCampaignStep(
                    fault_config=FaultConfig(
                        robot_id="amr-001",
                        fault_type=FaultType.SENSOR_NOISE,
                        severity=FaultSeverity.MEDIUM,
                        parameters={"noise_amplitude": 0.08},
                        duration_seconds=60,
                    ),
                    delay_before_seconds=10,
                    wait_for_resolution=False,
                ),
            ],
        ),
    )

    # 5. Fleet stress test
    _FAULT_TEMPLATES["fleet_stress_test"] = FaultTemplate(
        template_name="fleet_stress_test",
        display_name="Fleet Stress Test",
        description="Cascading motor faults across multiple robots to validate fleet management resilience",
        category="fleet",
        campaign=FaultCampaign(
            name="Fleet Stress Test",
            steps=[
                FaultCampaignStep(
                    fault_config=FaultConfig(
                        robot_id="amr-001",
                        fault_type=FaultType.CASCADING_FAILURE,
                        severity=FaultSeverity.HIGH,
                        parameters={
                            "cascade_delay_seconds": 5,
                            "secondary_fault_type": "MOTOR_FAULT",
                        },
                        duration_seconds=120,
                        cascade_targets=["amr-002", "amr-003"],
                    ),
                    delay_before_seconds=0,
                )
            ],
        ),
    )

    # 6. Network chaos
    _FAULT_TEMPLATES["network_chaos"] = FaultTemplate(
        template_name="network_chaos",
        display_name="Network Packet Loss Chaos",
        description="Simulates network instability by introducing packet loss to test telemetry resilience",
        category="network",
        campaign=FaultCampaign(
            name="Network Chaos",
            steps=[
                FaultCampaignStep(
                    fault_config=FaultConfig(
                        robot_id="amr-001",
                        fault_type=FaultType.NETWORK_PACKET_LOSS,
                        severity=FaultSeverity.MEDIUM,
                        parameters={"drop_every_nth": 3},
                        duration_seconds=120,
                    ),
                    delay_before_seconds=0,
                )
            ],
        ),
    )


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
redis_client: Optional[Redis] = None
fault_engine: Optional[FaultInjectionEngine] = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    global redis_client, fault_engine

    redis_client = redis_from_url(REDIS_URL, decode_responses=True)
    engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=10, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    fault_engine = FaultInjectionEngine(redis_client, session_factory)
    _build_templates()

    logger.info("fault_injection_service_started", port=SERVICE_PORT)
    yield

    # Shutdown
    if redis_client:
        await redis_client.aclose()
    await engine.dispose()
    logger.info("fault_injection_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Fault Injection Service",
    description="Chaos engineering service for Autonomous Robotics Validation Platform",
    version="1.0.0",
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
bearer_scheme = HTTPBearer(auto_error=False)


def verify_jwt(credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme)) -> Dict[str, Any]:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}") from exc


def get_engine() -> FaultInjectionEngine:
    if fault_engine is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return fault_engine


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    redis_ok = False
    try:
        if redis_client:
            await redis_client.ping()
            redis_ok = True
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": "healthy" if redis_ok else "degraded",
        "service": "fault-injection-service",
        "redis": "connected" if redis_ok else "disconnected",
        "active_faults": len(fault_engine._active_faults) if fault_engine else 0,
    }


@app.get("/metrics")
async def metrics() -> Response:
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


@app.post("/faults", response_model=FaultEvent, status_code=status.HTTP_201_CREATED)
async def inject_fault(
    config: FaultConfig,
    engine: FaultInjectionEngine = Depends(get_engine),
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> FaultEvent:
    """Inject a fault immediately."""
    try:
        fault_event = await engine.inject_fault(config)
        logger.info("fault_injected_via_api", fault_id=fault_event.fault_id, robot_id=config.robot_id)
        return fault_event
    except Exception as exc:
        logger.exception("fault_injection_api_error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/faults/schedule", response_model=FaultSchedule, status_code=status.HTTP_201_CREATED)
async def schedule_fault(
    schedule: FaultSchedule,
    engine: FaultInjectionEngine = Depends(get_engine),
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> FaultSchedule:
    """Schedule a fault using a cron expression."""
    try:
        await engine.start_schedule(schedule)
        return schedule
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/faults/campaign", response_model=CampaignStatus, status_code=status.HTTP_202_ACCEPTED)
async def run_campaign(
    campaign: FaultCampaign,
    engine: FaultInjectionEngine = Depends(get_engine),
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> CampaignStatus:
    """Start a fault campaign (asynchronous execution)."""
    try:
        campaign_status = await engine.run_campaign(campaign)
        return campaign_status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/faults", response_model=FaultListResponse)
async def list_faults(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    engine: FaultInjectionEngine = Depends(get_engine),
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> FaultListResponse:
    """List all faults (active + historical) with pagination."""
    data = await engine.load_faults_from_db(page=page, page_size=page_size)
    items: List[FaultEvent] = data["items"]
    total: int = data["total"]
    return FaultListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@app.get("/faults/templates", response_model=List[Dict[str, Any]])
async def list_templates(
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> List[Dict[str, Any]]:
    """List all pre-built fault scenario templates."""
    return [
        {
            "template_name": t.template_name,
            "display_name": t.display_name,
            "description": t.description,
            "category": t.category,
        }
        for t in _FAULT_TEMPLATES.values()
    ]


@app.post("/faults/templates/{template_name}", response_model=CampaignStatus, status_code=status.HTTP_202_ACCEPTED)
async def execute_template(
    template_name: str,
    robot_id: Optional[str] = Query(default=None, description="Override robot_id in template"),
    engine: FaultInjectionEngine = Depends(get_engine),
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> CampaignStatus:
    """Execute a named fault template (optionally override robot_id)."""
    template = _FAULT_TEMPLATES.get(template_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")

    campaign = template.campaign.model_copy(deep=True)
    campaign.campaign_id = str(uuid.uuid4())

    if robot_id:
        for step in campaign.steps:
            step.fault_config.robot_id = robot_id

    try:
        campaign_status = await engine.run_campaign(campaign)
        return campaign_status
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/faults/{fault_id}", response_model=FaultEvent)
async def get_fault(
    fault_id: str,
    engine: FaultInjectionEngine = Depends(get_engine),
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> FaultEvent:
    """Get a specific fault by ID."""
    # Check active faults first (faster)
    active = engine._active_faults.get(fault_id)
    if active:
        return active
    # Fall back to DB
    fault = await engine.get_fault_from_db(fault_id)
    if fault is None:
        raise HTTPException(status_code=404, detail=f"Fault '{fault_id}' not found")
    return fault


@app.delete("/faults/{fault_id}", status_code=status.HTTP_200_OK)
async def cancel_fault(
    fault_id: str,
    body: Optional[FaultResolveRequest] = None,
    engine: FaultInjectionEngine = Depends(get_engine),
    _claims: Dict[str, Any] = Depends(verify_jwt),
) -> Dict[str, Any]:
    """Cancel / resolve an active fault."""
    reason = body.reason if body else "manually cancelled via API"
    resolved = await engine.resolve_fault(fault_id, reason=reason)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"Active fault '{fault_id}' not found")
    return {"message": "Fault resolved", "fault_id": fault_id, "status": "RESOLVED"}


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", path=str(request.url))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        log_level="info",
        access_log=True,
    )
