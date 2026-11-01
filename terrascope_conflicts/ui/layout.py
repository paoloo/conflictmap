"""Box drawing, layout, and text helpers (Terrascope ui/layout.py conventions)."""
from __future__ import annotations

import curses

from ..core.config import UI_CONFIG

_BOX = {
    "top_left": "┌", "top_right": "┐", "bottom_left": "└", "bottom_right": "┘",
    "horizontal": "─", "vertical": "│",
}

OUTER_MARGIN = tuple(UI_CONFIG["outer_margin"])
PANEL_HEIGHT = int(UI_CONFIG["panel_height"])
PANEL_MIN_HEIGHT = int(UI_CONFIG["panel_min_height"])
PANEL_MIN_MAP_HEIGHT = int(UI_CONFIG["panel_min_map_height"])
MAP_EDGE_PAD_X = int(UI_CONFIG["map_edge_pad_x"])
MAP_EDGE_PAD_Y = max(0, int(UI_CONFIG["map_edge_pad_y"])) + 1

Rect = tuple[int, int, int, int]


def safe_addstr(window, y: int, x: int, text: str, attributes: int = 0) -> None:
    try:
        window.addnstr(y, x, text, max(0, window.getmaxyx()[1] - x - 1), attributes)
    except curses.error:
        pass


def draw_box(window, top: int, left: int, height: int, width: int, title: str) -> None:
    if height < 2 or width < 2:
        return
    horizontal = _BOX["horizontal"] * (width - 2)
    safe_addstr(window, top, left, _BOX["top_left"] + horizontal + _BOX["top_right"])
    for row in range(top + 1, top + height - 1):
        safe_addstr(window, row, left, _BOX["vertical"])
        safe_addstr(window, row, left + width - 1, _BOX["vertical"])
    safe_addstr(window, top + height - 1, left,
                _BOX["bottom_left"] + horizontal + _BOX["bottom_right"])
    if title:
        title_text = f" {title} "
        if len(title_text) < width - 2:
            safe_addstr(window, top, left + 2, title_text)


def compute_layout(height: int, width: int, panel_visible: bool) -> tuple[Rect, Rect | None]:
    """Split the terminal into a map box and an optional bottom panel.

    Returns ((top, left, height, width) map rect, panel rect or None)."""
    margin_x, margin_y = OUTER_MARGIN
    box_width = width - margin_x * 2
    map_top = margin_y
    total_height = height - margin_y * 2
    panel_height = 0
    if panel_visible:
        panel_height = min(PANEL_HEIGHT, total_height - PANEL_MIN_MAP_HEIGHT)
        if panel_height < PANEL_MIN_HEIGHT:
            panel_height = 0
    map_height = total_height - panel_height
    map_rect = (map_top, margin_x, map_height, box_width)
    if panel_height == 0:
        return map_rect, None
    panel_rect = (map_top + map_height, margin_x, panel_height, box_width)
    return map_rect, panel_rect
