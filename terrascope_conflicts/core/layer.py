"""Layer protocol, ported from Terrascope's core/layer.py (MIT).

A layer produces one LayerRender per frame: optional braille `lines` (drawn in
`line_color` over the map), per-cell background overrides `bg_cells` (the
conflict heatmap), and Marker glyphs/labels (diamonds, links, tracks, nodes).
Data comes from the project's own intel pipeline instead of live APIs, so the
refresh machinery is reduced to what the reload thread needs.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

from .config import APP_CONFIG

CLICK_RADIUS = float(APP_CONFIG["click_radius"])
RETRY_BASE_SECONDS = float(APP_CONFIG["retry_base_seconds"])
RETRY_MAX_SECONDS = float(APP_CONFIG["retry_max_seconds"])


@dataclass
class Marker:
    x: int
    y: int
    color: str
    glyph: str | None = None
    label: str | None = None
    item: object = None
    selected: bool = False
    dim: bool = False
    # Explicit background colour for this one marker (heat cells draw a block
    # on their own colour; None = the map's own background at that cell).
    bg_color: str | None = None


@dataclass
class LayerRender:
    lines: list[str] | None
    line_color: str
    markers: list[Marker] = field(default_factory=list)
    # Background colour overrides, {(x, y): "#rrggbb"}: the map's own
    # background at these cells is replaced (the conflict heatmap tint).
    bg_cells: dict[tuple[int, int], str] | None = None


@dataclass
class SidebarLine:
    """One line of the map legend, right-aligned, swatch at the right end."""
    text: str = ""
    swatches: tuple[str, ...] = ()
    swatch_glyph: str = "\u25a0"
    bold: bool = False
    # Clickable toggle line: "[ A | B ]" with the choice at choice_index lit.
    choices: tuple[str, ...] = ()
    choice_index: int = 0
    action: str | None = None


class Layer:
    name = "layer"
    toggle_key = ""
    enabled = True

    def __init__(self) -> None:
        self.dirty = True
        self.selected = None
        self.last_markers: list[Marker] = []
        self.lock = threading.Lock()

    def tick(self) -> None:
        pass

    def toggle(self) -> None:
        self.enabled = not self.enabled
        self.dirty = True

    def handle_key(self, key: int) -> bool:
        if (self.toggle_key and 0 <= key < 256
                and chr(key).lower() == self.toggle_key.lower()):
            self.toggle()
            return True
        return False

    def render(self, view) -> LayerRender:
        self.dirty = False
        result = self.build(view)
        self.last_markers = result.markers
        return result

    def build(self, view) -> LayerRender:
        raise NotImplementedError

    def hit_test_all(self, x: int, y: int, radius: float | None = None) -> list[tuple[float, object]]:
        if radius is None:
            radius = CLICK_RADIUS
        hits = []
        for marker in self.last_markers:
            distance = math.hypot(marker.x - x, marker.y - y)
            if distance <= radius and marker.item is not None:
                hits.append((distance, marker.item))
        return sorted(hits, key=lambda hit: hit[0])

    def select(self, item) -> None:
        self.selected = item
        self.dirty = True

    def info_rows(self) -> list[tuple[str, str]]:
        return []

    def legend_lines(self) -> list[SidebarLine]:
        return []

    def loading_messages(self) -> list[str]:
        return []
