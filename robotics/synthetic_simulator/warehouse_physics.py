"""
WarehousePhysics: grid-based warehouse environment with A* pathfinding,
collision detection, and congestion modelling.

Designed to be fully async-compatible — all CPU-bound work is synchronous
but can be wrapped with asyncio.to_thread() by callers.
"""

from __future__ import annotations

import heapq
import math
from typing import Optional

import numpy as np

from .models import CellType, WarehouseMap


# ---------------------------------------------------------------------------
# Cost table per cell type  (multiplier applied to movement cost)
# ---------------------------------------------------------------------------

CELL_COST: dict[int, float] = {
    CellType.FREE: 1.0,
    CellType.WALL: math.inf,  # impassable
    CellType.SHELF: math.inf,  # impassable
    CellType.CHARGING_STATION: 1.2,
    CellType.PICKUP_ZONE: 1.1,
    CellType.DROPOFF_ZONE: 1.1,
    CellType.CONGESTION_ZONE: 3.0,  # slow-down area
}


# ---------------------------------------------------------------------------
# Helper: heuristic
# ---------------------------------------------------------------------------


def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Octile distance heuristic for 8-connected grid."""
    dr = abs(a[0] - b[0])
    dc = abs(a[1] - b[1])
    return max(dr, dc) + (math.sqrt(2) - 1) * min(dr, dc)


# ---------------------------------------------------------------------------
# WarehousePhysics
# ---------------------------------------------------------------------------


class WarehousePhysics:
    """
    Grid-based physics engine for a warehouse environment.

    Responsibilities:
    - Load and expose a WarehouseMap.
    - Provide A* pathfinding between any two world-coordinate points.
    - Detect collisions against static obstacles.
    - Model congestion (speed multiplier per cell).
    - Return smooth waypoint lists for robot navigation.
    """

    def __init__(self, warehouse_map: WarehouseMap) -> None:
        self._map = warehouse_map
        self._grid = warehouse_map.grid  # numpy ndarray (rows, cols)
        self._rows = warehouse_map.rows
        self._cols = warehouse_map.cols
        self._cell_size = warehouse_map.cell_size_m

        # Precompute cost grid as float32 for fast lookups
        self._cost_grid: np.ndarray = self._build_cost_grid()

        # 8-directional movement vectors (dr, dc) and their euclidean cost
        self._neighbours: list[tuple[int, int, float]] = [
            (-1, 0, 1.0),
            (1, 0, 1.0),
            (0, -1, 1.0),
            (0, 1, 1.0),
            (-1, -1, math.sqrt(2)),
            (-1, 1, math.sqrt(2)),
            (1, -1, math.sqrt(2)),
            (1, 1, math.sqrt(2)),
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def map(self) -> WarehouseMap:
        return self._map

    def find_path(
        self,
        start_x: float,
        start_y: float,
        goal_x: float,
        goal_y: float,
        smooth: bool = True,
    ) -> list[tuple[float, float]]:
        """
        Compute a collision-free path from (start_x, start_y) to (goal_x, goal_y).

        Returns a list of (x, y) world-coordinate waypoints including the
        start and goal positions.  Returns an empty list if no path exists.
        """
        start_cell = self._map.world_to_grid(start_x, start_y)
        goal_cell = self._map.world_to_grid(goal_x, goal_y)

        if not self._is_traversable(*start_cell):
            start_cell = self._find_nearest_free(start_cell)
        if not self._is_traversable(*goal_cell):
            goal_cell = self._find_nearest_free(goal_cell)

        if start_cell is None or goal_cell is None:
            return []

        if start_cell == goal_cell:
            return [(start_x, start_y)]

        grid_path = self._astar(start_cell, goal_cell)
        if not grid_path:
            return []

        # Convert grid cells back to world coordinates (cell centres)
        world_path: list[tuple[float, float]] = [
            self._map.grid_to_world(r, c) for r, c in grid_path
        ]

        # Replace first/last with exact world coords
        world_path[0] = (start_x, start_y)
        world_path[-1] = (goal_x, goal_y)

        if smooth:
            world_path = self._smooth_path(world_path)

        return world_path

    def is_collision_free(self, x: float, y: float) -> bool:
        """Return True if (x, y) is not inside a static obstacle."""
        row, col = self._map.world_to_grid(x, y)
        return self._is_traversable(row, col)

    def segment_collision_free(
        self, x0: float, y0: float, x1: float, y1: float, samples: int = 10
    ) -> bool:
        """
        Check whether the straight-line segment from (x0,y0) to (x1,y1)
        is free of obstacles by sampling along the line.
        """
        for i in range(samples + 1):
            t = i / samples
            x = x0 + t * (x1 - x0)
            y = y0 + t * (y1 - y0)
            if not self.is_collision_free(x, y):
                return False
        return True

    def speed_multiplier_at(self, x: float, y: float) -> float:
        """
        Return a speed multiplier [0, 1] for the cell at (x, y).
        Congestion zones return lower values; free space returns 1.0.
        """
        row, col = self._map.world_to_grid(x, y)
        cell = int(self._grid[row, col])
        if cell == CellType.CONGESTION_ZONE:
            return 0.4
        if cell in (CellType.PICKUP_ZONE, CellType.DROPOFF_ZONE):
            return 0.6
        if cell == CellType.CHARGING_STATION:
            return 0.5
        return 1.0

    def lidar_quality_at(self, x: float, y: float, radius_m: float = 2.0) -> float:
        """
        Estimate LiDAR quality [0, 1] based on wall/shelf density within
        a circular neighbourhood of radius_m.  Reflects are dense close to
        shelves, reducing effective scan quality.
        """
        row, col = self._map.world_to_grid(x, y)
        r_cells = max(1, int(radius_m / self._cell_size))

        r_min = max(0, row - r_cells)
        r_max = min(self._rows - 1, row + r_cells)
        c_min = max(0, col - r_cells)
        c_max = min(self._cols - 1, col + r_cells)

        patch = self._grid[r_min : r_max + 1, c_min : c_max + 1]
        total = patch.size
        if total == 0:
            return 1.0

        obstacle_count = int(np.sum((patch == CellType.WALL) | (patch == CellType.SHELF)))
        obstacle_ratio = obstacle_count / total
        # Quality degrades linearly: fully open → 1.0, 30%+ blocked → 0.5
        quality = 1.0 - 0.5 * min(obstacle_ratio / 0.3, 1.0)
        return float(quality)

    def path_length(self, waypoints: list[tuple[float, float]]) -> float:
        """Compute total Euclidean length of a waypoint path in metres."""
        if len(waypoints) < 2:
            return 0.0
        total = 0.0
        for (x0, y0), (x1, y1) in zip(waypoints[:-1], waypoints[1:]):
            total += math.hypot(x1 - x0, y1 - y0)
        return total

    def get_named_waypoints(self) -> dict[str, tuple[float, float]]:
        return dict(self._map.named_waypoints)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_cost_grid(self) -> np.ndarray:
        """Build a float32 cost grid from the integer cell-type grid."""
        cost = np.full((self._rows, self._cols), 1.0, dtype=np.float32)
        for cell_type, cell_cost in CELL_COST.items():
            mask = self._grid == cell_type
            cost[mask] = cell_cost
        return cost

    def _is_traversable(self, row: int, col: int) -> bool:
        if not (0 <= row < self._rows and 0 <= col < self._cols):
            return False
        return self._cost_grid[row, col] < math.inf

    def _find_nearest_free(
        self, cell: tuple[int, int], max_radius: int = 5
    ) -> Optional[tuple[int, int]]:
        """BFS to find the nearest traversable cell to 'cell'."""
        from collections import deque

        visited: set[tuple[int, int]] = set()
        queue: deque[tuple[int, int]] = deque([cell])
        visited.add(cell)

        while queue:
            r, c = queue.popleft()
            if self._is_traversable(r, c):
                return (r, c)
            for dr, dc, _ in self._neighbours:
                nr, nc = r + dr, c + dc
                if (
                    0 <= nr < self._rows
                    and 0 <= nc < self._cols
                    and (nr, nc) not in visited
                    and abs(nr - cell[0]) <= max_radius
                    and abs(nc - cell[1]) <= max_radius
                ):
                    visited.add((nr, nc))
                    queue.append((nr, nc))
        return None

    def _astar(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
    ) -> list[tuple[int, int]]:
        """
        A* on the 8-connected cost grid.
        Returns ordered list of (row, col) from start (inclusive) to goal (inclusive).
        """
        open_heap: list[tuple[float, tuple[int, int]]] = []
        heapq.heappush(open_heap, (0.0, start))

        came_from: dict[tuple[int, int], Optional[tuple[int, int]]] = {start: None}
        g_score: dict[tuple[int, int], float] = {start: 0.0}

        while open_heap:
            _, current = heapq.heappop(open_heap)

            if current == goal:
                return self._reconstruct_path(came_from, current)

            cr, cc = current
            for dr, dc, move_cost in self._neighbours:
                nr, nc = cr + dr, cc + dc
                if not (0 <= nr < self._rows and 0 <= nc < self._cols):
                    continue
                cell_cost = float(self._cost_grid[nr, nc])
                if cell_cost == math.inf:
                    continue

                tentative_g = g_score[current] + move_cost * cell_cost
                neighbour = (nr, nc)
                if tentative_g < g_score.get(neighbour, math.inf):
                    came_from[neighbour] = current
                    g_score[neighbour] = tentative_g
                    f = tentative_g + _heuristic(neighbour, goal)
                    heapq.heappush(open_heap, (f, neighbour))

        return []  # no path

    def _reconstruct_path(
        self,
        came_from: dict[tuple[int, int], Optional[tuple[int, int]]],
        current: tuple[int, int],
    ) -> list[tuple[int, int]]:
        path: list[tuple[int, int]] = []
        node: Optional[tuple[int, int]] = current
        while node is not None:
            path.append(node)
            node = came_from[node]
        path.reverse()
        return path

    def _smooth_path(
        self, waypoints: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """
        Greedy line-of-sight path smoothing (funnel-like).
        Removes intermediate waypoints that can be skipped without collisions.
        """
        if len(waypoints) <= 2:
            return waypoints

        smoothed = [waypoints[0]]
        anchor_idx = 0

        i = 2
        while i < len(waypoints):
            ax, ay = smoothed[-1]
            gx, gy = waypoints[i]
            if self.segment_collision_free(ax, ay, gx, gy, samples=12):
                # Can skip waypoints[i-1]; continue reaching further
                i += 1
            else:
                # Must include waypoints[i-1]
                smoothed.append(waypoints[i - 1])
                anchor_idx = i - 1
                i += 1

        smoothed.append(waypoints[-1])
        return smoothed
