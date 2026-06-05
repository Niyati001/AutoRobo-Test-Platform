"""
FaultInjectionEngine — core chaos-engineering logic.

Responsibilities:
  * Inject faults into robots via Redis pub/sub and Redis Streams
  * Auto-resolve faults after their configured duration
  * Track fault history in PostgreSQL
  * Expose Prometheus metrics
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import structlog
from prometheus_client import Counter, Gauge, Histogram
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import (
    FaultCampaign,
    FaultCampaignStep,
    FaultConfig,
    FaultEvent,
    FaultSchedule,
    FaultSeverity,
    FaultStatus,
    FaultType,
    CampaignStatus,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
faults_injected_total = Counter(
    "faults_injected_total",
    "Total number of faults injected",
    ["fault_type", "severity", "robot_id"],
)
active_faults_gauge = Gauge(
    "active_faults_gauge",
    "Number of currently active faults",
)
fault_resolution_seconds = Histogram(
    "fault_resolution_seconds",
    "Time in seconds a fault remained active before resolution",
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
)
campaign_executions_total = Counter(
    "campaign_executions_total",
    "Total campaign executions",
    ["campaign_name", "status"],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TELEMETRY_STREAM = "telemetry:stream"
FAULTS_STREAM = "faults:stream"
ROBOT_COMMANDS_PREFIX = "robot:commands:"

_FAULT_TYPES_FOR_RANDOM: List[FaultType] = [
    FaultType.LIDAR_DEGRADATION,
    FaultType.MOTOR_FAULT,
    FaultType.BATTERY_DRAIN,
    FaultType.NETWORK_PACKET_LOSS,
    FaultType.SENSOR_NOISE,
    FaultType.NAVIGATION_BLOCKAGE,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _build_command(fault_event: FaultEvent) -> str:
    return json.dumps({
        "type": "INJECT_FAULT",
        "fault_id": fault_event.fault_id,
        "fault_type": fault_event.fault_type.value,
        "severity": fault_event.severity.value,
        "parameters": fault_event.parameters,
        "duration_seconds": fault_event.parameters.get("duration_seconds", 30),
        "issued_at": _now_ts(),
    })


def _build_resolve_command(fault_id: str, robot_id: str) -> str:
    return json.dumps({
        "type": "RESOLVE_FAULT",
        "fault_id": fault_id,
        "robot_id": robot_id,
        "resolved_at": _now_ts(),
    })


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------
class FaultInjectionEngine:
    """
    Manages the full lifecycle of fault injection:
      - Inject → publish command + stream event
      - Track active faults
      - Auto-resolve after duration
      - Persist to PostgreSQL
    """

    def __init__(self, redis: Redis, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._redis = redis
        self._session_factory = session_factory

        # fault_id → FaultEvent
        self._active_faults: Dict[str, FaultEvent] = {}
        # fault_id → asyncio.Task (auto-resolve timer)
        self._resolve_tasks: Dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # schedule_id → asyncio.Task
        self._schedule_tasks: Dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # campaign_id → CampaignStatus
        self._running_campaigns: Dict[str, CampaignStatus] = {}
        # Set of packet-loss robot IDs and their drop counters
        self._packet_loss_counters: Dict[str, int] = {}

        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def inject_fault(
        self,
        config: FaultConfig,
        campaign_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
    ) -> FaultEvent:
        """Inject a fault according to *config*. Returns the created FaultEvent."""

        # Resolve RANDOM_FAULT to a concrete type
        fault_type = config.fault_type
        if fault_type == FaultType.RANDOM_FAULT:
            fault_type = random.choice(_FAULT_TYPES_FOR_RANDOM)
            logger.info("random_fault_resolved", resolved_to=fault_type.value, robot_id=config.robot_id)

        fault_event = FaultEvent(
            fault_id=str(uuid.uuid4()),
            robot_id=config.robot_id,
            fault_type=fault_type,
            severity=config.severity,
            parameters={**config.parameters, "duration_seconds": config.duration_seconds},
            injected_at=_now_ts(),
            status=FaultStatus.ACTIVE,
            campaign_id=campaign_id,
            schedule_id=schedule_id,
        )

        async with self._lock:
            await self._do_inject(fault_event)

            # Handle CASCADING_FAILURE — inject into additional targets
            if fault_type == FaultType.CASCADING_FAILURE and config.cascade_targets:
                asyncio.create_task(
                    self._inject_cascade(config, fault_event.fault_id)
                )

        return fault_event

    async def resolve_fault(self, fault_id: str, reason: Optional[str] = None) -> Optional[FaultEvent]:
        """Manually resolve an active fault."""
        async with self._lock:
            fault_event = self._active_faults.get(fault_id)
            if fault_event is None:
                return None
            await self._do_resolve(fault_event, reason=reason)
            return fault_event

    async def cancel_resolve_task(self, fault_id: str) -> None:
        task = self._resolve_tasks.pop(fault_id, None)
        if task and not task.done():
            task.cancel()

    async def start_schedule(self, schedule: FaultSchedule) -> None:
        """Start background task that fires faults according to cron schedule."""
        if schedule.schedule_id in self._schedule_tasks:
            logger.warning("schedule_already_running", schedule_id=schedule.schedule_id)
            return

        task = asyncio.create_task(
            self._run_schedule(schedule),
            name=f"schedule-{schedule.schedule_id}",
        )
        self._schedule_tasks[schedule.schedule_id] = task
        logger.info("schedule_started", schedule_id=schedule.schedule_id, cron=schedule.cron_expression)

    async def stop_schedule(self, schedule_id: str) -> None:
        task = self._schedule_tasks.pop(schedule_id, None)
        if task and not task.done():
            task.cancel()
            logger.info("schedule_stopped", schedule_id=schedule_id)

    async def run_campaign(self, campaign: FaultCampaign) -> CampaignStatus:
        """Execute a fault campaign (non-blocking — runs as a background task)."""
        status = CampaignStatus(
            campaign_id=campaign.campaign_id,
            name=campaign.name,
            status="RUNNING",
            total_steps=len(campaign.steps),
            total_repeats=campaign.repeat_count,
            started_at=datetime.utcnow(),
        )
        self._running_campaigns[campaign.campaign_id] = status
        asyncio.create_task(
            self._execute_campaign(campaign, status),
            name=f"campaign-{campaign.campaign_id}",
        )
        return status

    def get_campaign_status(self, campaign_id: str) -> Optional[CampaignStatus]:
        return self._running_campaigns.get(campaign_id)

    def list_active_faults(self) -> List[FaultEvent]:
        return list(self._active_faults.values())

    # ------------------------------------------------------------------
    # Fault-type specific injection helpers
    # ------------------------------------------------------------------

    async def _do_inject(self, fault_event: FaultEvent) -> None:
        """Core: publish commands, write stream, update in-memory state."""
        robot_id = fault_event.robot_id
        fault_type = fault_event.fault_type

        logger.info(
            "injecting_fault",
            fault_id=fault_event.fault_id,
            robot_id=robot_id,
            fault_type=fault_type.value,
            severity=fault_event.severity.value,
        )

        # Type-specific pre-injection logic
        if fault_type == FaultType.ESTOP:
            await self._inject_estop(fault_event)
        elif fault_type == FaultType.LIDAR_DEGRADATION:
            await self._inject_lidar_degradation(fault_event)
        elif fault_type == FaultType.MOTOR_FAULT:
            await self._inject_motor_fault(fault_event)
        elif fault_type == FaultType.BATTERY_DRAIN:
            await self._inject_battery_drain(fault_event)
        elif fault_type == FaultType.NETWORK_PACKET_LOSS:
            self._packet_loss_counters[robot_id] = 0
            await self._inject_generic(fault_event)
        elif fault_type == FaultType.SENSOR_NOISE:
            await self._inject_sensor_noise(fault_event)
        elif fault_type == FaultType.NAVIGATION_BLOCKAGE:
            await self._inject_nav_blockage(fault_event)
        else:
            await self._inject_generic(fault_event)

        # Publish to faults:stream
        await self._publish_fault_stream_event(fault_event)

        # Persist to DB
        await self._persist_fault(fault_event)

        # Track in memory
        self._active_faults[fault_event.fault_id] = fault_event
        active_faults_gauge.inc()

        # Prometheus counter
        faults_injected_total.labels(
            fault_type=fault_type.value,
            severity=fault_event.severity.value,
            robot_id=robot_id,
        ).inc()

        # Schedule auto-resolution
        duration = float(fault_event.parameters.get("duration_seconds", 30))
        task = asyncio.create_task(
            self._auto_resolve(fault_event.fault_id, duration),
            name=f"resolve-{fault_event.fault_id}",
        )
        self._resolve_tasks[fault_event.fault_id] = task

    async def _do_resolve(self, fault_event: FaultEvent, reason: Optional[str] = None) -> None:
        now = _now_ts()
        duration = now - fault_event.injected_at

        fault_event.status = FaultStatus.RESOLVED
        fault_event.resolved_at = now
        if reason:
            fault_event.error_message = reason

        # Cancel timer if still running
        await self.cancel_resolve_task(fault_event.fault_id)

        # Publish resolve command to robot
        resolve_cmd = _build_resolve_command(fault_event.fault_id, fault_event.robot_id)
        cmd_key = f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}"
        await self._redis.publish(cmd_key, resolve_cmd)

        # Remove packet-loss counter if applicable
        if fault_event.fault_type == FaultType.NETWORK_PACKET_LOSS:
            self._packet_loss_counters.pop(fault_event.robot_id, None)

        # Update stream
        await self._publish_fault_stream_event(fault_event)

        # Update DB
        await self._update_fault_status(fault_event)

        # Remove from active
        self._active_faults.pop(fault_event.fault_id, None)
        active_faults_gauge.dec()

        # Metrics
        fault_resolution_seconds.observe(duration)

        logger.info(
            "fault_resolved",
            fault_id=fault_event.fault_id,
            robot_id=fault_event.robot_id,
            duration_seconds=round(duration, 2),
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Type-specific injectors
    # ------------------------------------------------------------------

    async def _inject_estop(self, fault_event: FaultEvent) -> None:
        cmd = json.dumps({
            "type": "INJECT_FAULT",
            "fault_id": fault_event.fault_id,
            "fault_type": "ESTOP",
            "severity": fault_event.severity.value,
            "parameters": {"immediate": True},
            "issued_at": _now_ts(),
        })
        await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}", cmd)
        logger.warning("estop_injected", robot_id=fault_event.robot_id)

    async def _inject_lidar_degradation(self, fault_event: FaultEvent) -> None:
        params = fault_event.parameters
        degradation_factor = float(params.get("degradation_factor", 0.3))
        ramp_seconds = float(params.get("ramp_seconds", 10.0))
        cmd = json.dumps({
            "type": "INJECT_FAULT",
            "fault_id": fault_event.fault_id,
            "fault_type": "LIDAR_DEGRADATION",
            "severity": fault_event.severity.value,
            "parameters": {
                "degradation_factor": degradation_factor,
                "ramp_seconds": ramp_seconds,
                "target_quality": max(0.0, 1.0 - degradation_factor),
            },
            "issued_at": _now_ts(),
        })
        await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}", cmd)
        # Gradually push degradation updates over ramp period
        asyncio.create_task(
            self._ramp_lidar_degradation(
                fault_event.robot_id,
                fault_event.fault_id,
                degradation_factor,
                ramp_seconds,
            )
        )

    async def _ramp_lidar_degradation(
        self,
        robot_id: str,
        fault_id: str,
        degradation_factor: float,
        ramp_seconds: float,
    ) -> None:
        steps = max(5, int(ramp_seconds))
        interval = ramp_seconds / steps
        for step in range(1, steps + 1):
            await asyncio.sleep(interval)
            if fault_id not in self._active_faults:
                break
            current_quality = max(0.0, 1.0 - (degradation_factor * step / steps))
            update_cmd = json.dumps({
                "type": "UPDATE_SENSOR_PARAM",
                "fault_id": fault_id,
                "parameter": "lidar_quality",
                "value": round(current_quality, 3),
                "issued_at": _now_ts(),
            })
            await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{robot_id}", update_cmd)

    async def _inject_motor_fault(self, fault_event: FaultEvent) -> None:
        cmd = json.dumps({
            "type": "INJECT_FAULT",
            "fault_id": fault_event.fault_id,
            "fault_type": "MOTOR_FAULT",
            "severity": fault_event.severity.value,
            "parameters": {
                "motor_fault": True,
                "affected_motors": fault_event.parameters.get("affected_motors", ["left", "right"]),
                "torque_reduction": fault_event.parameters.get("torque_reduction", 0.5),
            },
            "issued_at": _now_ts(),
        })
        await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}", cmd)

    async def _inject_battery_drain(self, fault_event: FaultEvent) -> None:
        drain_rate = float(fault_event.parameters.get("drain_rate_per_second", 0.005))
        cmd = json.dumps({
            "type": "INJECT_FAULT",
            "fault_id": fault_event.fault_id,
            "fault_type": "BATTERY_DRAIN",
            "severity": fault_event.severity.value,
            "parameters": {
                "drain_rate_per_second": drain_rate,
                "min_battery_level": fault_event.parameters.get("min_battery_level", 0.05),
            },
            "issued_at": _now_ts(),
        })
        await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}", cmd)

    async def _inject_sensor_noise(self, fault_event: FaultEvent) -> None:
        noise_amplitude = float(fault_event.parameters.get("noise_amplitude", 0.05))
        cmd = json.dumps({
            "type": "INJECT_FAULT",
            "fault_id": fault_event.fault_id,
            "fault_type": "SENSOR_NOISE",
            "severity": fault_event.severity.value,
            "parameters": {
                "imu_drift_increase": noise_amplitude,
                "noise_frequency_hz": fault_event.parameters.get("noise_frequency_hz", 10.0),
            },
            "issued_at": _now_ts(),
        })
        await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}", cmd)

    async def _inject_nav_blockage(self, fault_event: FaultEvent) -> None:
        obstacle_x = float(fault_event.parameters.get("obstacle_x", 0.0))
        obstacle_y = float(fault_event.parameters.get("obstacle_y", 0.0))
        obstacle_radius = float(fault_event.parameters.get("obstacle_radius", 1.0))
        cmd = json.dumps({
            "type": "INJECT_FAULT",
            "fault_id": fault_event.fault_id,
            "fault_type": "NAVIGATION_BLOCKAGE",
            "severity": fault_event.severity.value,
            "parameters": {
                "obstacle_x": obstacle_x,
                "obstacle_y": obstacle_y,
                "obstacle_radius": obstacle_radius,
                "dynamic": fault_event.parameters.get("dynamic", False),
            },
            "issued_at": _now_ts(),
        })
        await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}", cmd)

    async def _inject_generic(self, fault_event: FaultEvent) -> None:
        cmd = _build_command(fault_event)
        await self._redis.publish(f"{ROBOT_COMMANDS_PREFIX}{fault_event.robot_id}", cmd)

    # ------------------------------------------------------------------
    # Cascading failure
    # ------------------------------------------------------------------

    async def _inject_cascade(self, config: FaultConfig, parent_fault_id: str) -> None:
        """After a short delay, inject secondary faults into cascade targets."""
        cascade_delay = float(config.parameters.get("cascade_delay_seconds", 5.0))
        secondary_fault_type = FaultType(
            config.parameters.get("secondary_fault_type", FaultType.MOTOR_FAULT.value)
        )
        for idx, target_id in enumerate(config.cascade_targets):
            await asyncio.sleep(cascade_delay * (idx + 1))
            secondary_config = FaultConfig(
                robot_id=target_id,
                fault_type=secondary_fault_type,
                severity=FaultSeverity.HIGH,
                parameters={"parent_fault_id": parent_fault_id},
                duration_seconds=config.duration_seconds,
            )
            try:
                await self.inject_fault(secondary_config)
                logger.info(
                    "cascade_fault_injected",
                    parent_fault_id=parent_fault_id,
                    target_robot=target_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("cascade_injection_failed", target_robot=target_id, error=str(exc))

    # ------------------------------------------------------------------
    # Auto-resolve timer
    # ------------------------------------------------------------------

    async def _auto_resolve(self, fault_id: str, duration: float) -> None:
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            return
        async with self._lock:
            fault_event = self._active_faults.get(fault_id)
            if fault_event is not None:
                await self._do_resolve(fault_event, reason="auto-resolved after duration")

    # ------------------------------------------------------------------
    # Campaign executor
    # ------------------------------------------------------------------

    async def _execute_campaign(self, campaign: FaultCampaign, status: CampaignStatus) -> None:
        try:
            for repeat in range(1, campaign.repeat_count + 1):
                status.current_repeat = repeat
                for step_idx, step in enumerate(campaign.steps):
                    status.current_step = step_idx + 1

                    # Delay before this step
                    if step.delay_before_seconds > 0:
                        await asyncio.sleep(step.delay_before_seconds)

                    # Wait for previous fault to resolve if requested
                    if step.wait_for_resolution and status.injected_fault_ids:
                        last_id = status.injected_fault_ids[-1]
                        while last_id in self._active_faults:
                            await asyncio.sleep(1.0)

                    try:
                        fault_event = await self.inject_fault(
                            step.fault_config, campaign_id=campaign.campaign_id
                        )
                        status.injected_fault_ids.append(fault_event.fault_id)
                        logger.info(
                            "campaign_step_injected",
                            campaign_id=campaign.campaign_id,
                            step=step_idx + 1,
                            fault_id=fault_event.fault_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "campaign_step_failed",
                            campaign_id=campaign.campaign_id,
                            step=step_idx + 1,
                            error=str(exc),
                        )
                        if campaign.abort_on_failure:
                            status.status = "FAILED"
                            status.error_message = str(exc)
                            campaign_executions_total.labels(
                                campaign_name=campaign.name, status="FAILED"
                            ).inc()
                            return

            status.status = "COMPLETED"
            status.ended_at = datetime.utcnow()
            campaign_executions_total.labels(campaign_name=campaign.name, status="COMPLETED").inc()
            logger.info("campaign_completed", campaign_id=campaign.campaign_id)

        except Exception as exc:  # noqa: BLE001
            status.status = "FAILED"
            status.error_message = str(exc)
            status.ended_at = datetime.utcnow()
            campaign_executions_total.labels(campaign_name=campaign.name, status="FAILED").inc()
            logger.exception("campaign_error", campaign_id=campaign.campaign_id)

    # ------------------------------------------------------------------
    # Schedule runner
    # ------------------------------------------------------------------

    async def _run_schedule(self, schedule: FaultSchedule) -> None:
        """Continuously fires faults according to cron expression."""
        try:
            from croniter import croniter  # type: ignore[import]
        except ImportError:
            logger.error("croniter_not_installed")
            return

        cron = croniter(schedule.cron_expression, datetime.utcnow())
        while schedule.enabled:
            if schedule.max_executions and schedule.execution_count >= schedule.max_executions:
                logger.info("schedule_max_executions_reached", schedule_id=schedule.schedule_id)
                break

            next_time: datetime = cron.get_next(datetime)
            now = datetime.utcnow()
            wait_secs = (next_time - now).total_seconds()
            if wait_secs > 0:
                try:
                    await asyncio.sleep(wait_secs)
                except asyncio.CancelledError:
                    logger.info("schedule_cancelled", schedule_id=schedule.schedule_id)
                    return

            try:
                await self.inject_fault(schedule.fault_config, schedule_id=schedule.schedule_id)
                schedule.execution_count += 1
                schedule.last_executed_at = datetime.utcnow()
                logger.info(
                    "scheduled_fault_injected",
                    schedule_id=schedule.schedule_id,
                    execution_count=schedule.execution_count,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduled_fault_failed", schedule_id=schedule.schedule_id, error=str(exc))

    # ------------------------------------------------------------------
    # Redis stream helpers
    # ------------------------------------------------------------------

    async def _publish_fault_stream_event(self, fault_event: FaultEvent) -> None:
        try:
            payload = {
                "fault_id": fault_event.fault_id,
                "robot_id": fault_event.robot_id,
                "fault_type": fault_event.fault_type.value,
                "severity": fault_event.severity.value,
                "parameters": json.dumps(fault_event.parameters),
                "injected_at": str(fault_event.injected_at),
                "resolved_at": str(fault_event.resolved_at) if fault_event.resolved_at else "",
                "status": fault_event.status.value,
            }
            await self._redis.xadd(FAULTS_STREAM, payload, maxlen=10000)
        except Exception as exc:  # noqa: BLE001
            logger.error("fault_stream_publish_failed", fault_id=fault_event.fault_id, error=str(exc))

    # ------------------------------------------------------------------
    # PostgreSQL helpers
    # ------------------------------------------------------------------

    async def _persist_fault(self, fault_event: FaultEvent) -> None:
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text("""
                        INSERT INTO faults
                            (id, fault_id, robot_id, fault_type, severity, parameters,
                             injected_at, resolved_at, status)
                        VALUES
                            (:id, :fault_id, :robot_id, :fault_type, :severity, :parameters::jsonb,
                             to_timestamp(:injected_at), NULL, :status)
                        ON CONFLICT (fault_id) DO NOTHING
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "fault_id": fault_event.fault_id,
                        "robot_id": fault_event.robot_id,
                        "fault_type": fault_event.fault_type.value,
                        "severity": fault_event.severity.value,
                        "parameters": json.dumps(fault_event.parameters),
                        "injected_at": fault_event.injected_at,
                        "status": fault_event.status.value,
                    },
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("fault_persist_failed", fault_id=fault_event.fault_id, error=str(exc))

    async def _update_fault_status(self, fault_event: FaultEvent) -> None:
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text("""
                        UPDATE faults
                        SET status = :status,
                            resolved_at = CASE WHEN :resolved_at IS NOT NULL
                                               THEN to_timestamp(:resolved_at)
                                               ELSE NULL END
                        WHERE fault_id = :fault_id
                    """),
                    {
                        "fault_id": fault_event.fault_id,
                        "status": fault_event.status.value,
                        "resolved_at": fault_event.resolved_at,
                    },
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("fault_update_failed", fault_id=fault_event.fault_id, error=str(exc))

    async def load_faults_from_db(self, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        """Query fault history from PostgreSQL for list endpoint."""
        offset = (page - 1) * page_size
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    text("""
                        SELECT fault_id, robot_id, fault_type, severity, parameters,
                               EXTRACT(EPOCH FROM injected_at) AS injected_at,
                               EXTRACT(EPOCH FROM resolved_at) AS resolved_at,
                               status
                        FROM faults
                        ORDER BY injected_at DESC
                        LIMIT :limit OFFSET :offset
                    """),
                    {"limit": page_size, "offset": offset},
                )
                rows = result.mappings().all()

                count_result = await session.execute(text("SELECT COUNT(*) FROM faults"))
                total = count_result.scalar_one()

                faults = []
                for row in rows:
                    faults.append(FaultEvent(
                        fault_id=row["fault_id"],
                        robot_id=row["robot_id"],
                        fault_type=FaultType(row["fault_type"]),
                        severity=FaultSeverity(row["severity"]),
                        parameters=json.loads(row["parameters"]) if isinstance(row["parameters"], str) else dict(row["parameters"]),
                        injected_at=float(row["injected_at"]) if row["injected_at"] else 0.0,
                        resolved_at=float(row["resolved_at"]) if row["resolved_at"] else None,
                        status=FaultStatus(row["status"]),
                    ))
                return {"items": faults, "total": total}
        except Exception as exc:  # noqa: BLE001
            logger.error("fault_load_failed", error=str(exc))
            return {"items": [], "total": 0}

    async def get_fault_from_db(self, fault_id: str) -> Optional[FaultEvent]:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    text("""
                        SELECT fault_id, robot_id, fault_type, severity, parameters,
                               EXTRACT(EPOCH FROM injected_at) AS injected_at,
                               EXTRACT(EPOCH FROM resolved_at) AS resolved_at,
                               status
                        FROM faults WHERE fault_id = :fault_id
                    """),
                    {"fault_id": fault_id},
                )
                row = result.mappings().first()
                if row is None:
                    return None
                return FaultEvent(
                    fault_id=row["fault_id"],
                    robot_id=row["robot_id"],
                    fault_type=FaultType(row["fault_type"]),
                    severity=FaultSeverity(row["severity"]),
                    parameters=json.loads(row["parameters"]) if isinstance(row["parameters"], str) else dict(row["parameters"]),
                    injected_at=float(row["injected_at"]) if row["injected_at"] else 0.0,
                    resolved_at=float(row["resolved_at"]) if row["resolved_at"] else None,
                    status=FaultStatus(row["status"]),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("fault_get_failed", fault_id=fault_id, error=str(exc))
            return None
