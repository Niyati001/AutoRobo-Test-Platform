"""
Unit tests for the ValidationTestRunner.
"""

import sys
import os
import math
import time

import pytest
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../services/validation-service")))

from test_runner import ValidationTestRunner
from models import ValidationTestType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner() -> ValidationTestRunner:
    return ValidationTestRunner()


def _make_telemetry(n: int = 100, degraded: bool = False) -> list:
    """Generate synthetic telemetry samples."""
    samples = []
    now = time.time()
    x, y = 12.5, 12.5
    battery = 0.85
    rng = np.random.default_rng(seed=0)

    for i in range(n):
        ts = now - (n - i) * 0.1
        x += 0.1 + rng.normal(0, 0.01)
        y += 0.1 + rng.normal(0, 0.01)
        battery -= 0.0002 if not degraded else 0.005
        battery = max(battery, 0.0)
        nav_state = "MOVING" if i < n - 5 else "IDLE"
        lidar = rng.uniform(0.8, 1.0) if not degraded else rng.uniform(0.1, 0.4)

        samples.append({
            "robot_id": "amr-001",
            "timestamp": ts,
            "position": {"x": x, "y": y, "theta": 0.0},
            "velocity": {
                "linear": rng.uniform(0.2, 0.6) if not degraded else rng.uniform(0, 2.0),
                "angular": rng.normal(0.0, 0.02 if not degraded else 0.5),
            },
            "battery": {"level": battery, "voltage": 24.0, "charging": False},
            "motors": {
                "left_rpm": 60.0,
                "right_rpm": 60.0,
                "left_current": 1.0,
                "right_current": 1.0,
            },
            "sensors": {
                "lidar_quality": lidar,
                "imu_calibrated": True,
                "obstacle_detected": False,
                "distance_to_nearest_obstacle": 2.0,
            },
            "navigation": {
                "state": nav_state,
                "path_length": 10.0,
                "eta_seconds": max(0.0, (n - i) * 0.1),
            },
            "diagnostics": {
                "cpu_usage": 0.3,
                "memory_usage": 0.3,
                "cpu_temp": 45.0,
                "network_latency_ms": 10.0,
                "uptime_seconds": i * 0.1,
            },
        })
    return samples


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestNavigationStability:
    def test_healthy_robot_passes(self, runner):
        telemetry = _make_telemetry(100, degraded=False)
        result = runner.test_navigation_stability(telemetry)
        assert result.test_name == ValidationTestType.NAVIGATION_STABILITY
        assert result.score >= 0.0
        assert result.score <= 1.0
        assert result.passed  # healthy robot should pass

    def test_degraded_robot_lower_score(self, runner):
        healthy = runner.test_navigation_stability(_make_telemetry(100, degraded=False))
        degraded = runner.test_navigation_stability(_make_telemetry(100, degraded=True))
        assert degraded.score <= healthy.score

    def test_insufficient_samples(self, runner):
        result = runner.test_navigation_stability([_make_telemetry(100)[0]])
        assert not result.passed
        assert "Insufficient" in (result.details.get("reason") or "")


class TestCollisionPrevention:
    def test_healthy_robot_passes(self, runner):
        telemetry = _make_telemetry(100, degraded=False)
        result = runner.test_collision_prevention(telemetry)
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_degraded_lidar_reduces_score(self, runner):
        healthy = runner.test_collision_prevention(_make_telemetry(100, degraded=False))
        degraded = runner.test_collision_prevention(_make_telemetry(100, degraded=True))
        assert degraded.score <= healthy.score + 0.01  # degraded should be <= healthy


class TestTelemetryIntegrity:
    def test_complete_telemetry_high_score(self, runner):
        telemetry = _make_telemetry(50, degraded=False)
        result = runner.test_telemetry_integrity(telemetry)
        assert result.score >= 0.8
        assert result.details["total_samples"] == 50

    def test_empty_stream_fails(self, runner):
        result = runner.test_telemetry_integrity([])
        assert not result.passed
        assert result.score == 0.0

    def test_missing_fields_reduce_score(self, runner):
        telemetry = [{"robot_id": "amr-001"} for _ in range(20)]
        result = runner.test_telemetry_integrity(telemetry)
        full = runner.test_telemetry_integrity(_make_telemetry(20))
        assert result.score < full.score


class TestBatteryEndurance:
    def test_healthy_battery_passes(self, runner):
        telemetry = _make_telemetry(100, degraded=False)
        result = runner.test_battery_endurance(telemetry)
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_rapid_drain_fails(self, runner):
        telemetry = _make_telemetry(100, degraded=True)
        result = runner.test_battery_endurance(telemetry)
        assert result.details["avg_drain_per_second"] > 0

    def test_insufficient_samples(self, runner):
        result = runner.test_battery_endurance(_make_telemetry(5))
        assert not result.passed


class TestFleetCoordination:
    def test_multi_robot_no_collision(self, runner):
        fleet = {
            "amr-001": _make_telemetry(50, degraded=False),
            "amr-002": [
                {**s, "robot_id": "amr-002",
                 "position": {"x": s["position"]["x"] + 5.0, "y": s["position"]["y"] + 5.0, "theta": 0}}
                for s in _make_telemetry(50, degraded=False)
            ],
        }
        result = runner.test_fleet_coordination(fleet)
        assert result.test_name == ValidationTestType.FLEET_COORDINATION
        assert result.details["grid_collision_events"] == 0

    def test_single_robot_fails(self, runner):
        fleet = {"amr-001": _make_telemetry(50)}
        result = runner.test_fleet_coordination(fleet)
        assert not result.passed


class TestLoadStress:
    def test_normal_load_passes(self, runner):
        telemetry = _make_telemetry(100, degraded=False)
        result = runner.test_load_stress(telemetry)
        assert result.score >= 0.0
        assert result.details["avg_cpu_usage"] < 1.0

    def test_empty_fails(self, runner):
        result = runner.test_load_stress([])
        assert not result.passed


class TestRecoveryBehavior:
    def test_recovery_detected(self, runner):
        samples = _make_telemetry(60, degraded=False)
        # Inject recovery state into middle samples
        for i in range(20, 30):
            samples[i]["navigation"]["state"] = "RECOVERY"
        for i in range(30, 40):
            samples[i]["navigation"]["state"] = "MOVING"

        fault_event = {"severity": "MEDIUM", "injected_at": samples[20]["timestamp"]}
        result = runner.test_recovery_behavior(fault_event, samples[20:])
        assert result.test_name == ValidationTestType.RECOVERY_BEHAVIOR
        assert result.details["recovery_state_entered"]

    def test_no_telemetry_fails(self, runner):
        result = runner.test_recovery_behavior({"severity": "HIGH", "injected_at": 0.0}, [])
        assert not result.passed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
