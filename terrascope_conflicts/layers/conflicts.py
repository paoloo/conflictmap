"""Conflict layer: the world-conflict heatmap + aggression points + UCDP dots.

The heatmap emulates Terrascope's radar/day-night overlays — three techniques
composed, exactly as in the original repo:

  * bg_cells  — per-cell background tint (Terrascope's night "tint" look and
    radar's solid cells): every cell inside a conflict's intensity disc gets
    that intensity's colour as its background, so borders, labels and markers
    drawn there sit ON the heat colour;
  * Marker(bg_color=...) — half-cell shading (Terrascope's radar uses ▀/▄
    half blocks with fg/bg from two readings; here the seam between adjacent
    intensity levels is traced with ▀/▄ markers so the boundary reads smooth);
  * draw_layer_symbols lines — nothing; heat is a background phenomenon, like
    the night tint (lines stay reserved for link lines from the links layer).

Intensity of a conflict = situation_level/5 x confidence (0..1), quantised to
HEATMAP_STEPS colours; radius grows with UCDP fatalities, so the map reads
like the Qt app's diamonds+dots but as a continuous threat surface. The
aggression switch removes the whole overlay (points, dots AND heat).
"""
from __future__ import annotations

import math
import time

import numpy as np

from ..core.config import (HEATMAP_STEPS, HEATMAP_STOPS, LEVEL_COLORS,
                           TREND_ARROW, UI_CONFIG)
from ..core.layer import Layer, LayerRender, Marker, SidebarLine
from ..core.view import BLANK_BRAILLE, View

HALF_BLOCK_UPPER = "\u2580"
HALF_BLOCK_LOWER = "\u2584"
KM_PER_DEGREE = 111.32


def _interpolate_palette(stops, fraction: float) -> str:
    fraction = max(0.0, min(1.0, fraction))
    for (f0, c0), (f1, c1) in zip(stops, stops[1:]):
        if fraction <= f1:
            span = f1 - f0
            local = 0.0 if span <= 0 else (fraction - f0) / span
            from ..ui.colors import hex_to_rgb
            r0, g0, b0 = hex_to_rgb(c0)
            r1, g1, b1 = hex_to_rgb(c1)
            return "#{:02x}{:02x}{:02x}".format(
                round(r0 + (r1 - r0) * local),
                round(g0 + (g1 - g0) * local),
                round(b0 + (b1 - b0) * local))
    return stops[-1][1]


# One colour per intensity level (lowest = first stop). Only these are drawn.
HEATMAP_COLORS = [
    _interpolate_palette(HEATMAP_STOPS, index / (HEATMAP_STEPS - 1))
    for index in range(HEATMAP_STEPS)
]


def heat_level(fraction: float) -> int:
    """Raw intensity fraction (0..1) -> level index 0..HEATMAP_STEPS-1."""
    return max(0, min(HEATMAP_STEPS - 1, int(fraction * HEATMAP_STEPS)))


class ConflictPoint:
    """One ranked conflict as a temporary aggression point (Qt parity)."""

    def __init__(self, conflict: dict, ttl_hours: float, min_level: int) -> None:
        self.key = conflict["key"]
        self.label = conflict["conflict"]
        self.level = max(0, min(5, int(conflict["situation_level"])))
        self.confidence = float(conflict.get("confidence") or 0.0)
        self.trend = conflict.get("trend") or "stable"
        self.rank = int(conflict.get("rank") or 0)
        self.summary = conflict.get("summary") or ""
        self.lat = conflict.get("lat")
        self.lon = conflict.get("lon")
        self.fatalities = int(conflict.get("fatalities") or 0)
        assessed = conflict.get("assessed_at") or ""
        try:
            from datetime import datetime
            self.assessed_epoch = datetime.fromisoformat(
                assessed.replace("Z", "+00:00")).timestamp()
        except ValueError:
            self.assessed_epoch = time.time()
        self.expires = self.assessed_epoch + ttl_hours * 3600
        self.expired = ttl_hours > 0 and time.time() >= self.expires
        self.visible = (not self.expired and self.level >= min_level
                        and self.lat is not None and self.lon is not None)

    @property
    def intensity(self) -> float:
        """0..1 heat value: level/5 scaled by the model's confidence."""
        return (self.level / 5.0) * (0.35 + 0.65 * self.confidence)

    @property
    def radius_deg(self) -> float:
        """Heat disc radius in degrees, grown by UCDP fatalities (log scale)."""
        base = 3.0 + 2.0 * self.level
        return base + min(6.0, 1.2 * math.log10(1 + max(0, self.fatalities)))

    def diamond_glyph(self) -> str:
        return "\u25c6"

    def tooltip(self) -> str:
        head = f"#{self.rank} " if self.rank else ""
        return (f"{head}{self.label} — level {self.level} {self.trend}\n"
                f"{self.summary[:300]}")


