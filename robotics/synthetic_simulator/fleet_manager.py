"""
FleetManager: orchestrates multiple AsyncRobotSimulator instances.

Responsibilities:
- Lifecycle management of up to 20 robots
- Mission assignment with priority-based deadlock prevention
- Telemetry publication via TelemetryPublisher
- SimPy discrete-event scheduling for mission queuing
- asyncio orchestration
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import simpy
import structlog

from .models import (
    FaultType,
    Mission,
    MissionType,
    NavigationGoal,
    RobotSimState,
    SimulationConfig,
)
from .robot_simulator import AsyncRobotSimulator
from .telemetry_publisher import TelemetryPublisher
from .warehouse_physics import WarehousePhysics

logger = structlog.get_logger(__name__)

MAX_ROBOTS = 20
MISSION_TIMEOUT_S = 300.0  # 5 minutes per mission


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class RobotEntry:
    robot_id: str
    simulator: AsyncRobotSimulator
    task: Optional[asyncio.Task] = None
    current_mission: Optional[Mission] = None
    completed_missions: int = 0
    failed_missions: int = 0
    added_at: float = field(default_factory=time.time)


@dataclass
class MissionQueueItem:
    priority: int
    enqueued_at: float
    mission: Mission

    def __lt__(self, other: "MissionQueueItem") -> bool:
        # Lower priority number = higher priority; break ties by enqueue time
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.enqueued_at < other.enqueued_at


# ---------------------------------------------------------------------------
# FleetManager
# ---------------------------------------------------------------------------


class FleetManager:
    """
    Manages a fleet of simulated AMR robots.

    Usage::

        physics = WarehousePhysics(warehouse_map)
        publisher = TelemetryPublisher(redis_url)
        fleet = FleetManager(physics, publisher)
        await fleet.start()
        await fleet.add_robot("amr-001")
        await fleet.assign_mission("amr-001", mission)
        await fleet.stop()
    """

    def __init__(
        self,
        physics: WarehousePhysics,
        publisher: TelemetryPublisher,
        config: Optional[SimulationConfig] = None,
    ) -> None:
        self._physics = physics
        self._publisher = publisher
        self._config = config

        self._robots: dict[str, RobotEntry] = {}
        self._mission_queue: asyncio.PriorityQueue[MissionQueueItem] = asyncio.PriorityQueue()
        self._pending_missions: dict[str, Mission] = {}  # mission_id → Mission

        # SimPy environment for discrete-event scheduling
        self._simpy_env: Optional[simpy.Environment] = None
        self._simpy_task: Optional[asyncio.Task] = None

        # Fleet-level tasks
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._running = False

        # Deadlock prevention: tracks which robots hold which "zones"
        # (simple priority-based: higher-priority robot wins the zone)
        self._zone_locks: dict[str, str] = {}  # zone_id → robot_id

        self._log = logger.bind(component="fleet_manager")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the fleet manager and background tasks."""
        if self._running:
            return
        self._running = True

        await self._publisher.connect()

        # Start SimPy RT environment in background
        self._simpy_env = simpy.Environment()
        self._simpy_task = asyncio.create_task(
            self._run_simpy_loop(), name="simpy_loop"
        )

        # Start mission dispatcher
        self._dispatcher_task = asyncio.create_task(
            self._mission_dispatcher(), name="mission_dispatcher"
        )

        self._log.info("fleet_manager_started")

    async def stop(self) -> None:
        """Gracefully stop all robots and background tasks."""
        self._running = False

        # Cancel dispatcher
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass

        # Cancel SimPy loop
        if self._simpy_task and not self._simpy_task.done():
            self._simpy_task.cancel()
            try:
                await self._simpy_task
            except asyncio.CancelledError:
                pass

        # Stop all robots
        stop_tasks = []
        for entry in list(self._robots.values()):
            entry.simulator.stop()
            if entry.task and not entry.task.done():
                entry.task.cancel()
                stop_tasks.append(entry.task)

        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)

        await self._publisher.close()
        self._log.info("fleet_manager_stopped", robots_stopped=len(self._robots))

    # ------------------------------------------------------------------
    # Robot management
    # ------------------------------------------------------------------

    async def add_robot(
        self,
        robot_id: str,
        initial_position: Optional[tuple[float, float]] = None,
        initial_battery: float = 1.0,
        seed: Optional[int] = None,
    ) -> bool:
        """
        Add a robot to the fleet.  Returns True on success.
        """
        if len(self._robots) >= MAX_ROBOTS:
            self._log.warning("fleet_at_capacity", max_robots=MAX_ROBOTS)
            return False

        if robot_id in self._robots:
            self._log.warning("robot_already_exists", robot_id=robot_id)
            return False

        simulator = AsyncRobotSimulator(
            robot_id=robot_id,
            physics=self._physics,
            rate_hz=self._config.telemetry_rate_hz if self._config else 10.0,
            initial_battery=initial_battery,
            seed=seed,
        )

        # Set initial position if provided
        if initial_position:
            simulator._pos.x = initial_position[0]
            simulator._pos.y = initial_position[1]

        entry = RobotEntry(robot_id=robot_id, simulator=simulator)

        # Start the robot's telemetry publishing loop
        task = asyncio.create_task(
            self._robot_telemetry_loop(entry),
            name=f"robot_{robot_id}",
        )
        entry.task = task
        self._robots[robot_id] = entry

        self._log.info("robot_added", robot_id=robot_id, fleet_size=len(self._robots))
        return True

    async def remove_robot(self, robot_id: str) -> bool:
        """Remove a robot from the fleet."""
        entry = self._robots.get(robot_id)
        if not entry:
            return False

        entry.simulator.stop()
        if entry.task and not entry.task.done():
            entry.task.cancel()
            try:
                await entry.task
            except (asyncio.CancelledError, Exception):
                pass

        del self._robots[robot_id]
        self._log.info("robot_removed", robot_id=robot_id)
        return True

    # ------------------------------------------------------------------
    # Mission assignment
    # ------------------------------------------------------------------

    async def assign_mission(self, robot_id: str, mission: Mission) -> bool:
        """
        Assign a mission to a robot.  Enqueues the mission and the dispatcher
        will assign it when the robot is available.
        """
        if robot_id not in self._robots:
            self._log.warning("assign_mission_unknown_robot", robot_id=robot_id)
            return False

        mission = mission.model_copy(update={"robot_id": robot_id})
        self._pending_missions[mission.mission_id] = mission

        item = MissionQueueItem(
            priority=mission.priority,
            enqueued_at=mission.assigned_at,
            mission=mission,
        )
        await self._mission_queue.put(item)
        self._log.info(
            "mission_enqueued",
            mission_id=mission.mission_id,
            robot_id=robot_id,
            type=mission.mission_type,
            priority=mission.priority,
        )
        return True

    async def assign_patrol_route(
        self,
        robot_id: str,
        waypoints: list[tuple[float, float]],
        priority: int = 5,
    ) -> Optional[str]:
        """Convenience method: create and enqueue a patrol mission."""
        mission = Mission(
            mission_id=str(uuid.uuid4()),
            robot_id=robot_id,
            mission_type=MissionType.PATROL_ROUTE,
            waypoints=waypoints,
            priority=priority,
        )
        success = await self.assign_mission(robot_id, mission)
        return mission.mission_id if success else None

    async def assign_move_to_goal(
        self,
        robot_id: str,
        goal_x: float,
        goal_y: float,
        priority: int = 5,
    ) -> Optional[str]:
        """Convenience method: create and enqueue a move-to-goal mission."""
        mission = Mission(
            mission_id=str(uuid.uuid4()),
            robot_id=robot_id,
            mission_type=MissionType.MOVE_TO_GOAL,
            waypoints=[(goal_x, goal_y)],
            priority=priority,
        )
        success = await self.assign_mission(robot_id, mission)
        return mission.mission_id if success else None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_fleet_status(self) -> dict[str, Any]:
        """Return a dict summary of all robot states."""
        robots_info = {}
        for robot_id, entry in self._robots.items():
            sim = entry.simulator
            robots_info[robot_id] = {
                "robot_id": robot_id,
                "state": sim.state.value,
                "battery_level": round(sim.battery_level, 3),
                "position": {
                    "x": round(sim.position.x, 2),
                    "y": round(sim.position.y, 2),
                    "theta": round(sim.position.theta, 4),
                },
                "current_mission": (
                    entry.current_mission.mission_id
                    if entry.current_mission
                    else None
                ),
                "completed_missions": entry.completed_missions,
                "failed_missions": entry.failed_missions,
                "uptime_seconds": round(sim._uptime_s, 1),
            }
        return {
            "fleet_size": len(self._robots),
            "running": self._running,
            "pending_missions": self._mission_queue.qsize(),
            "robots": robots_info,
        }

    def get_robot_ids(self) -> list[str]:
        return list(self._robots.keys())

    def get_robot_state(self, robot_id: str) -> Optional[dict[str, Any]]:
        entry = self._robots.get(robot_id)
        if not entry:
            return None
        sim = entry.simulator
        return {
            "robot_id": robot_id,
            "state": sim.state.value,
            "battery_level": sim.battery_level,
            "position": {"x": sim.position.x, "y": sim.position.y},
            "active_faults": [f.value for f in sim._active_faults],
        }

    # ------------------------------------------------------------------
    # Private: robot telemetry loop
    # ------------------------------------------------------------------

    async def _robot_telemetry_loop(self, entry: RobotEntry) -> None:
        """
        Runs the robot simulator and publishes each packet to Redis.
        """
        robot_id = entry.robot_id
        try:
            async for packet in entry.simulator.run():
                await self._publisher.publish(packet)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error(
                "robot_telemetry_loop_error",
                robot_id=robot_id,
                error=str(exc),
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Private: mission dispatcher
    # ------------------------------------------------------------------

    async def _mission_dispatcher(self) -> None:
        """
        Continuously pulls missions from the priority queue and dispatches them
        to robots.  Implements deadlock prevention via robot priority.
        """
        self._log.info("mission_dispatcher_started")
        while self._running:
            try:
                # Non-blocking: only dispatch if a robot is available
                if self._mission_queue.empty():
                    await asyncio.sleep(0.5)
                    continue

                item = await asyncio.wait_for(
                    self._mission_queue.get(), timeout=1.0
                )
                mission = item.mission
                robot_id = mission.robot_id

                entry = self._robots.get(robot_id)
                if not entry:
                    self._log.warning(
                        "mission_dropped_robot_gone",
                        mission_id=mission.mission_id,
                        robot_id=robot_id,
                    )
                    continue

                sim = entry.simulator
                if sim.state in (RobotSimState.FAULT, RobotSimState.ESTOP):
                    # Re-enqueue with slight delay
                    await asyncio.sleep(2.0)
                    await self._mission_queue.put(item)
                    continue

                # Execute the mission
                asyncio.create_task(
                    self._execute_mission(entry, mission),
                    name=f"mission_{mission.mission_id}",
                )

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error(
                    "dispatcher_error", error=str(exc), exc_info=True
                )
                await asyncio.sleep(1.0)

    async def _execute_mission(self, entry: RobotEntry, mission: Mission) -> None:
        """Execute a single mission on a robot."""
        robot_id = entry.robot_id
        sim = entry.simulator
        entry.current_mission = mission

        self._log.info(
            "mission_started",
            mission_id=mission.mission_id,
            robot_id=robot_id,
            type=mission.mission_type,
        )

        try:
            if mission.mission_type == MissionType.MOVE_TO_GOAL:
                if mission.waypoints:
                    goal_x, goal_y = mission.waypoints[-1]
                    goal = NavigationGoal(
                        robot_id=robot_id,
                        goal_x=goal_x,
                        goal_y=goal_y,
                        priority=mission.priority,
                    )
                    accepted = await sim.set_navigation_goal(goal)
                    if accepted:
                        await self._wait_for_goal_reached(sim, goal_x, goal_y)

            elif mission.mission_type == MissionType.PATROL_ROUTE:
                for wp_x, wp_y in mission.waypoints:
                    if not self._running:
                        break
                    goal = NavigationGoal(
                        robot_id=robot_id,
                        goal_x=wp_x,
                        goal_y=wp_y,
                        priority=mission.priority,
                    )
                    accepted = await sim.set_navigation_goal(goal)
                    if accepted:
                        await self._wait_for_goal_reached(sim, wp_x, wp_y)

            elif mission.mission_type == MissionType.DOCK_AT_CHARGER:
                if mission.waypoints:
                    dock_x, dock_y = mission.waypoints[0]
                    await sim.dock_at_charger(dock_x, dock_y)
                    await self._wait_for_goal_reached(sim, dock_x, dock_y)

            mission.completed = True
            entry.completed_missions += 1
            self._log.info(
                "mission_completed",
                mission_id=mission.mission_id,
                robot_id=robot_id,
            )

        except asyncio.CancelledError:
            mission.failed = True
            entry.failed_missions += 1
            self._log.warning(
                "mission_cancelled", mission_id=mission.mission_id, robot_id=robot_id
            )
        except Exception as exc:
            mission.failed = True
            entry.failed_missions += 1
            self._log.error(
                "mission_failed",
                mission_id=mission.mission_id,
                robot_id=robot_id,
                error=str(exc),
                exc_info=True,
            )
        finally:
            entry.current_mission = None
            self._pending_missions.pop(mission.mission_id, None)

    async def _wait_for_goal_reached(
        self,
        sim: AsyncRobotSimulator,
        goal_x: float,
        goal_y: float,
        timeout: float = MISSION_TIMEOUT_S,
    ) -> None:
        """Poll until robot reaches the goal or times out."""
        import math
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            dist = math.hypot(sim.position.x - goal_x, sim.position.y - goal_y)
            if dist < 0.3:
                return
            if sim.state in (RobotSimState.FAULT, RobotSimState.ESTOP):
                raise RuntimeError(f"Robot {sim.robot_id} in fault state during mission")
            await asyncio.sleep(0.5)
        self._log.warning(
            "goal_timeout",
            robot_id=sim.robot_id,
            goal_x=goal_x,
            goal_y=goal_y,
        )

    # ------------------------------------------------------------------
    # Private: SimPy loop
    # ------------------------------------------------------------------

    async def _run_simpy_loop(self) -> None:
        """
        Runs a SimPy environment for discrete-event mission scheduling.
        The SimPy environment manages mission queue timing events.
        """
        if self._simpy_env is None:
            return
        env = self._simpy_env
        env.process(self._simpy_mission_scheduler(env))
        try:
            while self._running:
                # Step the SimPy clock forward by 0.1 simulation seconds
                env.step()
                await asyncio.sleep(0.01)
        except simpy.core.EmptySchedule:
            pass
        except asyncio.CancelledError:
            pass

    def _simpy_mission_scheduler(self, env: simpy.Environment):  # type: ignore[no-untyped-def]
        """SimPy process: emits periodic scheduling events."""
        while True:
            yield env.timeout(1)  # 1 simulated second
            # Future: emit SimPy events for timed mission dispatch
