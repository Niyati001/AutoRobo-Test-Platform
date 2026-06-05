"""
Pydantic v2 models for the Fault Injection Service.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class FaultType(str, Enum):
    ESTOP = "ESTOP"
    LIDAR_DEGRADATION = "LIDAR_DEGRADATION"
    MOTOR_FAULT = "MOTOR_FAULT"
    BATTERY_DRAIN = "BATTERY_DRAIN"
    NETWORK_PACKET_LOSS = "NETWORK_PACKET_LOSS"
    SENSOR_NOISE = "SENSOR_NOISE"
    NAVIGATION_BLOCKAGE = "NAVIGATION_BLOCKAGE"
    CASCADING_FAILURE = "CASCADING_FAILURE"
    RANDOM_FAULT = "RANDOM_FAULT"


class FaultSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class FaultStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class FaultConfig(BaseModel):
    """Configuration for a single fault injection."""

    robot_id: str = Field(..., min_length=1, max_length=64, description="Target robot identifier")
    fault_type: FaultType = Field(..., description="Type of fault to inject")
    severity: FaultSeverity = Field(default=FaultSeverity.MEDIUM, description="Fault severity level")
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Fault-type-specific parameters",
    )
    duration_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=3600.0,
        description="How long the fault remains active (seconds)",
    )
    scheduled_at: Optional[datetime] = Field(
        default=None,
        description="When to execute this fault (None = immediate)",
    )
    cascade_targets: List[str] = Field(
        default_factory=list,
        description="Additional robot IDs targeted in a cascading failure",
    )

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        # Ensure all values are JSON-serialisable primitives
        for key, val in v.items():
            if not isinstance(val, (str, int, float, bool, type(None), list, dict)):
                raise ValueError(f"Parameter '{key}' has non-serialisable type {type(val)}")
        return v

    model_config = {"json_schema_extra": {"example": {
        "robot_id": "amr-001",
        "fault_type": "LIDAR_DEGRADATION",
        "severity": "HIGH",
        "parameters": {"degradation_factor": 0.4, "ramp_seconds": 10},
        "duration_seconds": 60,
    }}}


class FaultEvent(BaseModel):
    """A fault event as stored in the faults:stream Redis stream and PostgreSQL."""

    fault_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    robot_id: str
    fault_type: FaultType
    severity: FaultSeverity
    parameters: Dict[str, Any] = Field(default_factory=dict)
    injected_at: float = Field(default_factory=lambda: datetime.utcnow().timestamp())
    resolved_at: Optional[float] = None
    status: FaultStatus = FaultStatus.PENDING
    error_message: Optional[str] = None
    campaign_id: Optional[str] = None
    schedule_id: Optional[str] = None

    model_config = {"from_attributes": True}


class FaultSchedule(BaseModel):
    """Schedule a fault using a cron expression."""

    schedule_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    cron_expression: str = Field(
        ...,
        description="Standard 5-field cron expression (minute hour dom month dow)",
    )
    fault_config: FaultConfig
    enabled: bool = Field(default=True)
    max_executions: Optional[int] = Field(
        default=None,
        ge=1,
        description="Stop after this many executions (None = infinite)",
    )
    execution_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_executed_at: Optional[datetime] = None
    next_execution_at: Optional[datetime] = None

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        parts = v.strip().split()
        if len(parts) not in (5, 6):
            raise ValueError("Cron expression must have 5 or 6 fields")
        return v.strip()


class FaultCampaignStep(BaseModel):
    """One step in a fault campaign."""

    fault_config: FaultConfig
    delay_before_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Seconds to wait before injecting this fault",
    )
    wait_for_resolution: bool = Field(
        default=False,
        description="If True, wait for previous fault to resolve before starting this one",
    )


class FaultCampaign(BaseModel):
    """An ordered sequence of faults executed as a test campaign."""

    campaign_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    steps: List[FaultCampaignStep] = Field(..., min_length=1)
    repeat_count: int = Field(default=1, ge=1, le=100)
    abort_on_failure: bool = Field(default=False)
    tags: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def check_steps_not_empty(self) -> "FaultCampaign":
        if not self.steps:
            raise ValueError("Campaign must have at least one step")
        return self


class CampaignStatus(BaseModel):
    """Runtime status of a running campaign."""

    campaign_id: str
    name: str
    status: str  # RUNNING, COMPLETED, FAILED, CANCELLED
    current_step: int = 0
    total_steps: int
    current_repeat: int = 1
    total_repeats: int
    started_at: datetime
    ended_at: Optional[datetime] = None
    injected_fault_ids: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None


class FaultTemplate(BaseModel):
    """A pre-built named fault scenario."""

    template_name: str
    display_name: str
    description: str
    category: str  # e.g., "warehouse", "battery", "sensor"
    campaign: FaultCampaign


class FaultListResponse(BaseModel):
    """Paginated list of fault events."""

    items: List[FaultEvent]
    total: int
    page: int
    page_size: int
    has_more: bool


class FaultResolveRequest(BaseModel):
    """Request body for manually resolving / cancelling a fault."""

    reason: Optional[str] = Field(default=None, max_length=512)
