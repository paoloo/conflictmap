"""Map state and frame building, ported from Terrascope's core/world.py (MIT).

Same ring rasterisation (PIL outlines + land mask), same antimeridian wrap
handling, same selection flow. Layers render after the base map; the Frame
carries what ui/map.py needs to compose the screen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import ImageDraw

from .config import MAP_CONFIG
from .layer import Layer
from .mapdata import Ring
from .view import WRAP_SHIFTS, View, calculate_viewport, clamp_center, visible

ZOOM_CONFIG = MAP_CONFIG["zoom"]
ZOOM_MIN = float(ZOOM_CONFIG["min"])
ZOOM_MAX = float(ZOOM_CONFIG["max"])
ZOOM_STEP = float(ZOOM_CONFIG["step"])
PAN_STEP = float(ZOOM_CONFIG["pan_step"])

START_LON, START_LAT = clamp_center(0.0, 0.0, *calculate_viewport(ZOOM_MIN))

RingPoints = tuple[Ring, np.ndarray]


@dataclass
class Frame:
    country_lines: list[str]
    layers: list[Any]                 # LayerRender per enabled layer
    lon_span: float
    render_width: int
    background_cache: Any = None      # filled by ui/map.py
    land_cover: Any = None            # np.ndarray (rows, cols) 0..1
    province_lines: list[str] | None = None


def _rings_with_points(rings: list[Ring]) -> list[RingPoints]:
    return [
        (ring, points)
        for ring in rings
        if len(points := np.asarray(ring[0], dtype=np.float64)) >= 2
    ]


def prepare_ring_set(rings: list[Ring]):
    items = _rings_with_points(rings)
    boxes = np.array([ring[1:5] for ring, _ in items], dtype=np.float64).reshape(-1, 4)
    return rings, items, boxes


def _draw_rings(rings: list[RingPoints], view: View, outline: ImageDraw.ImageDraw,
                fill: ImageDraw.ImageDraw | None = None) -> None:
    for ring, points in rings:
        shifts = visible(ring, view.center_lon, view.center_lat, view.lon_span, view.lat_span)
        if not shifts:
            continue
        xy = np.empty((len(points), 2), dtype=np.int64)
        xy[:, 1] = np.rint((view.origin_lat - points[:, 1]) * view.scale_y)
        for shift in shifts:
            xy[:, 0] = np.rint((points[:, 0] + shift - view.origin_lon) * view.scale_x)
            flat = xy.ravel().tolist()
            outline.line(flat, fill=255, width=1)
            if fill is not None:
                fill.polygon(flat, fill=255)


def _visible_rings(tier, view: View) -> list[RingPoints]:
    items, boxes = tier
    if not items:
        return []
    view_min_lon = view.center_lon - view.lon_span / 2
    view_max_lon = view.center_lon + view.lon_span / 2
    view_min_lat = view.center_lat - view.lat_span / 2
    view_max_lat = view.center_lat + view.lat_span / 2
    keep = (boxes[:, 3] >= view_min_lat) & (boxes[:, 2] <= view_max_lat)
    in_lon = np.zeros(len(items), dtype=bool)
    for shift in WRAP_SHIFTS:
        in_lon |= (boxes[:, 1] + shift >= view_min_lon) & (boxes[:, 0] + shift <= view_max_lon)
    return [items[index] for index in np.nonzero(keep & in_lon)[0].tolist()]


class WorldMap:
    def __init__(self, countries: list[Ring], layers: list[Layer]) -> None:
        self.countries = countries
        self.layers = layers
        self.zoom = ZOOM_MIN
        self.center_lon = START_LON
        self.center_lat = START_LAT
        self.dirty = True
        self.aspect = 2.0
        self._selection_candidates: list[tuple[Layer, object]] = []
        self._selection_index = 0

    @property
    def countries(self) -> list[Ring]:
        return self._countries

    @countries.setter
    def countries(self, value: list[Ring]) -> None:
        self._countries = value
        self._rings = _rings_with_points(value)
        boxes = np.array([ring[1:5] for ring, _ in self._rings], dtype=np.float64).reshape(-1, 4)
        self._country_tier = (self._rings, boxes)

    def set_prepared_countries(self, prepared) -> None:
        self._countries, self._rings, boxes = prepared
        self._country_tier = (self._rings, boxes)

    # ---- selection -------------------------------------------------------

    @property
    def selected_layer(self) -> Layer | None:
        if self._selection_candidates:
            return self._selection_candidates[self._selection_index][0]
        return next(
            (l for l in self.layers if l.enabled and l.selected is not None), None
        )

    @property
    def selection_position(self) -> tuple[int, int]:
        count = len(self._selection_candidates)
        return (self._selection_index + 1, count) if count else (0, 0)

    def cycle_selection(self, direction: int) -> None:
        count = len(self._selection_candidates)
        if count < 2:
            return
        self._selection_index = (self._selection_index + direction) % count
        self._activate_selection()

    def _activate_selection(self) -> None:
        for layer in self.layers:
            layer.select(None)
        if self._selection_candidates:
            layer, item = self._selection_candidates[self._selection_index]
            layer.select(item)
        self.dirty = True

    def select_at(self, x: int, y: int) -> None:
        hits = sorted(
            ((distance, layer, item)
             for layer in self.active_layers()
             for distance, item in layer.hit_test_all(x, y)),
            key=lambda hit: hit[0],
        )
        self._selection_candidates = [(layer, item) for _, layer, item in hits]
        self._selection_index = 0
        self._activate_selection()

    def clear_selection(self) -> None:
        self._selection_candidates = []
        self._selection_index = 0
        for layer in self.layers:
            layer.select(None)
        self.dirty = True

    # ---- layers ----------------------------------------------------------

    def active_layers(self) -> list[Layer]:
        return [layer for layer in self.layers if layer.enabled]

    def needs_render(self) -> bool:
        return self.dirty or any(layer.dirty for layer in self.layers)

    # ---- view: pan / zoom / reset -----------------------------------------

    def _clamp_center(self) -> None:
        self.center_lon, self.center_lat = clamp_center(
            self.center_lon, self.center_lat,
            *calculate_viewport(self.zoom, self.aspect))

    def focus_location(self, longitude: float, latitude: float, zoom: float = 8.0) -> None:
        self._set_zoom(max(self.zoom, zoom))
        self.center_lon = longitude
        self.center_lat = latitude
        self._clamp_center()
        self.dirty = True

    def pan(self, lon_direction: int, lat_direction: int) -> None:
        lon_span, lat_span = calculate_viewport(self.zoom, self.aspect)
        self.center_lon += lon_direction * lon_span * PAN_STEP
        self.center_lat += lat_direction * lat_span * PAN_STEP
        self._clamp_center()
        self.dirty = True

    def pan_cells(self, dx: int, dy: int, cols: int, rows: int) -> None:
        if cols <= 0 or rows <= 0:
            return
        lon_span, lat_span = calculate_viewport(self.zoom, self.aspect)
        self.center_lon -= dx * lon_span / cols
        self.center_lat += dy * lat_span / rows
        self._clamp_center()
        self.dirty = True

    def zoom_at(self, direction: int, col: int, row: int, cols: int, rows: int) -> None:
        """Zoom at a cell so the spot under the pointer stays put."""
        if cols <= 0 or rows <= 0:
            return
        old_lon_span, old_lat_span = calculate_viewport(self.zoom, self.aspect)
        fx = (col + 0.5) / cols
        fy = (row + 0.5) / rows
        lon = self.center_lon - old_lon_span / 2 + fx * old_lon_span
        lat = self.center_lat + old_lat_span / 2 - fy * old_lat_span
        old_zoom = self.zoom
        self._set_zoom(self.zoom * ZOOM_STEP if direction > 0 else self.zoom / ZOOM_STEP)
        if self.zoom == old_zoom:
            return
        lon_span, lat_span = calculate_viewport(self.zoom, self.aspect)
        self.center_lon = lon - (fx - 0.5) * lon_span
        self.center_lat = lat + (fy - 0.5) * lat_span
        self._clamp_center()
        self.dirty = True

    def _set_zoom(self, zoom: float) -> None:
        zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))
        if zoom != self.zoom:
            self.zoom = zoom
            self._clamp_center()
            self.dirty = True

    def zoom_in(self) -> None:
        self._set_zoom(self.zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        self._set_zoom(self.zoom / ZOOM_STEP)

    def reset_view(self) -> None:
        self.zoom = ZOOM_MIN
        self.center_lon = START_LON
        self.center_lat = START_LAT
        self.clear_selection()

    # ---- frame building ---------------------------------------------------

    def render(self, width: int, height: int) -> Frame:
        view = View(self.center_lon, self.center_lat, self.zoom, width, height)
        self.aspect = view.aspect
        country_image = view.new_image()
        land_mask = view.new_image()
        outline_draw = ImageDraw.Draw(country_image)
        land_draw = ImageDraw.Draw(land_mask)
        _draw_rings(_visible_rings(self._country_tier, view), view,
                    outline_draw, fill=land_draw)

        land_cover = view.to_cell_coverage(land_mask)
        land_mask.paste(0, mask=country_image)

        renders = []
        for layer in self.layers:
            if not layer.enabled:
                layer.dirty = False
                continue
            renders.append(layer.render(view))
        self.dirty = False

        return Frame(
            country_lines=view.to_lines(country_image),
            layers=renders,
            lon_span=view.lon_span,
            render_width=width,
            land_cover=land_cover,
        )
