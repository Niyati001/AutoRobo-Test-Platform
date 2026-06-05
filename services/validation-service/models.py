"""
Pydantic v2 models for the Validation Service.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ValidationTestType(str, Enum):
    NAVIGATION_STABILITY = "NAVIGATION_STABILITY"
    REROUTING = "REROUTING"
    COLLISION_PREVENTION = "COLLISION_PREVENTION"
    TELEMETRY_INTEGRITY = "TELEMETRY_INTEGRITY"
    RECOVERY_BEHAVIOR = "RECOVERY_BEHAVIOR"
    BATTERY_ENDURANCE = "BATTERY_ENDURANCE"
    FLEET_COORDINATION = "FLEET_COORDINATION"
    LOAD_STRESS = "LOAD_STRESS"


class ValidationRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ValidationConfig(BaseModel):
    """Configuration for a validation run."""

    robot_id: Optional[str] = Field(
        default=None,
        description="Single robot to validate (mutually exclusive with fleet_ids)",
    )
    fleet_ids: List[str] = Field(
        default_factory=list,
        description="List of robot IDs for fleet-level tests",
    )
    test_suite: List[ValidationTestType] = Field(
        default_factory=lambda: list(ValidationTestType),
        description="Which tests to run (empty = all tests)",
    )
    pass_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Minimum score (0-1) to consider a test passed",
    )
    timeout_seconds: float = Field(
        default=300.0,
        ge=10.0,
        le=7200.0,
        description="Maximum wall-clock time for the entire run",
    )
    telemetry_window_seconds: float = Field(
        default=60.0,
        ge=5.0,
        description="How many seconds of telemetry history to analyse",
    )
    baseline_run_id: Optional[str] = Field(
        default=None,
        description="Compare results against this baseline run",
    )
    tags: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_target(self) -> "ValidationConfig":
        if not self.robot_id and not self.fleet_ids:
            raise ValueError("Either robot_id or fleet_ids must be specified")
        return self

    @field_validator("test_suite", mode="before")
    @classmethod
    def default_all_tests(cls, v: Any) -> Any:
        if not v:
            return list(ValidationTestType)
        return v


class TestResult(BaseModel):
    """Result of a single validation test."""

    test_name: ValidationTestType
    passed: bool
    score: float = Field(ge=0.0, le=1.0, description="Normalised score 0-1")
    details: Dict[str, Any] = Field(default_factory=dict, description="Test-specific metrics")
    duration_seconds: float = Field(ge=0.0)
    error_message: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ValidationRun(BaseModel):
    """A completed or in-progress validation run."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    robot_id: Optional[str] = None
    fleet_ids: List[str] = Field(default_factory=list)
    test_suite: List[ValidationTestType]
    config: ValidationConfig
    status: ValidationRunStatus = ValidationRunStatus.PENDING
    results: List[TestResult] = Field(default_factory=list)
    pass_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    overall_passed: bool = False
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    baseline_run_id: Optional[str] = None
    regression_detected: bool = False
    regression_details: Optional[Dict[str, Any]] = None

    model_config = {"from_attributes": True}

    def compute_pass_rate(self) -> None:
        if not self.results:
            self.pass_rate = 0.0
            self.overall_passed = False
            return
        passed_count = sum(1 for r in self.results if r.passed)
        self.pass_rate = passed_count / len(self.results)
        self.overall_passed = self.pass_rate >= self.config.pass_threshold


class ValidationReport(BaseModel):
    """Full detailed report for a validation run."""

    run_id: str
    robot_id: Optional[str]
    fleet_ids: List[str]
    status: ValidationRunStatus
    overall_passed: bool
    pass_rate: float
    pass_threshold: float
    total_tests: int
    passed_tests: int
    failed_tests: int
    results: List[TestResult]
    started_at: datetime
    ended_at: Optional[datetime]
    duration_seconds: Optional[float]
    regression_detected: bool
    regression_details: Optional[Dict[str, Any]]
    baseline_run_id: Optional[str]
    summary: str
    recommendations: List[str]
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class ValidationBaseline(BaseModel):
    """A stored baseline against which future runs are compared."""

    baseline_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    robot_id: Optional[str]
    fleet_ids: List[str] = Field(default_factory=list)
    pass_rate: float
    test_scores: Dict[str, float] = Field(default_factory=dict, description="test_name → score")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class ComparisonResult(BaseModel):
    """Result of comparing two validation runs."""

    run_id: str
    compared_to_run_id: str
    pass_rate_delta: float
    regression_detected: bool
    regression_threshold: float
    per_test_deltas: Dict[str, float] = Field(
        default_factory=dict, description="test_name → score_delta"
    )
    summary: str


class ValidationListResponse(BaseModel):
    """Paginated list of validation runs."""

    items: List[ValidationRun]
    total: int
    page: int
    page_size: int
    has_more: bool


class BaselineSetRequest(BaseModel):
    """Request to promote a run to baseline."""

    run_id: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