class ConflictLayer(Layer):
    """Owns conflicts + UCDP events + the single aggression switch."""
    name = "conflicts"
    toggle_key = "a"
    info_title = "CONFLICT"

    def __init__(self) -> None:
        super().__init__()
        self.conflicts: list[ConflictPoint] = []
        self.events: list[dict] = []
        self.show_heat = True          # toggle key 'x': heatmap on/off
        self.show_events = True
        self.assessed_at = ""
        self.ranked_by = ""

    # -- data -------------------------------------------------------------

    def set_data(self, data: dict | None, ttl_hours: float, min_level: int) -> None:
        self.conflicts = []
        if data:
            self.assessed_at = data.get("assessed_at") or ""
            self.ranked_by = data.get("ranked_by") or ""
            self.conflicts = [ConflictPoint(c, ttl_hours, min_level)
                              for c in data.get("conflicts", [])]
        self.dirty = True

    def set_events(self, events: list[dict]) -> None:
        self.events = events
        self.dirty = True

    @property
    def enabled_points(self) -> list[ConflictPoint]:
        return [c for c in self.conflicts if self.enabled and c.visible]

    def visible_conflicts(self) -> list[ConflictPoint]:
        return self.enabled_points

    def handle_key(self, key: int) -> bool:
        if super().handle_key(key):
            return True
        if 0 <= key < 256 and chr(key).lower() == "x":
            self.show_heat = not self.show_heat
            self.dirty = True
            return True
        return False

    def press_legend(self, action: str) -> None:
        if action == "heat":
            self.show_heat = not self.show_heat

    # -- rendering ----------------------------------------------------------

    def _heat_field(self, view: View) -> tuple[np.ndarray, np.ndarray]:
        """(levels, strength) per cell: the max intensity over all conflict
        discs covering the cell, like the radar's max-reading quantisation."""
        cols, rows = view.width, view.height
        lons = view.origin_lon + (np.arange(cols) + 0.5) / cols * view.lon_span
        lats = view.origin_lat - (np.arange(rows) + 0.5) / rows * view.lat_span
        strength = np.zeros((rows, cols), dtype=np.float32)
        for point in self.enabled_points:
            intensity = point.intensity
            if intensity <= 0:
                continue
            radius = point.radius_deg
            cos_lat = max(0.15, math.cos(math.radians(point.lat)))
            # Elliptical degree distance (lat degrees are ~111 km everywhere,
            # lon degrees shrink by cos(lat)); anisotropy keeps discs round on
            # the screen where x is degrees*aspect and cells are 2x taller.
            dx = (lons - point.lon) / cos_lat
            dy = lats - point.lat
            gx, gy = np.meshgrid(dx, dy)
            dist = np.hypot(gx, gy)
            inside = dist <= radius
            # Linear falloff from the centre to the edge, like the radar's
            # dBZ gradient: core = full intensity, edge = 0.
            contribution = intensity * np.clip(1.0 - dist / radius, 0.0, 1.0)
            strength = np.where(inside, np.maximum(strength, contribution), strength)
        levels = np.where(strength > 0.02,
                          np.clip((strength * HEATMAP_STEPS).astype(int), 0, HEATMAP_STEPS - 1),
                          -1)
        return levels, strength

    def _heat_markers(self, view: View, levels: np.ndarray,
                      strength: np.ndarray) -> list[Marker]:
        """Half-block seam markers, the radar's two-reading trick turned up a
        notch: where the intensity level differs between vertically adjacent
        cells, the boundary is traced with ▀/▄ in the darker level's colour so
        the seam follows the disc curve instead of a stair-step."""
        markers: list[Marker] = []
        rows, cols = levels.shape
        seam_down = np.zeros((rows, cols), dtype=bool)   # boundary to the cell BELOW
        seam_down[:-1, :] = levels[:-1, :] != levels[1:, :]
        seam_up = np.zeros((rows, cols), dtype=bool)     # boundary to the cell ABOVE
        seam_up[1:, :] = levels[:-1, :] != levels[1:, :]
        for (y, x), has in np.ndenumerate(seam_down):
            if not has or levels[y, x] < 0:
                continue
            other = levels[y + 1, x]
            top, bottom = int(levels[y, x]), int(other) if other >= 0 else int(levels[y, x])
            if top == bottom:
                continue
            dark, light = min(top, bottom), max(top, bottom)
            # ▀ fg=light / bg=dark (the radar's mixed-level cell) keeps both
            # colours in one cell; where the lower cell is empty, only ▄.
            if other >= 0:
                markers.append(Marker(x=x, y=y, color=HEATMAP_COLORS[light],
                                      bg_color=HEATMAP_COLORS[dark],
                                      label=HALF_BLOCK_UPPER, item=None))
            else:
                markers.append(Marker(x=x, y=y, color=HEATMAP_COLORS[dark],
                                      label=HALF_BLOCK_LOWER, item=None))
        for (y, x), has in np.ndenumerate(seam_up):
            if not has or levels[y, x] < 0:
                continue
            other = levels[y - 1, x]
            if other >= 0:
                continue      # already drawn from the upper cell's side
            markers.append(Marker(x=x, y=y, color=HEATMAP_COLORS[int(levels[y, x])],
                                  label=HALF_BLOCK_UPPER, item=None))
        return markers

    def build(self, view: View) -> LayerRender:
        markers: list[Marker] = []
        bg_cells: dict[tuple[int, int], str] = {}
        lines = None
        if self.enabled:
            if self.show_heat and self.enabled_points:
                levels, strength = self._heat_field(view)
                rows, cols = np.nonzero(levels >= 0)
                for y, x in zip(rows.tolist(), cols.tolist()):
                    bg_cells[(x, y)] = HEATMAP_COLORS[int(levels[y, x])]
                markers.extend(self._heat_markers(view, levels, strength))
            if self.show_events:
                for event in self.events:
                    cell = view.to_cell(event.get("lon", 0), event.get("lat", 0))
                    if cell is None:
                        continue
                    try:
                        fatal = int(event.get("fatalities") or 1)
                    except (TypeError, ValueError):
                        fatal = 1
                    glyph = "\u2022" if fatal < 10 else "\u25cf"
                    markers.append(Marker(x=cell[0], y=cell[1], color="#ff5d4d",
                                          glyph=glyph, dim=fatal < 5,
                                          item=("event", event)))
            # Diamonds: rank order so the strongest ends on top; labels under.
            points = sorted(self.enabled_points, key=lambda p: -p.rank)
            for point in points:
                cell = view.to_cell(point.lon, point.lat)
                if cell is None:
                    continue
                x, y = cell
                color = LEVEL_COLORS[point.level]
                selected = (self.selected is not None
                            and isinstance(self.selected, ConflictPoint)
                            and self.selected.key == point.key)
                markers.append(Marker(x=x, y=y, color=color,
                                      glyph=point.diamond_glyph(), item=point,
                                      selected=selected))
                arrow = TREND_ARROW.get(point.trend, "")
                label = f"#{point.rank} {point.label} {arrow}".strip()
                markers.append(Marker(x=x, y=y, color=color, label=label, item=point,
                                      selected=selected))
        return LayerRender(lines, "#000000", markers, bg_cells=bg_cells or None)

    # -- info / legend --------------------------------------------------------

    def info_rows(self) -> list[tuple[str, str]]:
        point = self.selected
        if not isinstance(point, ConflictPoint):
            return []
        rows = [
            ("CONFLICT", f"#{point.rank} {point.label}"),
            ("LEVEL", f"{point.level}/5 ({point.trend})"),
            ("SCORE", f"{point.confidence:.2f} confidence"),
            ("UCDP", f"{point.fatalities} killed (60 d)"),
        ]
        if point.expired:
            rows.append(("STATE", "EXPIRED — refresh"))
        return rows

    def legend_lines(self) -> list[SidebarLine]:
        lines = [SidebarLine("CONFLICT HEAT", swatches=(HEATMAP_COLORS[-2],))]
        lines.append(SidebarLine(
            "HEATMAP", choices=("ON", "OFF"),
            choice_index=0 if self.show_heat else 1, action="heat"))
        lines.append(SidebarLine("UCDP INCIDENT", swatches=("#ff5d4d",),
                                 swatch_glyph="\u25cf"))
        if self.assessed_at:
            lines.append(SidebarLine(f"assessed {self.assessed_at[:16].replace('T', ' ')}Z",
                                     bold=True))
        return lines
