"""
Pydantic v2 models for the synthetic robotics simulator.
Covers all telemetry, state, map, and configuration data structures.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RobotMode(str, Enum):
    AUTONOMOUS = "AUTONOMOUS"
    MANUAL = "MANUAL"
    TELEOPERATED = "TELEOPERATED"
    DOCKED = "DOCKED"


class NavigationState(str, Enum):
    IDLE = "IDLE"
    MOVING = "MOVING"
    ROTATING = "ROTATING"
    OBSTACLE_AVOIDANCE = "OBSTACLE_AVOIDANCE"
    GOAL_REACHED = "GOAL_REACHED"
    REPLANNING = "REPLANNING"
    STUCK = "STUCK"


class RobotSimState(str, Enum):
    IDLE = "IDLE"
    CHARGING = "CHARGING"
    MOVING = "MOVING"
    OBSTACLE_AVOIDANCE = "OBSTACLE_AVOIDANCE"
    FAULT = "FAULT"
    RECOVERY = "RECOVERY"
    ESTOP = "ESTOP"


class MissionType(str, Enum):
    MOVE_TO_GOAL = "MOVE_TO_GOAL"
    PATROL_ROUTE = "PATROL_ROUTE"
    DOCK_AT_CHARGER = "DOCK_AT_CHARGER"
    PICKUP = "PICKUP"
    DROPOFF = "DROPOFF"


class FaultType(str, Enum):
    ESTOP = "ESTOP"
    MOTOR_FAULT = "MOTOR_FAULT"
    BATTERY_CRITICAL = "BATTERY_CRITICAL"
    LIDAR_DEGRADED = "LIDAR_DEGRADED"
    IMU_FAILURE = "IMU_FAILURE"
    NETWORK_LOSS = "NETWORK_LOSS"
    OBSTACLE_STUCK = "OBSTACLE_STUCK"


class CellType(int, Enum):
    FREE = 0
    WALL = 1
    SHELF = 2
    CHARGING_STATION = 3
    PICKUP_ZONE = 4
    DROPOFF_ZONE = 5
    CONGESTION_ZONE = 6


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Position(BaseModel):
    x: float = Field(..., description="X coordinate in meters")
    y: float = Field(..., description="Y coordinate in meters")
    theta: float = Field(default=0.0, description="Heading in radians [-π, π]")

    @field_validator("theta")
    @classmethod
    def clamp_theta(cls, v: float) -> float:
        import math
        # Normalise to [-π, π]
        while v > math.pi:
            v -= 2 * math.pi
        while v < -math.pi:
            v += 2 * math.pi
        return v


class Velocity(BaseModel):
    linear: float = Field(default=0.0, ge=-2.0, le=2.0, description="Linear velocity m/s")
    angular: float = Field(default=0.0, ge=-3.14, le=3.14, description="Angular velocity rad/s")


class BatteryState(BaseModel):
    level: float = Field(..., ge=0.0, le=1.0, description="State of charge [0,1]")
    voltage: float = Field(default=24.0, ge=0.0, le=30.0, description="Pack voltage (V)")
    current: float = Field(default=0.0, description="Draw current (A), positive = discharging")

    @field_validator("level")
    @classmethod
    def round_level(cls, v: float) -> float:
        return round(v, 4)


class MotorState(BaseModel):
    left_rpm: float = Field(default=0.0, description="Left wheel RPM")
    right_rpm: float = Field(default=0.0, description="Right wheel RPM")
    torque: float = Field(default=0.0, ge=0.0, description="Motor torque (Nm)")


class SensorState(BaseModel):
    lidar_quality: float = Field(default=1.0, ge=0.0, le=1.0)
    camera_active: bool = Field(default=True)
    imu_drift: float = Field(default=0.0, ge=0.0, description="IMU drift magnitude (rad/s)")


class NavigationStatus(BaseModel):
    state: NavigationState = Field(default=NavigationState.IDLE)
    goal_x: Optional[float] = Field(default=None)
    goal_y: Optional[float] = Field(default=None)
    path_length: float = Field(default=0.0, ge=0.0, description="Remaining path length (m)")
    eta_seconds: float = Field(default=0.0, ge=0.0, description="Estimated time to goal (s)")


class DiagnosticsState(BaseModel):
    cpu_temp: float = Field(default=45.0, ge=0.0, le=120.0, description="CPU temperature (°C)")
    cpu_usage: float = Field(default=0.1, ge=0.0, le=1.0)
    memory_usage: float = Field(default=0.3, ge=0.0, le=1.0)
    network_latency_ms: float = Field(default=5.0, ge=0.0)


class RobotFaultFlags(BaseModel):
    estop_active: bool = Field(default=False)
    lidar_degraded: bool = Field(default=False)
    motor_fault: bool = Field(default=False)
    battery_critical: bool = Field(default=False)
    imu_failure: bool = Field(default=False)
    network_loss: bool = Field(default=False)

    @property
    def any_fault(self) -> bool:
        return any([
            self.estop_active,
            self.lidar_degraded,
            self.motor_fault,
            self.battery_critical,
            self.imu_failure,
            self.network_loss,
        ])


# ---------------------------------------------------------------------------
# Top-level telemetry packet — matches the shared JSON schema
# ---------------------------------------------------------------------------


class TelemetryPacket(BaseModel):
    robot_id: str = Field(..., min_length=1, max_length=64)
    timestamp: float = Field(default_factory=time.time)
    position: Position
    velocity: Velocity = Field(default_factory=Velocity)
    battery: BatteryState
    motors: MotorState = Field(default_factory=MotorState)
    sensors: SensorState = Field(default_factory=SensorState)
    navigation: NavigationStatus = Field(default_factory=NavigationStatus)
    diagnostics: DiagnosticsState = Field(default_factory=DiagnosticsState)
    fault_flags: RobotFaultFlags = Field(default_factory=RobotFaultFlags)
    mode: RobotMode = Field(default=RobotMode.AUTONOMOUS)

    model_config = {"use_enum_values": True}

    @model_validator(mode="after")
    def validate_consistency(self) -> "TelemetryPacket":
        # If e-stop is active, linear velocity must be zero
        if self.fault_flags.estop_active and abs(self.velocity.linear) > 1e-3:
            self.velocity = Velocity(linear=0.0, angular=0.0)
        return self

    def to_redis_dict(self) -> dict[str, str]:
        """Serialize to flat dict of strings suitable for Redis XADD."""
        import json
        return {"data": self.model_dump_json(), "robot_id": self.robot_id}


# ---------------------------------------------------------------------------
# Robot state (internal simulator bookkeeping)
# ---------------------------------------------------------------------------


class RobotState(BaseModel):
    robot_id: str
    sim_state: RobotSimState = Field(default=RobotSimState.IDLE)
    position: Position = Field(default_factory=lambda: Position(x=0.0, y=0.0, theta=0.0))
    velocity: Velocity = Field(default_factory=Velocity)
    battery_level: float = Field(default=1.0, ge=0.0, le=1.0)
    current_waypoints: list[tuple[float, float]] = Field(default_factory=list)
    waypoint_index: int = Field(default=0, ge=0)
    active_faults: set[FaultType] = Field(default_factory=set)
    mission_type: Optional[MissionType] = None
    odometer_m: float = Field(default=0.0, ge=0.0)
    uptime_seconds: float = Field(default=0.0, ge=0.0)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def current_goal(self) -> Optional[tuple[float, float]]:
        if self.waypoint_index < len(self.current_waypoints):
            return self.current_waypoints[self.waypoint_index]
        return None


# ---------------------------------------------------------------------------
# Warehouse map
# ---------------------------------------------------------------------------


class WarehouseMap(BaseModel):
    name: str
    grid: Any  # numpy ndarray — validated separately
    cell_size_m: float = Field(default=0.5, gt=0.0)
    rows: int = Field(..., gt=0)
    cols: int = Field(..., gt=0)
    origin_x: float = Field(default=0.0)
    origin_y: float = Field(default=0.0)
    named_waypoints: dict[str, tuple[float, float]] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def validate_grid_shape(self) -> "WarehouseMap":
        import numpy as np
        if not isinstance(self.grid, np.ndarray):
            raise ValueError("grid must be a numpy ndarray")
        if self.grid.shape != (self.rows, self.cols):
            raise ValueError(
                f"Grid shape {self.grid.shape} does not match rows={self.rows}, cols={self.cols}"
            )
        return self

    def world_to_grid(self, x: float, y: float) -> tuple[int, int]:
        """Convert world (m) coordinates to grid (row, col) indices."""
        col = int((x - self.origin_x) / self.cell_size_m)
        row = int((y - self.origin_y) / self.cell_size_m)
        row = max(0, min(self.rows - 1, row))
        col = max(0, min(self.cols - 1, col))
        return row, col

    def grid_to_world(self, row: int, col: int) -> tuple[float, float]:
        """Convert grid (row, col) indices to world (m) coordinates (cell centre)."""
        x = self.origin_x + (col + 0.5) * self.cell_size_m
        y = self.origin_y + (row + 0.5) * self.cell_size_m
        return x, y

    def is_obstacle(self, row: int, col: int) -> bool:
        return int(self.grid[row, col]) in (CellType.WALL, CellType.SHELF)


# ---------------------------------------------------------------------------
# Navigation goal
# ---------------------------------------------------------------------------


class NavigationGoal(BaseModel):
    robot_id: str
    goal_x: float
    goal_y: float
    goal_theta: Optional[float] = None
    priority: int = Field(default=5, ge=1, le=10, description="1=highest, 10=lowest")
    timeout_seconds: float = Field(default=120.0, gt=0.0)


# ---------------------------------------------------------------------------
# Simulation configuration
# ---------------------------------------------------------------------------


class SimulationConfig(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=64)
    map_name: str = Field(default="small_warehouse")
    num_robots: int = Field(default=3, ge=1, le=20)
    robot_ids: list[str] = Field(default_factory=list)
    telemetry_rate_hz: float = Field(default=10.0, gt=0.0, le=100.0)
    fault_injection_enabled: bool = Field(default=False)
    fault_probability_per_minute: float = Field(default=0.05, ge=0.0, le=1.0)
    mission_type: MissionType = Field(default=MissionType.PATROL_ROUTE)
    duration_seconds: Optional[float] = Field(default=None, gt=0.0)
    seed: Optional[int] = Field(default=None)

    model_config = {"use_enum_values": True}

    @model_validator(mode="after")
    def auto_generate_robot_ids(self) -> "SimulationConfig":
        if not self.robot_ids:
            self.robot_ids = [f"amr-{i+1:03d}" for i in range(self.num_robots)]
        else:
            self.num_robots = len(self.robot_ids)
        return self


# ---------------------------------------------------------------------------
# Mission model
# ---------------------------------------------------------------------------


class Mission(BaseModel):
    mission_id: str
    robot_id: str
    mission_type: MissionType
    waypoints: list[tuple[float, float]] = Field(default_factory=list)
    priority: int = Field(default=5, ge=1, le=10)
    assigned_at: float = Field(default_factory=time.time)
    completed: bool = Field(default=False)
    failed: bool = Field(default=False)

    model_config = {"arbitrary_types_allowed": True}
