"""
AWS RoboMaker Small Warehouse map definition.

Layout (50×50 grid, 0.5 m/cell → 25 m × 25 m total):

  North wall (row 0)
  South wall (row 49) — loading dock area
  West wall  (col 0)
  East wall  (col 49)

  Zones (approximate):
  - Loading docks: south end (rows 44-48, cols 5-44)
  - Shelf rows: 5 parallel shelving rows (rows 10-38)
  - Charging stations: 4 corners (inset)
  - Pickup zones: south-central area
  - Dropoff zones: near loading docks
  - Congestion zones: aisles immediately adjacent to shelves

  Cell encoding:
    0 = FREE
    1 = WALL
    2 = SHELF
    3 = CHARGING_STATION
    4 = PICKUP_ZONE
    5 = DROPOFF_ZONE
    6 = CONGESTION_ZONE
"""

from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# Grid dimensions
# ---------------------------------------------------------------------------

ROWS = 50
COLS = 50
CELL_SIZE_M = 0.5          # 0.5 m per cell
MAP_WIDTH_M = COLS * CELL_SIZE_M   # 25 m
MAP_HEIGHT_M = ROWS * CELL_SIZE_M  # 25 m

# ---------------------------------------------------------------------------
# Cell type constants (mirror CellType enum values)
# ---------------------------------------------------------------------------

FREE = 0
WALL = 1
SHELF = 2
CHARGING_STATION = 3
PICKUP_ZONE = 4
DROPOFF_ZONE = 5
CONGESTION_ZONE = 6


def _build_grid() -> np.ndarray:
    """Construct the warehouse grid as a 50×50 numpy array."""
    grid = np.zeros((ROWS, COLS), dtype=np.uint8)

    # ------------------------------------------------------------------
    # Outer walls (perimeter)
    # ------------------------------------------------------------------
    grid[0, :] = WALL          # North wall
    grid[ROWS - 1, :] = WALL   # South wall
    grid[:, 0] = WALL          # West wall
    grid[:, COLS - 1] = WALL   # East wall

    # ------------------------------------------------------------------
    # Charging stations — inset 2 cells from each corner
    # ------------------------------------------------------------------
    # NW corner
    grid[2:4, 2:4] = CHARGING_STATION
    # NE corner
    grid[2:4, COLS - 4 : COLS - 2] = CHARGING_STATION
    # SW corner (near loading dock)
    grid[ROWS - 4 : ROWS - 2, 2:4] = CHARGING_STATION
    # SE corner (near loading dock)
    grid[ROWS - 4 : ROWS - 2, COLS - 4 : COLS - 2] = CHARGING_STATION

    # ------------------------------------------------------------------
    # Shelving rows — 5 rows of shelves, each 2 cells deep × 18 cells wide
    # Rows are centred between rows 10–38, spaced ~5 rows apart
    # Each shelf row leaves 2-cell-wide aisles on each side
    # ------------------------------------------------------------------
    shelf_row_starts = [10, 16, 22, 28, 34]
    shelf_col_start = 5
    shelf_col_end = 45    # exclusive
    shelf_depth = 2       # cells (1 m deep)

    for sr in shelf_row_starts:
        grid[sr : sr + shelf_depth, shelf_col_start:shelf_col_end] = SHELF
        # Congestion zones on both aisles (1 cell wide each side of shelf)
        if sr - 1 >= 1:
            grid[sr - 1, shelf_col_start:shelf_col_end] = CONGESTION_ZONE
        if sr + shelf_depth < ROWS - 1:
            grid[sr + shelf_depth, shelf_col_start:shelf_col_end] = CONGESTION_ZONE

    # ------------------------------------------------------------------
    # Pickup zones — 3 zones, south-central area
    # ------------------------------------------------------------------
    # PICKUP_ZONE_1
    grid[41:43, 8:14] = PICKUP_ZONE
    # PICKUP_ZONE_2
    grid[41:43, 21:27] = PICKUP_ZONE
    # PICKUP_ZONE_3
    grid[41:43, 34:40] = PICKUP_ZONE

    # ------------------------------------------------------------------
    # Dropoff zones — 2 zones, just north of south wall
    # ------------------------------------------------------------------
    # DROPOFF_ZONE_1
    grid[46:48, 10:18] = DROPOFF_ZONE
    # DROPOFF_ZONE_2
    grid[46:48, 30:38] = DROPOFF_ZONE

    # ------------------------------------------------------------------
    # Cross-aisle walls — vertical dividers between shelf rows and
    # the east/west main aisles to create a realistic warehouse layout
    # ------------------------------------------------------------------
    # Nothing to add here — the main aisles remain open

    return grid


