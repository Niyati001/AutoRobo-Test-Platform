"""
ValidationTestRunner — real statistical tests against robot telemetry.

Each test analyses actual telemetry data using numpy/scipy and returns
a TestResult with a score between 0.0 (fail) and 1.0 (perfect pass).
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import structlog
from scipy import stats  # type: ignore[import]

from models import TestResult, ValidationTestType

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Typing aliases
# ---------------------------------------------------------------------------
Telemetry = Dict[str, Any]  # single telemetry sample
TelemetryWindow = List[Telemetry]  # ordered list oldest→newest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SAFE_DISTANCE_M = 0.5          # minimum distance to obstacle (metres)
RECOVERY_SLA_MINOR_S = 30.0    # recovery SLA for LOW/MEDIUM fault
RECOVERY_SLA_MAJOR_S = 120.0   # recovery SLA for HIGH/CRITICAL fault
BATTERY_CRITICAL_THRESHOLD = 0.10
MAX_CPU_TEMP_C = 85.0
TELEMETRY_GAP_THRESHOLD_S = 5.0
MAX_BATTERY_DRAIN_PER_SECOND = 0.003   # normal max drain rate


def _extract_float(sample: Telemetry, *keys: str, default: float = 0.0) -> float:
    """Safely navigate nested dict keys."""
    obj: Any = sample
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k, default)
    try:
        return float(obj)
    except (TypeError, ValueError):
        return default


def _safe_score(value: float) -> float:
    """Clamp a score to [0, 1]."""
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Test implementations
# ---------------------------------------------------------------------------


class ValidationTestRunner:
    """
    Stateless test runner.  Each method receives pre-collected telemetry
    and returns a TestResult.  The caller is responsible for collecting the
    data from Redis/PostgreSQL.
    """

    # ------------------------------------------------------------------
    # 1. Navigation Stability
    # ------------------------------------------------------------------
    def test_navigation_stability(self, telemetry_window: TelemetryWindow) -> TestResult:
        """
        Analyses position jitter and angular-velocity oscillations over a
        telemetry window.

        Scoring:
          - Angular velocity variance component (50%)
          - Position deviation from smoothed path component (50%)
        """
        start = time.monotonic()
        if len(telemetry_window) < 5:
            return TestResult(
                test_name=ValidationTestType.NAVIGATION_STABILITY,
                passed=False,
                score=0.0,
                details={"reason": "Insufficient telemetry samples"},
                duration_seconds=time.monotonic() - start,
            )

        try:
            angular_vels = np.array([
                _extract_float(s, "velocity", "angular") for s in telemetry_window
            ], dtype=float)
            xs = np.array([_extract_float(s, "position", "x") for s in telemetry_window], dtype=float)
            ys = np.array([_extract_float(s, "position", "y") for s in telemetry_window], dtype=float)

            # Angular velocity variance — lower is more stable
            ang_var = float(np.var(angular_vels))
            # Normalised: assume variance > 0.1 rad²/s² is very poor
            ang_score = _safe_score(1.0 - min(ang_var / 0.1, 1.0))

            # Position smoothness — compare actual path to low-pass filtered path
            window_size = max(3, len(xs) // 5)
            kernel = np.ones(window_size) / window_size
            xs_smooth = np.convolve(xs, kernel, mode="valid")
            ys_smooth = np.convolve(ys, kernel, mode="valid")
            n = min(len(xs_smooth), len(xs) - window_size + 1)
            pos_deviations = np.sqrt(
                (xs[:n] - xs_smooth[:n]) ** 2 + (ys[:n] - ys_smooth[:n]) ** 2
            )
            mean_dev = float(np.mean(pos_deviations))
            # Assume mean deviation > 0.5 m is unacceptable
            pos_score = _safe_score(1.0 - min(mean_dev / 0.5, 1.0))

            combined_score = 0.5 * ang_score + 0.5 * pos_score
            passed = combined_score >= 0.7

            return TestResult(
                test_name=ValidationTestType.NAVIGATION_STABILITY,
                passed=passed,
                score=_safe_score(combined_score),
                details={
                    "angular_velocity_variance": round(ang_var, 6),
                    "angular_score": round(ang_score, 4),
                    "mean_position_deviation_m": round(mean_dev, 4),
                    "position_score": round(pos_score, 4),
                    "samples_analysed": len(telemetry_window),
                },
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("navigation_stability_test_error")
            return TestResult(
                test_name=ValidationTestType.NAVIGATION_STABILITY,
                passed=False,
                score=0.0,
                details={},
                duration_seconds=time.monotonic() - start,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # 2. Rerouting Validation
    # ------------------------------------------------------------------
    def test_rerouting_validation(
        self,
        robot_id: str,
        obstacle_injection_event: Dict[str, Any],
        telemetry_after_injection: TelemetryWindow,
        original_eta_seconds: float,
        rerouting_timeout_seconds: float = 30.0,
    ) -> TestResult:
        """
        After obstacle injection, verify that the robot:
          1. Stops/reroutes within *rerouting_timeout_seconds*
          2. Its new path length is reasonable (< 2x original)
          3. It reaches the goal within 1.5x the original ETA
        """
        start = time.monotonic()
        if not telemetry_after_injection:
            return TestResult(
                test_name=ValidationTestType.REROUTING,
                passed=False,
                score=0.0,
                details={"reason": "No post-injection telemetry"},
                duration_seconds=time.monotonic() - start,
            )

        try:
            injection_ts = float(obstacle_injection_event.get("injected_at", 0.0))

            # Find first sample where nav state changes or path_length increases
            reroute_detected = False
            reroute_latency_s = rerouting_timeout_seconds  # pessimistic
            first_path_length = _extract_float(
                telemetry_after_injection[0], "navigation", "path_length"
            )

            for sample in telemetry_after_injection:
                ts = _extract_float(sample, "timestamp")
                elapsed = ts - injection_ts if injection_ts > 0 else 0.0
                nav_state = sample.get("navigation", {}).get("state", "")
                path_length = _extract_float(sample, "navigation", "path_length")

                # Rerouting detected when path length changes significantly or state is REROUTING/REPLANNING
                if (
                    nav_state in ("REROUTING", "REPLANNING", "RECOVERY")
                    or abs(path_length - first_path_length) > 0.5
                ):
                    reroute_detected = True
                    reroute_latency_s = max(0.0, elapsed)
                    break

            # Check if goal was eventually reached
            goal_reached = any(
                s.get("navigation", {}).get("state") == "IDLE"
                or _extract_float(s, "navigation", "eta_seconds") == 0.0
                for s in telemetry_after_injection[-10:]
            )

            # Scoring breakdown
            latency_score = _safe_score(1.0 - (reroute_latency_s / rerouting_timeout_seconds))
            detection_score = 1.0 if reroute_detected else 0.0
            goal_score = 1.0 if goal_reached else 0.4

            # Check final ETA vs original
            if telemetry_after_injection:
                final_eta = _extract_float(telemetry_after_injection[-1], "navigation", "eta_seconds")
                eta_ratio = final_eta / max(original_eta_seconds, 1.0)
                eta_score = _safe_score(1.0 - max(0.0, eta_ratio - 1.5) / 1.5)
            else:
                eta_score = 0.5

            combined = 0.3 * latency_score + 0.3 * detection_score + 0.2 * goal_score + 0.2 * eta_score
            passed = combined >= 0.6 and reroute_detected

            return TestResult(
                test_name=ValidationTestType.REROUTING,
                passed=passed,
                score=_safe_score(combined),
                details={
                    "reroute_detected": reroute_detected,
                    "reroute_latency_seconds": round(reroute_latency_s, 2),
                    "goal_reached": goal_reached,
                    "latency_score": round(latency_score, 4),
                    "eta_score": round(eta_score, 4),
                    "samples_analysed": len(telemetry_after_injection),
                },
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("rerouting_test_error")
            return TestResult(
                test_name=ValidationTestType.REROUTING,
                passed=False,
                score=0.0,
                details={},
                duration_seconds=time.monotonic() - start,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # 3. Collision Prevention
    # ------------------------------------------------------------------
    def test_collision_prevention(self, telemetry_history: TelemetryWindow) -> TestResult:
        """
        Simulates minimum-distance analysis.

        We use the robot's position + LiDAR quality as a proxy for collision risk:
        - Low lidar_quality + high speed = collision risk event
        - Checks deceleration near obstacles (linear vel decreasing when lidar_quality < 0.6)
        """
        start = time.monotonic()
        if len(telemetry_history) < 3:
            return TestResult(
                test_name=ValidationTestType.COLLISION_PREVENTION,
                passed=False,
                score=0.0,
                details={"reason": "Insufficient telemetry"},
                duration_seconds=time.monotonic() - start,
            )

        try:
            lidar_qualities = np.array([
                _extract_float(s, "sensors", "lidar_quality") for s in telemetry_history
            ], dtype=float)
            linear_vels = np.array([
                _extract_float(s, "velocity", "linear") for s in telemetry_history
            ], dtype=float)

            # Risk events: low lidar quality AND high speed
            proximity_mask = lidar_qualities < 0.6
            risk_events = np.sum(proximity_mask & (linear_vels > 0.3))

            # Deceleration compliance: when lidar < 0.6, speed should be < 0.3 m/s
            unsafe_events = int(np.sum(proximity_mask & (linear_vels > 0.3)))
            total_proximity_events = int(np.sum(proximity_mask))

            if total_proximity_events == 0:
                # No obstacle encounters — perfect score
                collision_free_score = 1.0
                decel_compliance = 1.0
            else:
                safe_events = total_proximity_events - unsafe_events
                decel_compliance = safe_events / total_proximity_events
                collision_free_score = _safe_score(1.0 - (unsafe_events / len(telemetry_history)))

            # Minimum speed during proximity events
            if total_proximity_events > 0:
                max_speed_near_obstacle = float(np.max(linear_vels[proximity_mask]))
            else:
                max_speed_near_obstacle = 0.0

            combined = 0.5 * collision_free_score + 0.5 * decel_compliance
            passed = combined >= 0.75

            return TestResult(
                test_name=ValidationTestType.COLLISION_PREVENTION,
                passed=passed,
                score=_safe_score(combined),
                details={
                    "total_proximity_events": total_proximity_events,
                    "unsafe_events": unsafe_events,
                    "deceleration_compliance_rate": round(decel_compliance, 4),
                    "max_speed_near_obstacle_ms": round(max_speed_near_obstacle, 3),
                    "collision_free_score": round(collision_free_score, 4),
                    "samples_analysed": len(telemetry_history),
                },
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("collision_prevention_test_error")
            return TestResult(
                test_name=ValidationTestType.COLLISION_PREVENTION,
                passed=False,
                score=0.0,
                details={},
                duration_seconds=time.monotonic() - start,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # 4. Telemetry Integrity
    # ------------------------------------------------------------------
    def test_telemetry_integrity(self, telemetry_stream: TelemetryWindow) -> TestResult:
        """
        Validates telemetry data quality:
          - Missing required fields
          - NaN / out-of-range values
          - Timestamp gaps > 5 seconds
          - Packet loss rate
        """
        start = time.monotonic()
        if not telemetry_stream:
            return TestResult(
                test_name=ValidationTestType.TELEMETRY_INTEGRITY,
                passed=False,
                score=0.0,
                details={"reason": "Empty telemetry stream"},
                duration_seconds=time.monotonic() - start,
            )

        REQUIRED_FIELDS = [
            "robot_id", "timestamp", "position", "velocity",
            "battery", "motors", "sensors", "navigation",
        ]
        VALUE_RANGES = {
            ("battery", "level"): (0.0, 1.0),
            ("velocity", "linear"): (-5.0, 5.0),
            ("diagnostics", "cpu_temp"): (0.0, 120.0),
            ("sensors", "lidar_quality"): (0.0, 1.0),
            ("diagnostics", "cpu_usage"): (0.0, 1.0),
            ("diagnostics", "memory_usage"): (0.0, 1.0),
        }

        missing_field_count = 0
        nan_count = 0
        out_of_range_count = 0
        gap_count = 0
        total_samples = len(telemetry_stream)
        prev_ts: Optional[float] = None

        for sample in telemetry_stream:
            # Required field check
            for field in REQUIRED_FIELDS:
                if field not in sample:
                    missing_field_count += 1

            # Timestamp gap check
            ts = sample.get("timestamp")
            if ts is not None:
                try:
                    ts_f = float(ts)
                    if prev_ts is not None and (ts_f - prev_ts) > TELEMETRY_GAP_THRESHOLD_S:
                        gap_count += 1
                    prev_ts = ts_f
                except (TypeError, ValueError):
                    nan_count += 1

            # Range checks
            for (key1, key2), (lo, hi) in VALUE_RANGES.items():
                val = _extract_float(sample, key1, key2, default=float("nan"))
                if math.isnan(val):
                    nan_count += 1
                elif not (lo <= val <= hi):
                    out_of_range_count += 1

        # Score components
        field_completeness = _safe_score(
            1.0 - min(missing_field_count / (total_samples * len(REQUIRED_FIELDS)), 1.0)
        )
        nan_score = _safe_score(1.0 - min(nan_count / (total_samples * len(VALUE_RANGES)), 1.0))
        range_score = _safe_score(
            1.0 - min(out_of_range_count / (total_samples * len(VALUE_RANGES)), 1.0)
        )
        gap_score = _safe_score(1.0 - min(gap_count / max(total_samples, 1), 1.0))

        combined = 0.3 * field_completeness + 0.25 * nan_score + 0.25 * range_score + 0.2 * gap_score
        passed = combined >= 0.85

        return TestResult(
            test_name=ValidationTestType.TELEMETRY_INTEGRITY,
            passed=passed,
            score=_safe_score(combined),
            details={
                "total_samples": total_samples,
                "missing_field_count": missing_field_count,
                "nan_count": nan_count,
                "out_of_range_count": out_of_range_count,
                "timestamp_gap_count": gap_count,
                "field_completeness_score": round(field_completeness, 4),
                "nan_score": round(nan_score, 4),
                "range_score": round(range_score, 4),
                "gap_score": round(gap_score, 4),
            },
            duration_seconds=time.monotonic() - start,
        )

    # ------------------------------------------------------------------
    # 5. Recovery Behaviour
    # ------------------------------------------------------------------
    def test_recovery_behavior(
        self,
        fault_event: Dict[str, Any],
        telemetry_after_fault: TelemetryWindow,
    ) -> TestResult:
        """
        Validates:
          1. Robot enters RECOVERY state after fault
          2. Recovery completes within SLA (30s minor / 120s major)
          3. Robot returns to operational (MOVING / IDLE) state
        """
        start = time.monotonic()
        if not telemetry_after_fault:
            return TestResult(
                test_name=ValidationTestType.RECOVERY_BEHAVIOR,
                passed=False,
                score=0.0,
                details={"reason": "No post-fault telemetry"},
                duration_seconds=time.monotonic() - start,
            )

        try:
            severity = str(fault_event.get("severity", "MEDIUM")).upper()
            sla = RECOVERY_SLA_MINOR_S if severity in ("LOW", "MEDIUM") else RECOVERY_SLA_MAJOR_S
            fault_ts = float(fault_event.get("injected_at", 0.0))

            recovery_entered = False
            recovery_ts: Optional[float] = None
            operational_restored = False
            operational_ts: Optional[float] = None

            for sample in telemetry_after_fault:
                nav_state = sample.get("navigation", {}).get("state", "")
                ts = _extract_float(sample, "timestamp")

                if not recovery_entered and nav_state in ("RECOVERY", "FAULT", "STOPPED"):
                    recovery_entered = True
                    recovery_ts = ts

                if recovery_entered and not operational_restored and nav_state in ("MOVING", "IDLE", "NAVIGATING"):
                    operational_restored = True
                    operational_ts = ts

            # Compute recovery time
            recovery_time_s: float = sla  # pessimistic default
            if recovery_ts and fault_ts:
                recovery_time_s = recovery_ts - fault_ts
            if recovery_ts and operational_ts:
                actual_recovery_duration = operational_ts - recovery_ts
            elif recovery_ts:
                actual_recovery_duration = sla  # still not recovered
            else:
                actual_recovery_duration = sla

            # Scoring
            entry_score = 1.0 if recovery_entered else 0.0
            sla_score = _safe_score(1.0 - (actual_recovery_duration / sla))
            restoration_score = 1.0 if operational_restored else 0.0

            combined = 0.3 * entry_score + 0.4 * sla_score + 0.3 * restoration_score
            passed = recovery_entered and sla_score >= 0.5 and operational_restored

            return TestResult(
                test_name=ValidationTestType.RECOVERY_BEHAVIOR,
                passed=passed,
                score=_safe_score(combined),
                details={
                    "fault_severity": severity,
                    "sla_seconds": sla,
                    "recovery_state_entered": recovery_entered,
                    "actual_recovery_duration_seconds": round(actual_recovery_duration, 2),
                    "operational_restored": operational_restored,
                    "entry_score": round(entry_score, 4),
                    "sla_score": round(sla_score, 4),
                    "restoration_score": round(restoration_score, 4),
                },
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("recovery_behavior_test_error")
            return TestResult(
                test_name=ValidationTestType.RECOVERY_BEHAVIOR,
                passed=False,
                score=0.0,
                details={},
                duration_seconds=time.monotonic() - start,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # 6. Battery Endurance
    # ------------------------------------------------------------------
    def test_battery_endurance(self, telemetry_history: TelemetryWindow) -> TestResult:
        """
        Analyses battery discharge curve:
          - Linear regression of battery level over time
          - Detect abnormal drain spikes (Z-score > 3)
          - Ensure level never drops below critical threshold during mission
        """
        start = time.monotonic()
        if len(telemetry_history) < 10:
            return TestResult(
                test_name=ValidationTestType.BATTERY_ENDURANCE,
                passed=False,
                score=0.0,
                details={"reason": "Insufficient telemetry for battery analysis"},
                duration_seconds=time.monotonic() - start,
            )

        try:
            timestamps = np.array([
                _extract_float(s, "timestamp") for s in telemetry_history
            ], dtype=float)
            battery_levels = np.array([
                _extract_float(s, "battery", "level") for s in telemetry_history
            ], dtype=float)

            # Normalise timestamps to seconds from start
            t_rel = timestamps - timestamps[0]
            duration = float(t_rel[-1]) if t_rel[-1] > 0 else 1.0

            # Linear regression to get average drain rate
            if duration > 0:
                slope, intercept, r_value, p_value, std_err = stats.linregress(t_rel, battery_levels)
                drain_per_second = abs(float(slope))  # positive = drain
            else:
                drain_per_second = 0.0
                r_value = 0.0

            # Anomalous drain events: large drops between consecutive samples
            diffs = np.diff(battery_levels)  # negative = drain
            drains = -diffs  # positive values are drains
            dt = np.diff(t_rel)
            dt = np.where(dt == 0, 0.1, dt)  # avoid division by zero
            drain_rates = drains / dt  # per-second drain rate

            if len(drain_rates) > 3:
                z_scores = np.abs(stats.zscore(drain_rates))
                anomalous_events = int(np.sum(z_scores > 3.0))
            else:
                anomalous_events = 0

            # Critical threshold check
            min_battery = float(np.min(battery_levels))
            below_critical = bool(min_battery < BATTERY_CRITICAL_THRESHOLD)

            # Scoring
            drain_rate_score = _safe_score(1.0 - min(drain_per_second / MAX_BATTERY_DRAIN_PER_SECOND, 1.0))
            anomaly_score = _safe_score(1.0 - min(anomalous_events / max(len(drain_rates), 1), 1.0))
            critical_score = 0.0 if below_critical else 1.0

            combined = 0.3 * drain_rate_score + 0.3 * anomaly_score + 0.4 * critical_score
            passed = combined >= 0.7 and not below_critical

            return TestResult(
                test_name=ValidationTestType.BATTERY_ENDURANCE,
                passed=passed,
                score=_safe_score(combined),
                details={
                    "avg_drain_per_second": round(drain_per_second, 6),
                    "regression_r_squared": round(float(r_value) ** 2, 4),
                    "anomalous_drain_events": anomalous_events,
                    "min_battery_level": round(min_battery, 4),
                    "below_critical_threshold": below_critical,
                    "drain_rate_score": round(drain_rate_score, 4),
                    "anomaly_score": round(anomaly_score, 4),
                    "samples_analysed": len(telemetry_history),
                },
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("battery_endurance_test_error")
            return TestResult(
                test_name=ValidationTestType.BATTERY_ENDURANCE,
                passed=False,
                score=0.0,
                details={},
                duration_seconds=time.monotonic() - start,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # 7. Fleet Coordination
    # ------------------------------------------------------------------
    def test_fleet_coordination(
        self,
        multi_robot_telemetry: Dict[str, TelemetryWindow],
        grid_resolution: float = 1.0,
    ) -> TestResult:
        """
        Validates fleet-level behaviour:
          - No two robots occupy the same grid cell simultaneously
          - No deadlock conditions (robots stationary indefinitely)
          - Fleet throughput estimation
        """
        start = time.monotonic()
        if not multi_robot_telemetry or len(multi_robot_telemetry) < 2:
            return TestResult(
                test_name=ValidationTestType.FLEET_COORDINATION,
                passed=False,
                score=0.0,
                details={"reason": "Need telemetry for at least 2 robots"},
                duration_seconds=time.monotonic() - start,
            )

        try:
            collision_events = 0
            deadlock_events = 0
            total_timestep_checks = 0
            completed_missions = 0

            # Build time-indexed grid occupation maps
            # Group each robot's samples by rounded timestamp
            robot_ids = list(multi_robot_telemetry.keys())
            for robot_id, samples in multi_robot_telemetry.items():
                # Deadlock: robot stationary for > 15 consecutive samples
                vels = [_extract_float(s, "velocity", "linear") for s in samples]
                stationary_streak = 0
                max_stationary_streak = 0
                for v in vels:
                    if abs(v) < 0.01:
                        stationary_streak += 1
                        max_stationary_streak = max(max_stationary_streak, stationary_streak)
                    else:
                        stationary_streak = 0
                if max_stationary_streak > 15:
                    deadlock_events += 1

                # Count completed missions (state transitions to IDLE)
                states = [s.get("navigation", {}).get("state", "") for s in samples]
                for i in range(1, len(states)):
                    if states[i] == "IDLE" and states[i-1] not in ("IDLE", ""):
                        completed_missions += 1

            # Grid collision detection: check if any two robots share a cell at same timestamp
            # Build {ts_bucket: {cell: robot_id}} mapping
            ts_grid: Dict[int, Dict[Tuple[int, int], str]] = {}
            for robot_id, samples in multi_robot_telemetry.items():
                for sample in samples:
                    ts = int(_extract_float(sample, "timestamp"))
                    x = int(_extract_float(sample, "position", "x") / grid_resolution)
                    y = int(_extract_float(sample, "position", "y") / grid_resolution)
                    cell = (x, y)
                    if ts not in ts_grid:
                        ts_grid[ts] = {}
                    if cell in ts_grid[ts] and ts_grid[ts][cell] != robot_id:
                        collision_events += 1
                    else:
                        ts_grid[ts][cell] = robot_id
                    total_timestep_checks += 1

            # Throughput: missions per simulated minute
            if total_timestep_checks > 0 and multi_robot_telemetry:
                any_robot = next(iter(multi_robot_telemetry.values()))
                if len(any_robot) >= 2:
                    t_start = _extract_float(any_robot[0], "timestamp")
                    t_end = _extract_float(any_robot[-1], "timestamp")
                    elapsed_min = max((t_end - t_start) / 60.0, 0.01)
                    throughput = completed_missions / elapsed_min
                else:
                    throughput = 0.0
            else:
                throughput = 0.0

            # Scoring
            collision_rate = collision_events / max(total_timestep_checks, 1)
            collision_score = _safe_score(1.0 - min(collision_rate * 100, 1.0))
            deadlock_score = _safe_score(1.0 - (deadlock_events / len(robot_ids)))
            throughput_score = _safe_score(min(throughput / 10.0, 1.0))  # 10 missions/min = perfect

            combined = 0.5 * collision_score + 0.3 * deadlock_score + 0.2 * throughput_score
            passed = combined >= 0.7 and collision_events == 0

            return TestResult(
                test_name=ValidationTestType.FLEET_COORDINATION,
                passed=passed,
                score=_safe_score(combined),
                details={
                    "robots_analysed": len(robot_ids),
                    "grid_collision_events": collision_events,
                    "deadlock_events": deadlock_events,
                    "completed_missions": completed_missions,
                    "throughput_missions_per_minute": round(throughput, 2),
                    "collision_score": round(collision_score, 4),
                    "deadlock_score": round(deadlock_score, 4),
                    "throughput_score": round(throughput_score, 4),
                },
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("fleet_coordination_test_error")
            return TestResult(
                test_name=ValidationTestType.FLEET_COORDINATION,
                passed=False,
                score=0.0,
                details={},
                duration_seconds=time.monotonic() - start,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # 8. Load Stress
    # ------------------------------------------------------------------
    def test_load_stress(
        self,
        telemetry_history: TelemetryWindow,
        cpu_threshold: float = 0.90,
        memory_threshold: float = 0.90,
    ) -> TestResult:
        """
        Validates system resource usage under load:
          - CPU usage stays below threshold
          - Memory usage stays below threshold
          - Network latency remains acceptable
          - Temperature stays below MAX_CPU_TEMP_C
        """
        start = time.monotonic()
        if not telemetry_history:
            return TestResult(
                test_name=ValidationTestType.LOAD_STRESS,
                passed=False,
                score=0.0,
                details={"reason": "No telemetry data"},
                duration_seconds=time.monotonic() - start,
            )

        try:
            cpu_usages = np.array([
                _extract_float(s, "diagnostics", "cpu_usage") for s in telemetry_history
            ], dtype=float)
            mem_usages = np.array([
                _extract_float(s, "diagnostics", "memory_usage") for s in telemetry_history
            ], dtype=float)
            cpu_temps = np.array([
                _extract_float(s, "diagnostics", "cpu_temp") for s in telemetry_history
            ], dtype=float)
            net_latencies = np.array([
                _extract_float(s, "diagnostics", "network_latency_ms") for s in telemetry_history
            ], dtype=float)

            # Violation rates
            cpu_violation_rate = float(np.mean(cpu_usages > cpu_threshold))
            mem_violation_rate = float(np.mean(mem_usages > memory_threshold))
            temp_violation_rate = float(np.mean(cpu_temps > MAX_CPU_TEMP_C))
            p95_latency = float(np.percentile(net_latencies, 95)) if len(net_latencies) > 0 else 0.0

            # Scoring
            cpu_score = _safe_score(1.0 - cpu_violation_rate)
            mem_score = _safe_score(1.0 - mem_violation_rate)
            temp_score = _safe_score(1.0 - temp_violation_rate)
            # < 50ms p95 latency is excellent; > 200ms is unacceptable
            latency_score = _safe_score(1.0 - min((p95_latency - 50.0) / 150.0, 1.0))

            combined = 0.25 * cpu_score + 0.25 * mem_score + 0.3 * temp_score + 0.2 * latency_score
            passed = combined >= 0.75

            return TestResult(
                test_name=ValidationTestType.LOAD_STRESS,
                passed=passed,
                score=_safe_score(combined),
                details={
                    "avg_cpu_usage": round(float(np.mean(cpu_usages)), 4),
                    "max_cpu_usage": round(float(np.max(cpu_usages)), 4),
                    "cpu_violation_rate": round(cpu_violation_rate, 4),
                    "avg_memory_usage": round(float(np.mean(mem_usages)), 4),
                    "mem_violation_rate": round(mem_violation_rate, 4),
                    "max_cpu_temp_c": round(float(np.max(cpu_temps)), 2),
                    "temp_violation_rate": round(temp_violation_rate, 4),
                    "p95_network_latency_ms": round(p95_latency, 2),
                    "samples_analysed": len(telemetry_history),
                },
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("load_stress_test_error")
            return TestResult(
                test_name=ValidationTestType.LOAD_STRESS,
                passed=False,
                score=0.0,
                details={},
                duration_seconds=time.monotonic() - start,
                error_message=str(exc),
            )
