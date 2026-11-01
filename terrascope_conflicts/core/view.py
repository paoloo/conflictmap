"""Map maths and viewport -> pixel/terminal-cell conversion.

Ported from Terrascope's core/view.py (MIT): the same braille 2x4 dot encoding,
the same land-coverage blur, the same latitude band. The only structural
change: settings come from core/config.py instead of default.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from typing import TYPE_CHECKING

from .config import MAP_CONFIG

if TYPE_CHECKING:
    from .mapdata import Ring as RingT

CELL_ASPECT = float(MAP_CONFIG["cell_aspect"])
COAST_BLUR_HORIZONTAL = tuple(MAP_CONFIG["coast_blur_horizontal"])
COAST_BLUR_VERTICAL = tuple(MAP_CONFIG["coast_blur_vertical"])

LAT_MIN = float(MAP_CONFIG["lat_min"])
LAT_MAX = float(MAP_CONFIG["lat_max"])
LAT_RANGE = LAT_MAX - LAT_MIN
LAT_MID = (LAT_MAX + LAT_MIN) / 2

BRAILLE_BIT_ORDER = np.array([[0, 3], [1, 4], [2, 5], [6, 7]], dtype=np.uint8)
BRAILLE_BIT_WEIGHTS = (1 << BRAILLE_BIT_ORDER).astype(np.uint8)
BLANK_BRAILLE = "\u2800"

WRAP_SHIFTS = (-360.0, 0.0, 360.0)


if TYPE_CHECKING:
    from .mapdata import Ring as RingT


def visible(polygon: "RingT", center_lon: float, center_lat: float,
            lon_span: float, lat_span: float) -> list[float]:
    _, min_lon, max_lon, min_lat, max_lat = polygon
    view_min_lon = center_lon - lon_span / 2
    view_max_lon = center_lon + lon_span / 2
    if max_lat < center_lat - lat_span / 2 or min_lat > center_lat + lat_span / 2:
        return []
    shifts = []
    for shift in WRAP_SHIFTS:
        if max_lon + shift < view_min_lon or min_lon + shift > view_max_lon:
            continue
        shifts.append(shift)
    return shifts


def project_location(longitude, latitude, center_lon, center_lat, lon_span,
                     lat_span, render_width, render_height):
    relative_lon = (longitude - (center_lon - lon_span / 2)) % 360.0
    if relative_lon > lon_span:
        return None
    x = relative_lon / lon_span * render_width
    y = (center_lat + lat_span / 2 - latitude) / lat_span * render_height
    if x < 0 or x >= render_width or y < 0 or y >= render_height:
        return None
    return x, y


def calculate_viewport(zoom: float, aspect: float = 2.0) -> tuple[float, float]:
    lat_span = LAT_RANGE / zoom
    lon_span = lat_span * aspect
    return lon_span, lat_span


def viewport_aspect(width: int, height: int, cell_aspect: float = 2.0) -> float:
    if height <= 0:
        return 2.0
    return width / (height * cell_aspect)


def normalize_longitude(longitude: float) -> float:
    return ((longitude + 180.0) % 360.0) - 180.0


def clamp_center(center_lon, center_lat, lon_span, lat_span):
    center_lon = normalize_longitude(center_lon)
    if lat_span >= LAT_RANGE - 1e-9:
        center_lat = LAT_MID
    else:
        half_lat = lat_span / 2
        center_lat = max(LAT_MIN + half_lat, min(LAT_MAX - half_lat, center_lat))
    return center_lon, center_lat


def _blur(values: np.ndarray, axis: int, kernel: tuple[float, ...]) -> np.ndarray:
    weights = np.asarray(kernel, dtype=np.float32)
    weights /= weights.sum()
    radius = len(weights) // 2
    pad = [(0, 0), (0, 0)]
    pad[axis] = (radius, radius)
    padded = np.pad(values, pad, mode="edge")
    length = values.shape[axis]
    result = np.zeros_like(values)
    for offset, weight in enumerate(weights):
        result += weight * np.take(padded, range(offset, offset + length), axis=axis)
    return result


@dataclass
class View:
    center_lon: float
    center_lat: float
    zoom: float
    width: int
    height: int
    lon_span: float = field(init=False)
    lat_span: float = field(init=False)
    pixel_width: int = field(init=False)
    pixel_height: int = field(init=False)
    scale_x: float = field(init=False)
    scale_y: float = field(init=False)
    origin_lon: float = field(init=False)
    origin_lat: float = field(init=False)
    aspect: float = field(init=False)

    def __post_init__(self) -> None:
        self.aspect = viewport_aspect(self.width, self.height, CELL_ASPECT)
        self.lon_span, self.lat_span = calculate_viewport(self.zoom, self.aspect)
        self.pixel_width = self.width * 2
        self.pixel_height = self.height * 4
        self.scale_x = self.pixel_width / self.lon_span
        self.scale_y = self.pixel_height / self.lat_span
        self.origin_lon = self.center_lon - self.lon_span / 2
        self.origin_lat = self.center_lat + self.lat_span / 2

    def new_image(self) -> Image.Image:
        return Image.new("L", (self.pixel_width, self.pixel_height), 0)

    def _cell_array(self, image: Image.Image, dtype) -> np.ndarray:
        expected = (self.width * 2, self.height * 4)
        if image.size != expected:
            image = image.resize(expected)
        array = np.asarray(image.convert("1"), dtype=dtype)
        return array.reshape(self.height, 4, self.width, 2)

    def to_lines(self, image: Image.Image) -> list[str]:
        array = self._cell_array(image, np.uint8)
        weights = BRAILLE_BIT_WEIGHTS[np.newaxis, :, np.newaxis, :]
        values = (array * weights).sum(axis=(1, 3))
        codes = np.ascontiguousarray(values + 0x2800, dtype="<u2")
        return [row.tobytes().decode("utf-16-le") for row in codes]

    def to_cell_coverage(self, image: Image.Image, smooth: bool = True) -> np.ndarray:
        coverage = self._cell_array(image, np.float32).mean(axis=(1, 3))
        if smooth:
            coverage = _blur(coverage, axis=1, kernel=COAST_BLUR_HORIZONTAL)
            coverage = _blur(coverage, axis=0, kernel=COAST_BLUR_VERTICAL)
        return coverage

    def to_cell(self, longitude: float, latitude: float) -> tuple[int, int] | None:
        position = self.project(longitude, latitude)
        if position is None:
            return None
        cell_x, cell_y = int(position[0]), int(position[1])
        if 0 <= cell_x < self.width and 0 <= cell_y < self.height:
            return cell_x, cell_y
        return None

    def project(self, longitude: float, latitude: float) -> tuple[float, float] | None:
        return project_location(
            longitude, latitude, self.center_lon, self.center_lat,
            self.lon_span, self.lat_span, self.width, self.height)

    def to_pixel(self, longitude: float, latitude: float,
                 shift: float = 0.0) -> tuple[float, float]:
        """Pixel-space projection for drawing into view-sized images
        (pixel_width x pixel_height); unbounded, cull segments yourself."""
        x = (longitude + shift - self.origin_lon) * self.scale_x
        y = (self.origin_lat - latitude) * self.scale_y
        return x, y
