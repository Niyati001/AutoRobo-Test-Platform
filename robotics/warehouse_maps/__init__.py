"""
warehouse_maps package.

Provides factory functions for warehouse grid data and named waypoints.
"""

from .small_warehouse import (
    get_free_spawn_positions,
    get_map_info,
    get_named_waypoints,
    get_warehouse_map,
)

__all__ = [
    "get_warehouse_map",
    "get_named_waypoints",
    "get_map_info",
    "get_free_spawn_positions",
]
