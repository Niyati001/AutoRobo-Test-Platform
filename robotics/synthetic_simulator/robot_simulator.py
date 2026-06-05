"""
AsyncRobotSimulator: stochastic AMR simulation with realistic telemetry generation.

Each instance runs a single robot and yields TelemetryPacket objects via an
async generator.  State transitions follow a formal state machine.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import AsyncGenerator, Optional

import numpy as np
import structlog

from .models import (
    BatteryState,
    DiagnosticsState,
    FaultType,
    MotorState,
    NavigationGoal,
    NavigationState,
    NavigationStatus,
    Position,
    RobotFaultFlags,
    RobotMode,
    RobotSimState,
    SensorState,
    TelemetryPacket,
    Velocity,
)
from .warehouse_physics import WarehousePhysics

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Physical constants
# ---------------------------------------------------------------------------

WHEEL_RADIUS_M = 0.1          # metres
WHEEL_BASE_M = 0.5            # metres (distance between wheels)
MAX_LINEAR_VEL = 1.2          # m/s
MAX_ANGULAR_VEL = 1.5         # rad/s
MAX_LINEAR_ACCEL = 0.5        # m/s²
MAX_ANGULAR_ACCEL = 1.0       # rad/s²
WAYPOINT_TOLERANCE_M = 0.25   # metres — "close enough" to a waypoint
GOAL_TOLERANCE_M = 0.15       # metres — "arrived at goal"

# Battery discharge model
BASE_DISCHARGE_RATE = 0.00005   # per second at idle
MOTION_DISCHARGE_COEFF = 0.0002  # per second per (m/s)²
CHARGE_RATE = 0.0003             # per second while docked

# CPU thermal model
CPU_THERMAL_TAU = 120.0          # seconds time constant
AMBIENT_TEMP = 25.0              # °C
MAX_CPU_TEMP = 85.0              # °C at 100% load


class AsyncRobotSimulator:
    """
    Simulates one AMR robot asynchronously.

    Usage::

        physics = WarehousePhysics(warehouse_map)
        sim = AsyncRobotSimulator("amr-001", physics, rate_hz=10.0)
        async for packet in sim.run():
            await publish(packet)
    """

    def __init__(
        self,
        robot_id: str,
        physics: WarehousePhysics,
        rate_hz: float = 10.0,
        initial_battery: float = 1.0,
        seed: Optional[int] = None,
    ) -> None:
        self.robot_id = robot_id
        self._physics = physics
        self._rate_hz = rate_hz
        self._dt = 1.0 / rate_hz
        self._rng = np.random.default_rng(seed)

        # --- Mutable state ---
        self._state = RobotSimState.IDLE
        self._pos = Position(x=0.0, y=0.0, theta=0.0)
        self._vel = Velocity(linear=0.0, angular=0.0)

        # Battery
        self._battery_level: float = initial_battery
        self._battery_voltage: float = 24.0 + 4.0 * initial_battery  # 24-28 V range

        # Motor noise state
        self._left_rpm: float = 0.0
        self._right_rpm: float = 0.0
        self._motor_torque: float = 0.0

        # Sensor state
        self._imu_drift: float = 0.0            # random walk (rad/s)
        self._imu_drift_vel: float = 0.0        # rate of change
        self._lidar_quality: float = 1.0

        # CPU thermal state
        self._cpu_temp: float = AMBIENT_TEMP
        self._cpu_usage: float = 0.15
        self._memory_usage: float = 0.35
        self._net_latency_ms: float = 5.0

        # Navigation
        self._waypoints: list[tuple[float, float]] = []
        self._wp_index: int = 0
        self._nav_state = NavigationState.IDLE
        self._current_goal: Optional[tuple[float, float]] = None
        self._target_linear_vel: float = 0.0
        self._target_angular_vel: float = 0.0

        # Fault state
        self._fault_flags = RobotFaultFlags()
        self._active_faults: set[FaultType] = set()

        # Odometry / uptime
        self._odometer_m: float = 0.0
        self._uptime_s: float = 0.0
        self._started_at: float = time.time()

        # Control
        self._running = False
        self._stop_event = asyncio.Event()

        # Mission/command queue
        self._command_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)

        self._log = logger.bind(robot_id=robot_id)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> AsyncGenerator[TelemetryPacket, None]:
        """
        Main async generator.  Yields a TelemetryPacket at the configured Hz
        rate until stop() is called.
        """
        self._running = True
        self._stop_event.clear()
        self._log.info("robot_simulator_started", rate_hz=self._rate_hz)

        try:
            while self._running:
                loop_start = time.monotonic()

                # Process pending commands
                await self._drain_commands()

                # Advance simulation by dt
                self._step()

                # Build and yield telemetry
                packet = self._build_telemetry_packet()
                yield packet

                # Sleep for remainder of tick
                elapsed = time.monotonic() - loop_start
                sleep_time = max(0.0, self._dt - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            self._log.info("robot_simulator_cancelled")
        finally:
            self._running = False
            self._stop_event.set()
            self._log.info("robot_simulator_stopped", uptime_s=self._uptime_s)

    def stop(self) -> None:
        """Signal the simulator to stop."""
        self._running = False

    async def wait_stopped(self) -> None:
        await self._stop_event.wait()

    async def set_navigation_goal(self, goal: NavigationGoal) -> bool:
        """
        Send a navigation goal to the robot.  Returns True if accepted.
        """
        if self._state in (RobotSimState.FAULT, RobotSimState.ESTOP):
            self._log.warning("goal_rejected_due_to_fault", state=self._state)
            return False
        await self._command_queue.put(
            {
                "type": "nav_goal",
                "goal_x": goal.goal_x,
                "goal_y": goal.goal_y,
                "goal_theta": goal.goal_theta,
            }
        )
        return True

    async def apply_fault(self, fault_type: FaultType) -> None:
        """Inject a fault into the simulator."""
        await self._command_queue.put({"type": "fault", "fault": fault_type})

    async def clear_fault(self, fault_type: FaultType) -> None:
        """Clear a previously injected fault."""
        await self._command_queue.put({"type": "clear_fault", "fault": fault_type})

    async def dock_at_charger(self, charger_x: float, charger_y: float) -> None:
        """Command the robot to navigate to and dock at a charging station."""
        await self._command_queue.put(
            {"type": "dock", "charger_x": charger_x, "charger_y": charger_y}
        )

    @property
    def state(self) -> RobotSimState:
        return self._state

    @property
    def position(self) -> Position:
        return self._pos

    @property
    def battery_level(self) -> float:
        return self._battery_level

    # ------------------------------------------------------------------
    # Internal: command handling
    # ------------------------------------------------------------------

    async def _drain_commands(self) -> None:
        while not self._command_queue.empty():
            try:
                cmd = self._command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._process_command(cmd)

    async def _process_command(self, cmd: dict) -> None:
        cmd_type = cmd.get("type")

        if cmd_type == "nav_goal":
            gx, gy = cmd["goal_x"], cmd["goal_y"]
            self._log.info("new_nav_goal", goal_x=gx, goal_y=gy)
            path = await asyncio.to_thread(
                self._physics.find_path,
                self._pos.x, self._pos.y, gx, gy,
            )
            if path:
                self._waypoints = path
                self._wp_index = 0
                self._current_goal = (gx, gy)
                self._nav_state = NavigationState.MOVING
                self._transition_state(RobotSimState.MOVING)
            else:
                self._log.warning("no_path_found", goal_x=gx, goal_y=gy)

        elif cmd_type == "fault":
            fault: FaultType = cmd["fault"]
            self._active_faults.add(fault)
            self._apply_fault_effects(fault)
            self._log.warning("fault_injected", fault=fault)

        elif cmd_type == "clear_fault":
            fault = cmd["fault"]
            self._active_faults.discard(fault)
            self._clear_fault_effects(fault)
            self._log.info("fault_cleared", fault=fault)
            if not self._active_faults and self._state == RobotSimState.FAULT:
                self._transition_state(RobotSimState.RECOVERY)

        elif cmd_type == "dock":
            cx, cy = cmd["charger_x"], cmd["charger_y"]
            path = await asyncio.to_thread(
                self._physics.find_path,
                self._pos.x, self._pos.y, cx, cy,
            )
            if path:
                self._waypoints = path
                self._wp_index = 0
                self._current_goal = (cx, cy)
                self._nav_state = NavigationState.MOVING
                self._transition_state(RobotSimState.MOVING)

    # ------------------------------------------------------------------
    # Internal: state machine transitions
    # ------------------------------------------------------------------

    def _transition_state(self, new_state: RobotSimState) -> None:
        if new_state != self._state:
            self._log.debug("state_transition", old=self._state, new=new_state)
            self._state = new_state

    def _apply_fault_effects(self, fault: FaultType) -> None:
        if fault == FaultType.ESTOP:
            self._fault_flags = self._fault_flags.model_copy(update={"estop_active": True})
            self._transition_state(RobotSimState.ESTOP)
        elif fault == FaultType.MOTOR_FAULT:
            self._fault_flags = self._fault_flags.model_copy(update={"motor_fault": True})
            self._transition_state(RobotSimState.FAULT)
        elif fault == FaultType.BATTERY_CRITICAL:
            self._fault_flags = self._fault_flags.model_copy(update={"battery_critical": True})
            self._transition_state(RobotSimState.FAULT)
        elif fault == FaultType.LIDAR_DEGRADED:
            self._fault_flags = self._fault_flags.model_copy(update={"lidar_degraded": True})
        elif fault == FaultType.IMU_FAILURE:
            self._fault_flags = self._fault_flags.model_copy(update={"imu_failure": True})
        elif fault == FaultType.NETWORK_LOSS:
            self._fault_flags = self._fault_flags.model_copy(update={"network_loss": True})

    def _clear_fault_effects(self, fault: FaultType) -> None:
        if fault == FaultType.ESTOP:
            self._fault_flags = self._fault_flags.model_copy(update={"estop_active": False})
        elif fault == FaultType.MOTOR_FAULT:
            self._fault_flags = self._fault_flags.model_copy(update={"motor_fault": False})
        elif fault == FaultType.BATTERY_CRITICAL:
            self._fault_flags = self._fault_flags.model_copy(update={"battery_critical": False})
        elif fault == FaultType.LIDAR_DEGRADED:
            self._fault_flags = self._fault_flags.model_copy(update={"lidar_degraded": False})
        elif fault == FaultType.IMU_FAILURE:
            self._fault_flags = self._fault_flags.model_copy(update={"imu_failure": False})
        elif fault == FaultType.NETWORK_LOSS:
            self._fault_flags = self._fault_flags.model_copy(update={"network_loss": False})

    # ------------------------------------------------------------------
    # Internal: physics step
    # ------------------------------------------------------------------

    def _step(self) -> None:
        """Advance simulation by one dt tick."""
        self._uptime_s += self._dt

        if self._state == RobotSimState.ESTOP:
            self._step_estop()
        elif self._state == RobotSimState.FAULT:
            self._step_fault()
        elif self._state == RobotSimState.RECOVERY:
            self._step_recovery()
        elif self._state == RobotSimState.CHARGING:
            self._step_charging()
        elif self._state == RobotSimState.MOVING:
            self._step_moving()
        elif self._state == RobotSimState.OBSTACLE_AVOIDANCE:
            self._step_obstacle_avoidance()
        else:  # IDLE
            self._step_idle()

        # Always update sensors, battery, thermal model
        self._update_battery()
        self._update_sensors()
        self._update_thermal()
        self._update_imu_drift()
        self._update_network_sim()

        # Battery critical check
        if self._battery_level < 0.05 and FaultType.BATTERY_CRITICAL not in self._active_faults:
            self._active_faults.add(FaultType.BATTERY_CRITICAL)
            self._apply_fault_effects(FaultType.BATTERY_CRITICAL)

    def _step_idle(self) -> None:
        self._target_linear_vel = 0.0
        self._target_angular_vel = 0.0
        self._nav_state = NavigationState.IDLE
        self._update_velocity()

    def _step_estop(self) -> None:
        self._target_linear_vel = 0.0
        self._target_angular_vel = 0.0
        self._update_velocity()

    def _step_fault(self) -> None:
        self._target_linear_vel = 0.0
        self._target_angular_vel = 0.0
        self._update_velocity()

    def _step_recovery(self) -> None:
        # Rotate slowly in place to reorient
        self._target_linear_vel = 0.0
        self._target_angular_vel = 0.3
        self._update_velocity()
        if self._uptime_s % 5.0 < self._dt:
            self._transition_state(RobotSimState.IDLE)

    def _step_charging(self) -> None:
        self._target_linear_vel = 0.0
        self._target_angular_vel = 0.0
        self._update_velocity()
        if self._battery_level >= 0.99:
            self._transition_state(RobotSimState.IDLE)

    def _step_moving(self) -> None:
        if not self._waypoints or self._wp_index >= len(self._waypoints):
            self._transition_state(RobotSimState.IDLE)
            self._nav_state = NavigationState.GOAL_REACHED
            self._current_goal = None
            self._target_linear_vel = 0.0
            self._target_angular_vel = 0.0
            self._update_velocity()
            return

        # Current target waypoint
        wp_x, wp_y = self._waypoints[self._wp_index]
        dx = wp_x - self._pos.x
        dy = wp_y - self._pos.y
        dist = math.hypot(dx, dy)

        if dist < WAYPOINT_TOLERANCE_M:
            self._wp_index += 1
            if self._wp_index >= len(self._waypoints):
                self._transition_state(RobotSimState.IDLE)
                self._nav_state = NavigationState.GOAL_REACHED
                self._target_linear_vel = 0.0
                self._target_angular_vel = 0.0
                self._update_velocity()
                return
            wp_x, wp_y = self._waypoints[self._wp_index]
            dx = wp_x - self._pos.x
            dy = wp_y - self._pos.y
            dist = math.hypot(dx, dy)

        desired_heading = math.atan2(dy, dx)
        heading_error = _normalise_angle(desired_heading - self._pos.theta)

        # Speed multiplier from congestion model
        speed_mult = self._physics.speed_multiplier_at(self._pos.x, self._pos.y)

        # Proportional controller for angular velocity
        k_angular = 2.0
        self._target_angular_vel = float(
            np.clip(k_angular * heading_error, -MAX_ANGULAR_VEL, MAX_ANGULAR_VEL)
        )

        # Slow down when heading error is large or near waypoint
        heading_factor = max(0.0, 1.0 - abs(heading_error) / (math.pi / 4))
        distance_factor = min(1.0, dist / 1.0)  # ramp up over 1m
        self._target_linear_vel = (
            MAX_LINEAR_VEL * heading_factor * distance_factor * speed_mult
        )

        # Lookahead collision check
        look_dist = max(0.3, self._vel.linear * 1.5)
        look_x = self._pos.x + math.cos(self._pos.theta) * look_dist
        look_y = self._pos.y + math.sin(self._pos.theta) * look_dist
        if not self._physics.is_collision_free(look_x, look_y):
            self._transition_state(RobotSimState.OBSTACLE_AVOIDANCE)
            self._nav_state = NavigationState.OBSTACLE_AVOIDANCE
            return

        self._update_velocity()
        self._update_pose()

    def _step_obstacle_avoidance(self) -> None:
        # Simple: rotate in place until path ahead is clear
        self._target_linear_vel = 0.0
        self._target_angular_vel = MAX_ANGULAR_VEL * 0.6
        self._update_velocity()
        self._update_pose()

        # Check if path is clear ahead again
        look_x = self._pos.x + math.cos(self._pos.theta) * 0.5
        look_y = self._pos.y + math.sin(self._pos.theta) * 0.5
        if self._physics.is_collision_free(look_x, look_y):
            # Replan
            if self._current_goal:
                path = self._physics.find_path(
                    self._pos.x, self._pos.y,
                    self._current_goal[0], self._current_goal[1],
                )
                if path:
                    self._waypoints = path
                    self._wp_index = 0
                    self._nav_state = NavigationState.REPLANNING
                self._transition_state(RobotSimState.MOVING)

    # ------------------------------------------------------------------
    # Internal: physics helpers
    # ------------------------------------------------------------------

    def _update_velocity(self) -> None:
        """Smoothly ramp actual velocity toward target with acceleration limits."""
        dt = self._dt

        # Linear velocity ramp
        lin_diff = self._target_linear_vel - self._vel.linear
        max_lin_delta = MAX_LINEAR_ACCEL * dt
        new_linear = self._vel.linear + float(
            np.clip(lin_diff, -max_lin_delta, max_lin_delta)
        )

        # Angular velocity ramp
        ang_diff = self._target_angular_vel - self._vel.angular
        max_ang_delta = MAX_ANGULAR_ACCEL * dt
        new_angular = self._vel.angular + float(
            np.clip(ang_diff, -max_ang_delta, max_ang_delta)
        )

        # Fault clamp
        if self._fault_flags.estop_active or self._fault_flags.motor_fault:
            new_linear = 0.0
            new_angular = 0.0

        self._vel = Velocity(linear=new_linear, angular=new_angular)

        # Motor RPM (with Gaussian noise)
        rpm_base = (abs(self._vel.linear) / (2 * math.pi * WHEEL_RADIUS_M)) * 60.0
        noise = self._rng.normal(0, 2.0)
        self._left_rpm = max(0.0, rpm_base + self._vel.angular * 5.0 + noise)
        self._right_rpm = max(0.0, rpm_base - self._vel.angular * 5.0 + noise)
        # Torque: proportional to linear accel
        self._motor_torque = max(0.0, abs(lin_diff / dt) * 0.05 + 0.1 * abs(self._vel.linear))

    def _update_pose(self) -> None:
        """Integrate differential-drive kinematics."""
        dt = self._dt
        v = self._vel.linear
        w = self._vel.angular + self._imu_drift  # IMU drift adds heading error

        if abs(w) < 1e-6:
            new_x = self._pos.x + v * math.cos(self._pos.theta) * dt
            new_y = self._pos.y + v * math.sin(self._pos.theta) * dt
            new_theta = self._pos.theta
        else:
            r = v / w
            new_theta = _normalise_angle(self._pos.theta + w * dt)
            new_x = self._pos.x + r * (math.sin(new_theta) - math.sin(self._pos.theta))
            new_y = self._pos.y - r * (math.cos(new_theta) - math.cos(self._pos.theta))

        # Clamp to map bounds
        map_w = self._physics.map.cols * self._physics.map.cell_size_m
        map_h = self._physics.map.rows * self._physics.map.cell_size_m
        new_x = float(np.clip(new_x, 0.0, map_w - 0.01))
        new_y = float(np.clip(new_y, 0.0, map_h - 0.01))

        # Odometry
        dist = math.hypot(new_x - self._pos.x, new_y - self._pos.y)
        self._odometer_m += dist

        self._pos = Position(x=new_x, y=new_y, theta=new_theta)

    def _update_battery(self) -> None:
        """
        Nonlinear battery discharge model.
        - Idle draw: constant baseline
        - Motion draw: quadratic in velocity
        - Spike: rare random surge (e.g., motor stall)
        - Charge: when in CHARGING state
        """
        if self._state == RobotSimState.CHARGING:
            # Charge with slight randomness
            charge_noise = self._rng.normal(0, 0.00003)
            self._battery_level = min(1.0, self._battery_level + CHARGE_RATE + charge_noise)
        else:
            # Base discharge
            discharge = BASE_DISCHARGE_RATE
            # Motion component
            discharge += MOTION_DISCHARGE_COEFF * self._vel.linear ** 2
            # Gaussian noise
            discharge += abs(self._rng.normal(0, 0.000005))
            # Rare spike (0.1% chance per tick)
            if self._rng.random() < 0.001:
                discharge += self._rng.exponential(0.0005)
            self._battery_level = max(0.0, self._battery_level - discharge)

        # Nonlinear voltage curve: V = 24 + 4*SoC - 0.5*I  (Peukert-like)
        current_draw = (
            0.5 + 2.0 * abs(self._vel.linear) + self._rng.normal(0, 0.1)
        )
        self._battery_voltage = (
            24.0 + 4.0 * self._battery_level - 0.5 * current_draw
        )
        self._battery_voltage = float(np.clip(self._battery_voltage, 20.0, 29.4))

    def _update_sensors(self) -> None:
        """Update LiDAR quality based on nearby obstacles."""
        if self._fault_flags.lidar_degraded:
            self._lidar_quality = float(np.clip(
                self._lidar_quality - self._rng.uniform(0, 0.01), 0.2, 1.0
            ))
        else:
            base_quality = self._physics.lidar_quality_at(self._pos.x, self._pos.y)
            noise = self._rng.normal(0, 0.01)
            self._lidar_quality = float(np.clip(base_quality + noise, 0.0, 1.0))

    def _update_thermal(self) -> None:
        """
        Simple RC thermal model: cpu_temp → ambient when idle, rises with load.
        τ = CPU_THERMAL_TAU seconds.
        """
        load_temp_target = AMBIENT_TEMP + (MAX_CPU_TEMP - AMBIENT_TEMP) * self._cpu_usage
        alpha = self._dt / CPU_THERMAL_TAU
        self._cpu_temp += alpha * (load_temp_target - self._cpu_temp)
        self._cpu_temp = float(np.clip(self._cpu_temp, AMBIENT_TEMP, MAX_CPU_TEMP))

        # CPU usage: higher in MOVING/OBSTACLE states
        target_cpu = {
            RobotSimState.IDLE: 0.12,
            RobotSimState.CHARGING: 0.10,
            RobotSimState.MOVING: 0.45,
            RobotSimState.OBSTACLE_AVOIDANCE: 0.60,
            RobotSimState.FAULT: 0.20,
            RobotSimState.RECOVERY: 0.30,
            RobotSimState.ESTOP: 0.08,
        }.get(self._state, 0.15)
        self._cpu_usage = float(np.clip(
            target_cpu + self._rng.normal(0, 0.02), 0.05, 1.0
        ))
        self._memory_usage = float(np.clip(
            self._memory_usage + self._rng.normal(0, 0.001), 0.2, 0.95
        ))

    def _update_imu_drift(self) -> None:
        """
        Random walk IMU drift model.
        The drift velocity itself performs a random walk, so drift accumulates
        smoothly rather than jumping.
        """
        if self._fault_flags.imu_failure:
            self._imu_drift += self._rng.normal(0, 0.005)
        else:
            # Ornstein-Uhlenbeck process (mean-reverting random walk)
            theta_ou = 0.1  # reversion strength
            sigma_ou = 0.0001
            self._imu_drift += (
                -theta_ou * self._imu_drift * self._dt
                + sigma_ou * self._rng.normal() * math.sqrt(self._dt)
            )
        self._imu_drift = float(np.clip(self._imu_drift, -0.05, 0.05))

    def _update_network_sim(self) -> None:
        """Simulate network latency with occasional spikes."""
        base_latency = 5.0
        jitter = abs(self._rng.normal(0, 1.5))
        # 1% chance of a latency spike
        if self._rng.random() < 0.01:
            spike = self._rng.exponential(50.0)
        else:
            spike = 0.0
        self._net_latency_ms = float(np.clip(base_latency + jitter + spike, 1.0, 500.0))

    # ------------------------------------------------------------------
    # Internal: telemetry assembly
    # ------------------------------------------------------------------

    def _build_telemetry_packet(self) -> TelemetryPacket:
        # Determine navigation status
        remaining_path_len = 0.0
        eta = 0.0
        goal_x: Optional[float] = None
        goal_y: Optional[float] = None

        if self._current_goal:
            goal_x, goal_y = self._current_goal
            if self._waypoints and self._wp_index < len(self._waypoints):
                remaining_wps = self._waypoints[self._wp_index:]
                # Add distance from current pos to first remaining wp
                first_wp = remaining_wps[0]
                remaining_path_len = math.hypot(
                    first_wp[0] - self._pos.x, first_wp[1] - self._pos.y
                )
                for i in range(len(remaining_wps) - 1):
                    x0, y0 = remaining_wps[i]
                    x1, y1 = remaining_wps[i + 1]
                    remaining_path_len += math.hypot(x1 - x0, y1 - y0)

            eff_speed = max(0.1, abs(self._vel.linear))
            eta = remaining_path_len / eff_speed

        # Determine mode
        if self._state == RobotSimState.CHARGING:
            mode = RobotMode.DOCKED
        elif self._state == RobotSimState.ESTOP:
            mode = RobotMode.MANUAL
        else:
            mode = RobotMode.AUTONOMOUS

        # Current draw for battery model
        current_draw = 0.5 + 2.0 * abs(self._vel.linear) + self._rng.normal(0, 0.1)

        return TelemetryPacket(
            robot_id=self.robot_id,
            timestamp=time.time(),
            position=Position(
                x=round(self._pos.x, 4),
                y=round(self._pos.y, 4),
                theta=round(self._pos.theta, 6),
            ),
            velocity=Velocity(
                linear=round(self._vel.linear, 4),
                angular=round(self._vel.angular, 4),
            ),
            battery=BatteryState(
                level=round(self._battery_level, 4),
                voltage=round(self._battery_voltage, 2),
                current=round(float(np.clip(current_draw, 0.0, 20.0)), 2),
            ),
            motors=MotorState(
                left_rpm=round(self._left_rpm, 1),
                right_rpm=round(self._right_rpm, 1),
                torque=round(self._motor_torque, 3),
            ),
            sensors=SensorState(
                lidar_quality=round(self._lidar_quality, 4),
                camera_active=not self._fault_flags.estop_active,
                imu_drift=round(abs(self._imu_drift), 6),
            ),
            navigation=NavigationStatus(
                state=self._nav_state,
                goal_x=goal_x,
                goal_y=goal_y,
                path_length=round(remaining_path_len, 2),
                eta_seconds=round(eta, 1),
            ),
            diagnostics=DiagnosticsState(
                cpu_temp=round(self._cpu_temp, 1),
                cpu_usage=round(self._cpu_usage, 3),
                memory_usage=round(self._memory_usage, 3),
                network_latency_ms=round(self._net_latency_ms, 1),
            ),
            fault_flags=self._fault_flags,
            mode=mode,
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _normalise_angle(angle: float) -> float:
    """Normalise angle to [-π, π]."""
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle
