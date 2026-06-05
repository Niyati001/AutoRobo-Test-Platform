"""
Simulation Service — FastAPI microservice for managing robot fleet simulations.

Port: 8002
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import asyncpg
import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Path setup so we can import the robotics package
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from robotics.synthetic_simulator.fleet_manager import FleetManager
from robotics.synthetic_simulator.models import (
    FaultType,
    Mission,
    MissionType,
    SimulationConfig,
)
from robotics.synthetic_simulator.telemetry_publisher import TelemetryPublisher
from robotics.synthetic_simulator.warehouse_physics import WarehousePhysics
from robotics.warehouse_maps import get_named_waypoints, get_warehouse_map
from robotics.synthetic_simulator.models import WarehouseMap

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger("simulation-service")

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://arvp:arvp_pass@postgres:5432/arvp_db",
)
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
SERVICE_PORT = int(os.getenv("SIMULATION_SERVICE_PORT", "8002"))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

SIM_RUNS_STARTED = Counter(
    "simulation_runs_started_total",
    "Total simulation runs started",
)
SIM_RUNS_STOPPED = Counter(
    "simulation_runs_stopped_total",
    "Total simulation runs stopped",
)
ACTIVE_SIMULATIONS = Gauge(
    "active_simulations",
    "Currently running simulation count",
)
ACTIVE_ROBOTS = Gauge(
    "active_robots_total",
    "Total active robots across all simulations",
)
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)

# ---------------------------------------------------------------------------
# Database engine
# ---------------------------------------------------------------------------

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------


class SimulationRun:
    def __init__(self, run_id: str, config: SimulationConfig, fleet: FleetManager) -> None:
        self.run_id = run_id
        self.config = config
        self.fleet = fleet
        self.status = "running"
        self.started_at = time.time()
        self.ended_at: Optional[float] = None


# Global registry of running simulations
_simulations: dict[str, SimulationRun] = {}

# Shared publisher (single Redis connection pool)
_publisher: Optional[TelemetryPublisher] = None

# ---------------------------------------------------------------------------
# Warehouse map (loaded once)
# ---------------------------------------------------------------------------

def _build_warehouse_physics() -> WarehousePhysics:
    grid = get_warehouse_map()
    waypoints = get_named_waypoints()
    wmap = WarehouseMap(
        name="small_warehouse",
        grid=grid,
        cell_size_m=0.5,
        rows=50,
        cols=50,
        origin_x=0.0,
        origin_y=0.0,
        named_waypoints=waypoints,
    )
    return WarehousePhysics(wmap)


_physics: Optional[WarehousePhysics] = None

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _publisher, _physics

    log.info("simulation_service_starting")

    # Build physics engine (CPU-only, no I/O)
    _physics = _build_warehouse_physics()
    log.info("warehouse_physics_loaded")

    # Create telemetry publisher
    _publisher = TelemetryPublisher(redis_url=REDIS_URL)
    await _publisher.connect()
    log.info("telemetry_publisher_connected")

    yield  # --- application runs ---

    log.info("simulation_service_shutting_down")

    # Stop all running simulations
    for run_id in list(_simulations.keys()):
        await _stop_simulation(run_id)

    if _publisher:
        await _publisher.close()

    await engine.dispose()
    log.info("simulation_service_stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Simulation Service",
    version="1.0.0",
    description="Manages autonomous robot fleet simulations",
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
# Auth middleware
# ---------------------------------------------------------------------------

security = HTTPBearer(auto_error=False)
SKIP_AUTH_PATHS = {"/health", "/metrics", "/docs", "/openapi.json", "/redoc"}


async def verify_jwt(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if request.url.path in SKIP_AUTH_PATHS:
        return {}
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
        )
    try:
        payload = jwt.decode(
            credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM]
        )
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )


# ---------------------------------------------------------------------------
# Middleware: request logging + metrics
# ---------------------------------------------------------------------------


@app.middleware("http")
async def logging_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    HTTP_REQUESTS.labels(
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
    ).inc()
    log.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(duration * 1000, 1),
    )
    return response


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class StartSimulationRequest(BaseModel):
    run_id: Optional[str] = Field(default=None)
    map_name: str = Field(default="small_warehouse")
    num_robots: int = Field(default=3, ge=1, le=20)
    robot_ids: list[str] = Field(default_factory=list)
    telemetry_rate_hz: float = Field(default=10.0, gt=0.0, le=100.0)
    fault_injection_enabled: bool = Field(default=False)
    fault_probability_per_minute: float = Field(default=0.05)
    mission_type: str = Field(default="PATROL_ROUTE")
    duration_seconds: Optional[float] = Field(default=None)
    seed: Optional[int] = Field(default=None)


class AddRobotRequest(BaseModel):
    robot_id: str
    initial_battery: float = Field(default=1.0, ge=0.0, le=1.0)
    initial_x: float = Field(default=12.5)
    initial_y: float = Field(default=12.5)


class AssignMissionRequest(BaseModel):
    mission_type: str = Field(default="MOVE_TO_GOAL")
    goal_x: Optional[float] = None
    goal_y: Optional[float] = None
    waypoints: list[list[float]] = Field(default_factory=list)
    priority: int = Field(default=5, ge=1, le=10)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _persist_simulation_run(
    run_id: str,
    config: SimulationConfig,
    status: str,
    started_at: float,
    ended_at: Optional[float] = None,
) -> None:
    """Upsert a simulation_runs record."""
    import json
    try:
        async with AsyncSessionLocal() as session:
            check = await session.execute(
                text("SELECT id FROM simulation_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            existing = check.fetchone()
            if existing:
                await session.execute(
                    text(
                        "UPDATE simulation_runs SET status=:status, ended_at=TO_TIMESTAMP(:ended_at) "
                        "WHERE run_id=:run_id"
                    ),
                    {
                        "run_id": run_id,
                        "status": status,
                        "ended_at": ended_at,
                    },
                )
            else:
                await session.execute(
                    text(
                        "INSERT INTO simulation_runs (id, run_id, config, status, started_at) "
                        "VALUES (:id, :run_id, :config, :status, TO_TIMESTAMP(:started_at))"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "run_id": run_id,
                        "config": json.dumps(config.model_dump()),
                        "status": status,
                        "started_at": started_at,
                    },
                )
            await session.commit()
    except Exception as exc:
        log.error("db_persist_failed", run_id=run_id, error=str(exc))


async def _stop_simulation(run_id: str) -> bool:
    sim_run = _simulations.get(run_id)
    if not sim_run:
        return False
    await sim_run.fleet.stop()
    sim_run.status = "stopped"
    sim_run.ended_at = time.time()
    ACTIVE_SIMULATIONS.dec()
    ACTIVE_ROBOTS.dec(len(sim_run.fleet.get_robot_ids()))
    await _persist_simulation_run(
        run_id, sim_run.config, "stopped", sim_run.started_at, sim_run.ended_at
    )
    del _simulations[run_id]
    SIM_RUNS_STOPPED.inc()
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health_check() -> dict:
    """Health check: verifies Redis and DB connectivity."""
    checks: dict[str, Any] = {"status": "ok", "service": "simulation-service"}

    # Redis check
    if _publisher:
        redis_ok = await _publisher.is_healthy()
        checks["redis"] = "ok" if redis_ok else "degraded"
    else:
        checks["redis"] = "not_initialized"

    # DB check
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        checks["status"] = "degraded"

    checks["active_simulations"] = len(_simulations)
    return checks


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post(
    "/simulations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_jwt)],
)
async def start_simulation(body: StartSimulationRequest) -> dict:
    """Start a new simulation run."""
    global _physics, _publisher

    if _physics is None or _publisher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service not ready",
        )

    run_id = body.run_id or str(uuid.uuid4())
    if run_id in _simulations:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Simulation run_id '{run_id}' already exists",
        )

    config = SimulationConfig(
        run_id=run_id,
        map_name=body.map_name,
        num_robots=body.num_robots,
        robot_ids=body.robot_ids,
        telemetry_rate_hz=body.telemetry_rate_hz,
        fault_injection_enabled=body.fault_injection_enabled,
        fault_probability_per_minute=body.fault_probability_per_minute,
        mission_type=MissionType(body.mission_type),
        duration_seconds=body.duration_seconds,
        seed=body.seed,
    )

    fleet = FleetManager(physics=_physics, publisher=_publisher, config=config)
    await fleet.start()

    # Spawn robots at staggered positions
    spawn_positions = [
        (12.5, 5.0), (12.5, 10.0), (12.5, 15.0),
        (12.5, 20.0), (6.5, 5.0), (6.5, 10.0),
        (6.5, 15.0), (6.5, 20.0), (18.5, 5.0), (18.5, 10.0),
    ]
    for i, robot_id in enumerate(config.robot_ids):
        pos = spawn_positions[i % len(spawn_positions)]
        await fleet.add_robot(
            robot_id=robot_id,
            initial_position=pos,
            initial_battery=0.8 + 0.2 * (i % 3) / 2,
            seed=(config.seed + i) if config.seed is not None else None,
        )

    sim_run = SimulationRun(run_id=run_id, config=config, fleet=fleet)
    _simulations[run_id] = sim_run

    SIM_RUNS_STARTED.inc()
    ACTIVE_SIMULATIONS.inc()
    ACTIVE_ROBOTS.inc(len(config.robot_ids))

    await _persist_simulation_run(run_id, config, "running", sim_run.started_at)

    log.info("simulation_started", run_id=run_id, num_robots=config.num_robots)
    return {
        "run_id": run_id,
        "status": "running",
        "robot_ids": config.robot_ids,
        "started_at": sim_run.started_at,
    }


@app.get("/simulations/{run_id}", dependencies=[Depends(verify_jwt)])
async def get_simulation(run_id: str) -> dict:
    """Get simulation status and fleet summary."""
    sim_run = _simulations.get(run_id)
    if not sim_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Simulation '{run_id}' not found",
        )
    fleet_status = sim_run.fleet.get_fleet_status()
    return {
        "run_id": run_id,
        "status": sim_run.status,
        "started_at": sim_run.started_at,
        "ended_at": sim_run.ended_at,
        "config": sim_run.config.model_dump(),
        "fleet": fleet_status,
    }


@app.delete("/simulations/{run_id}", dependencies=[Depends(verify_jwt)])
async def stop_simulation(run_id: str) -> dict:
    """Stop a running simulation."""
    stopped = await _stop_simulation(run_id)
    if not stopped:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Simulation '{run_id}' not found",
        )
    log.info("simulation_stopped", run_id=run_id)
    return {"run_id": run_id, "status": "stopped"}


@app.get("/simulations/{run_id}/robots", dependencies=[Depends(verify_jwt)])
async def list_robots(run_id: str) -> dict:
    """List all robots in a simulation."""
    sim_run = _simulations.get(run_id)
    if not sim_run:
        raise HTTPException(status_code=404, detail=f"Simulation '{run_id}' not found")
    fleet_status = sim_run.fleet.get_fleet_status()
    return {
        "run_id": run_id,
        "robots": list(fleet_status["robots"].values()),
    }


@app.post(
    "/simulations/{run_id}/robots",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_jwt)],
)
async def add_robot(run_id: str, body: AddRobotRequest) -> dict:
    """Add a robot to a running simulation."""
    sim_run = _simulations.get(run_id)
    if not sim_run:
        raise HTTPException(status_code=404, detail=f"Simulation '{run_id}' not found")

    success = await sim_run.fleet.add_robot(
        robot_id=body.robot_id,
        initial_position=(body.initial_x, body.initial_y),
        initial_battery=body.initial_battery,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Robot '{body.robot_id}' already exists or fleet is at capacity",
        )
    ACTIVE_ROBOTS.inc()
    return {"run_id": run_id, "robot_id": body.robot_id, "status": "added"}


@app.delete(
    "/simulations/{run_id}/robots/{robot_id}",
    dependencies=[Depends(verify_jwt)],
)
async def remove_robot(run_id: str, robot_id: str) -> dict:
    """Remove a robot from a running simulation."""
    sim_run = _simulations.get(run_id)
    if not sim_run:
        raise HTTPException(status_code=404, detail=f"Simulation '{run_id}' not found")

    removed = await sim_run.fleet.remove_robot(robot_id)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"Robot '{robot_id}' not found in simulation '{run_id}'",
        )
    ACTIVE_ROBOTS.dec()
    return {"run_id": run_id, "robot_id": robot_id, "status": "removed"}


@app.post(
    "/simulations/{run_id}/robots/{robot_id}/mission",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_jwt)],
)
async def assign_mission(run_id: str, robot_id: str, body: AssignMissionRequest) -> dict:
    """Assign a mission to a robot."""
    sim_run = _simulations.get(run_id)
    if not sim_run:
        raise HTTPException(status_code=404, detail=f"Simulation '{run_id}' not found")

    if robot_id not in sim_run.fleet.get_robot_ids():
        raise HTTPException(
            status_code=404,
            detail=f"Robot '{robot_id}' not found in simulation '{run_id}'",
        )

    try:
        mission_type = MissionType(body.mission_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown mission type: {body.mission_type}",
        )

    if mission_type == MissionType.MOVE_TO_GOAL:
        if body.goal_x is None or body.goal_y is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="goal_x and goal_y are required for MOVE_TO_GOAL missions",
            )
        mission_id = await sim_run.fleet.assign_move_to_goal(
            robot_id, body.goal_x, body.goal_y, body.priority
        )
    else:
        waypoints = [(wp[0], wp[1]) for wp in body.waypoints if len(wp) >= 2]
        if not waypoints and mission_type == MissionType.PATROL_ROUTE:
            # Use default waypoints from warehouse map
            named_wps = get_named_waypoints()
            waypoints = [
                named_wps["SHELF_ROW_1_WEST"],
                named_wps["SHELF_ROW_3_WEST"],
                named_wps["SHELF_ROW_5_WEST"],
                named_wps["CORRIDOR_MAIN_SOUTH"],
            ]
        mission = Mission(
            mission_id=str(uuid.uuid4()),
            robot_id=robot_id,
            mission_type=mission_type,
            waypoints=waypoints,
            priority=body.priority,
        )
        mission_id = (
            await sim_run.fleet.assign_mission(robot_id, mission)
            and mission.mission_id
        )

    if not mission_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to assign mission",
        )

    return {
        "run_id": run_id,
        "robot_id": robot_id,
        "mission_id": mission_id,
        "status": "accepted",
    }
