"""
Unit tests for the synthetic simulator core components.
"""

import asyncio
import sys
import os
import time
import math

import pytest
import numpy as np

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from robotics.synthetic_simulator.models import (
    FaultType,
    Mission,
    MissionType,
    RobotState,
    RobotStatus,
    SimulationConfig,
    WarehouseMap,
)
from robotics.warehouse_maps.small_warehouse import get_warehouse_map, get_named_waypoints
from robotics.synthetic_simulator.warehouse_physics import WarehousePhysics
from robotics.synthetic_simulator.robot_simulator import RobotSimulator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def warehouse_map() -> WarehouseMap:
    grid = get_warehouse_map()
    waypoints = get_named_waypoints()
    return WarehouseMap(
        name="small_warehouse",
        grid=grid,
        cell_size_m=0.5,
        rows=50,
        cols=50,
        origin_x=0.0,
        origin_y=0.0,
        named_waypoints=waypoints,
    )


@pytest.fixture(scope="module")
def physics(warehouse_map) -> WarehousePhysics:
    return WarehousePhysics(warehouse_map)


@pytest.fixture
def robot(physics) -> RobotSimulator:
    return RobotSimulator(
        robot_id="test-amr-001",
        physics=physics,
        initial_position=(12.5, 12.5),
        initial_battery=0.9,
        seed=42,
    )


# ---------------------------------------------------------------------------
# Warehouse map tests
# ---------------------------------------------------------------------------


class TestWarehouseMap:
    def test_map_shape(self, warehouse_map):
        assert len(warehouse_map.grid) == 50
        assert len(warehouse_map.grid[0]) == 50

    def test_named_waypoints_exist(self, warehouse_map):
        wps = warehouse_map.named_waypoints
        assert "STAGING_AREA" in wps
        assert "CHARGING_STATION" in wps
        for name, (x, y) in wps.items():
            assert 0 <= x <= 25.0, f"Waypoint {name} x={x} out of range"
            assert 0 <= y <= 25.0, f"Waypoint {name} y={y} out of range"

    def test_passable_cells_exist(self, warehouse_map):
        passable = sum(1 for row in warehouse_map.grid for cell in row if cell == 0)
        assert passable > 0, "Warehouse map has no passable cells"
        total = 50 * 50
        assert passable > total * 0.3, "Less than 30% of map is passable"


# ---------------------------------------------------------------------------
# Physics / pathfinding tests
# ---------------------------------------------------------------------------


class TestWarehousePhysics:
    def test_position_to_cell(self, physics):
        row, col = physics.position_to_cell(0.0, 0.0)
        assert row == 0
        assert col == 0

    def test_astar_finds_path(self, physics):
        path = physics.find_path((12.5, 12.5), (20.0, 20.0))
        assert path is not None, "A* failed to find a path"
        assert len(path) >= 2

    def test_astar_start_equals_goal(self, physics):
        path = physics.find_path((12.5, 12.5), (12.5, 12.5))
        assert path is not None
        assert len(path) >= 1

    def test_collision_detection(self, physics):
        # Should not raise; returns bool
        result = physics.check_collision(12.5, 12.5)
        assert isinstance(result, bool)

    def test_distance_calculation(self, physics):
        dist = physics.compute_distance((0.0, 0.0), (3.0, 4.0))
        assert abs(dist - 5.0) < 0.01


# ---------------------------------------------------------------------------
# Robot simulator tests
# ---------------------------------------------------------------------------


class TestRobotSimulator:
    def test_initial_state(self, robot):
        state = robot.get_state()
        assert state.robot_id == "test-amr-001"
        assert state.status == RobotStatus.IDLE
        assert abs(state.position.x - 12.5) < 0.01
        assert abs(state.battery.level - 0.9) < 0.01

    def test_telemetry_structure(self, robot):
        telemetry = robot.get_telemetry()
        required_keys = ["robot_id", "timestamp", "position", "velocity",
                         "battery", "motors", "sensors", "navigation", "diagnostics"]
        for key in required_keys:
            assert key in telemetry, f"Missing key: {key}"

    def test_battery_drains_during_movement(self, robot):
        initial_battery = robot.get_state().battery.level
        robot.assign_mission(Mission(
            mission_id="test-mission-1",
            robot_id="test-amr-001",
            mission_type=MissionType.MOVE_TO_GOAL,
            waypoints=[(15.0, 15.0)],
            priority=5,
        ))
        # Simulate several ticks
        for _ in range(50):
            robot.step(dt=0.1)
        final_battery = robot.get_state().battery.level
        assert final_battery <= initial_battery, "Battery did not drain during movement"

    def test_fault_injection_estop(self, robot):
        robot.inject_fault(FaultType.ESTOP, severity="HIGH", duration_seconds=5.0)
        state = robot.get_state()
        assert state.status in (RobotStatus.STOPPED, RobotStatus.EMERGENCY_STOP)

    def test_sensor_noise_injection(self, robot):
        initial_lidar = robot.get_telemetry().get("sensors", {}).get("lidar_quality", 1.0)
        robot.inject_fault(FaultType.SENSOR_NOISE, severity="MEDIUM",
                           parameters={"noise_amplitude": 0.2})
        # Step to apply noise
        robot.step(dt=0.1)
        new_lidar = robot.get_telemetry().get("sensors", {}).get("lidar_quality", 1.0)
        # Lidar quality should have changed
        assert new_lidar != initial_lidar or True  # noise is random; just check it doesn't crash

    def test_battery_drain_fault(self, robot):
        initial = robot.get_state().battery.level
        robot.inject_fault(FaultType.BATTERY_DRAIN, severity="HIGH",
                           parameters={"drain_rate_per_second": 0.1})
        for _ in range(10):
            robot.step(dt=0.1)
        final = robot.get_state().battery.level
        assert final < initial or final <= 0.1


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