# ---------------------------------------------------------------------------
# Named waypoints (world coordinates, cell centres)
# ---------------------------------------------------------------------------

def _cell_centre(row: int, col: int) -> tuple[float, float]:
    """Convert grid (row, col) to world (x, y) coordinates (cell centre)."""
    x = (col + 0.5) * CELL_SIZE_M
    y = (row + 0.5) * CELL_SIZE_M
    return (x, y)


_NAMED_WAYPOINTS: dict[str, tuple[float, float]] = {
    # Charging docks
    "DOCK_A": _cell_centre(2, 2),     # NW charging station
    "DOCK_B": _cell_centre(2, 47),    # NE charging station
    "DOCK_C": _cell_centre(47, 2),    # SW charging station (near loading)
    "DOCK_D": _cell_centre(47, 47),   # SE charging station (near loading)

    # Shelf row access points (west-end aisle entry, clear of congestion)
    "SHELF_ROW_1_WEST": _cell_centre(12, 4),
    "SHELF_ROW_1_EAST": _cell_centre(12, 45),
    "SHELF_ROW_2_WEST": _cell_centre(18, 4),
    "SHELF_ROW_2_EAST": _cell_centre(18, 45),
    "SHELF_ROW_3_WEST": _cell_centre(24, 4),
    "SHELF_ROW_3_EAST": _cell_centre(24, 45),
    "SHELF_ROW_4_WEST": _cell_centre(30, 4),
    "SHELF_ROW_4_EAST": _cell_centre(30, 45),
    "SHELF_ROW_5_WEST": _cell_centre(36, 4),
    "SHELF_ROW_5_EAST": _cell_centre(36, 45),

    # Pickup zones (centre of each zone)
    "PICKUP_ZONE_1": _cell_centre(42, 11),
    "PICKUP_ZONE_2": _cell_centre(42, 24),
    "PICKUP_ZONE_3": _cell_centre(42, 37),

    # Dropoff zones (centre of each zone)
    "DROPOFF_ZONE_1": _cell_centre(47, 14),
    "DROPOFF_ZONE_2": _cell_centre(47, 34),

    # Main corridor intersections
    "CORRIDOR_MAIN_NORTH": _cell_centre(5, 25),
    "CORRIDOR_MAIN_SOUTH": _cell_centre(40, 25),
    "CORRIDOR_MAIN_CENTER": _cell_centre(24, 25),

    # Loading dock entry (south)
    "LOADING_DOCK_ENTRY": _cell_centre(44, 25),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_CACHED_GRID: np.ndarray | None = None


def get_warehouse_map() -> np.ndarray:
    """
    Return the 50×50 warehouse grid as a numpy uint8 array.

    Encoding:
        0 = FREE, 1 = WALL, 2 = SHELF, 3 = CHARGING_STATION,
        4 = PICKUP_ZONE, 5 = DROPOFF_ZONE, 6 = CONGESTION_ZONE
    """
    global _CACHED_GRID
    if _CACHED_GRID is None:
        _CACHED_GRID = _build_grid()
    return _CACHED_GRID.copy()


def get_named_waypoints() -> dict[str, tuple[float, float]]:
    """
    Return the dict of named waypoints.

    Each value is an (x, y) tuple in world-space metres, measured from the
    south-west corner of the warehouse (origin at (0, 0)).
    """
    return dict(_NAMED_WAYPOINTS)


def get_map_info() -> dict:
    """Return metadata about the map."""
    return {
        "name": "small_warehouse",
        "rows": ROWS,
        "cols": COLS,
        "cell_size_m": CELL_SIZE_M,
        "width_m": MAP_WIDTH_M,
        "height_m": MAP_HEIGHT_M,
        "origin_x": 0.0,
        "origin_y": 0.0,
        "num_waypoints": len(_NAMED_WAYPOINTS),
    }


def get_free_spawn_positions(n: int = 10) -> list[tuple[float, float]]:
    """
    Return up to n free-space positions suitable for spawning robots.
    Positions are evenly distributed across the main aisle.
    """
    grid = get_warehouse_map()
    positions = []
    # Main north-south aisle (col 25, rows 5-40)
    aisle_rows = range(5, 40, max(1, 35 // n))
    for row in aisle_rows:
        if len(positions) >= n:
            break
        if grid[row, 25] == FREE:
            positions.append(_cell_centre(row, 25))
    return positions[:n]
