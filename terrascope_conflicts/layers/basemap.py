"""Basemap labels layer: country names/codes, capitals, cities.

Ported from Terrascope's basemap layer (MIT): the same collision-avoiding
label placement (highest priority first, padding, three-row occupancy check).
Data from the project's own Natural Earth copies (core/mapdata.py).
"""
from __future__ import annotations

from ..core.config import UI_CONFIG
from ..core.layer import Layer, LayerRender, Marker
from ..core.mapdata import CountryLabel, Place

COUNTRY_MAX_ZOOM = float(UI_CONFIG["country_max_zoom"])
CAPITAL_MIN_ZOOM = float(UI_CONFIG["capital_min_zoom"])
CITY_LEVELS = tuple(UI_CONFIG["city_levels"])
COUNTRY_COLOR = UI_CONFIG["country_color"]
CAPITAL_COLOR = UI_CONFIG["capital_color"]
CITY_COLORS = tuple(UI_CONFIG["city_colors"])
LABEL_PADDING = int(UI_CONFIG["label_padding"])
MAX_PLACED_LABELS = int(UI_CONFIG["max_labels"])
LEGEND_TEXT_COLOR = UI_CONFIG["legend_text_color"]


def population_floor(zoom: float) -> int | None:
    floor = None
    for min_zoom, population in CITY_LEVELS:
        if zoom >= min_zoom:
            floor = population
    return floor


def city_color(population: int) -> str:
    for minimum, color in CITY_COLORS:
        if population >= minimum:
            return color
    return CITY_COLORS[-1][1]


class BasemapLayer(Layer):
    name = "basemap"
    toggle_key = ""          # always on; the tab owns it
    enabled = True

    def __init__(self) -> None:
        super().__init__()
        self._countries: list[CountryLabel] = []
        self._places: list[Place] = []
        self._state = "idle"      # idle | loading | ready | failed

    def set_data(self, countries: list[CountryLabel], places: list[Place]) -> None:
        self._countries = countries
        # Capitals first (population order), then non-capitals.
        self._places = sorted(places, key=lambda p: (not p[3], -p[4]))
        self._state = "ready"
        self.dirty = True

    def _candidates(self, zoom: float):
        if zoom <= COUNTRY_MAX_ZOOM:
            for lon, lat, code, full in self._countries:
                yield lon, lat, code, COUNTRY_COLOR, True
        if zoom >= CAPITAL_MIN_ZOOM:
            for lon, lat, name, is_capital, population in self._places:
                if is_capital:
                    yield lon, lat, name, CAPITAL_COLOR, False
        floor = population_floor(zoom)
        if floor is not None:
            for lon, lat, name, is_capital, population in self._places:
                if is_capital or population < floor:
                    continue
                yield lon, lat, name, city_color(population), False

    def _place_markers(self, view) -> list[Marker]:
        occupied: dict[int, list[tuple[int, int]]] = {}
        placed_names: set[str] = set()
        markers: list[Marker] = []
        for lon, lat, name, color, centred in self._candidates(view.zoom):
            if len(markers) >= MAX_PLACED_LABELS:
                break
            if name in placed_names:
                continue
            cell = view.to_cell(lon, lat)
            if cell is None:
                continue
            x, y = cell
            if centred:
                x = max(0, x - len(name) // 2)
            if x + len(name) > view.width:
                continue
            start, end = x - LABEL_PADDING, x + len(name) + LABEL_PADDING
            collision = False
            for row in (y - 1, y, y + 1):
                for seg_start, seg_end in occupied.get(row, ()):
                    if start < seg_end and seg_start < end:
                        collision = True
                        break
                if collision:
                    break
            if collision:
                continue
            occupied.setdefault(y, []).append((x, x + len(name)))
            placed_names.add(name)
            markers.append(Marker(x=x, y=y, color=color, label=name))
        return markers

    def build(self, view) -> LayerRender:
        return LayerRender(None, "#000000", markers=self._place_markers(view))
