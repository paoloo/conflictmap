"""Conflict map rasterizer for CyberEther.

Renders the world as an RGBA frame tensor (numpy) the `frame` block can
display: country outlines, conflict heat blobs, UCDP incident dots and
correlation great-circle lines. Pure numpy + stdlib; the geojson is parsed
once and cached.

Equirectangular projection, lat -60..85 (cuts Antarctica), lon -180..180.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

# Viewport (fixed grid; the frame block zooms/panns the raster natively).
LAT_MIN, LAT_MAX = -60.0, 85.0
HEIGHT, WIDTH = 480, 960

_GRID = np.mgrid[0:HEIGHT, 0:WIDTH]
_LAT_GRID = LAT_MAX - (_GRID[0] + 0.5) * (LAT_MAX - LAT_MIN) / HEIGHT
_LON_GRID = (_GRID[1] + 0.5) * 360.0 / WIDTH - 180.0

_data_dir = Path(__file__).resolve().parent.parent / "data" / "naturalearth"
_geo_cache: dict[str, object] = {}

# Level colors (conflict severity 1..5) as RGB 0..1 — mirrors the TUI/Qt palettes.
LEVEL_COLORS = {
    1: (0.90, 0.76, 0.29),   # yellow
    2: (0.95, 0.62, 0.22),   # orange
    3: (0.94, 0.42, 0.17),   # deep orange
    4: (0.88, 0.19, 0.16),   # red
    5: (0.64, 0.07, 0.12),   # dark red
}
LAND_COLOR = (0.42, 0.47, 0.53)
OCEAN_COLOR = (0.05, 0.08, 0.12)
LINK_COLOR = (0.69, 0.30, 1.00)
EVENT_COLOR = (1.00, 0.35, 0.20)
TEXT_BG = (0.02, 0.04, 0.07)


def _load_countries() -> list[list[list[tuple[float, float]]]]:
    """Country outline rings (lon, lat) from the bundled Natural Earth geojson."""
    if "rings" in _geo_cache:
        return _geo_cache["rings"]
    path = _data_dir / "ne_50m_admin_0_countries.geojson"
    data = json.loads(path.read_text(encoding="utf-8"))
    rings: list[list[tuple[float, float]]] = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        if str(props.get("CONTINENT", "")).lower() == "antarctica":
            continue
        geometry = feature.get("geometry") or {}
        polys: list = []
        if geometry.get("type") == "Polygon":
            polys = [geometry.get("coordinates", [])]
        elif geometry.get("type") == "MultiPolygon":
            polys = geometry.get("coordinates", [])
        for poly in polys:
            if poly:
                ring = [(float(lon), float(lat)) for lon, lat in poly[0]]
                if len(ring) >= 3:
                    rings.append(ring)
    _geo_cache["rings"] = rings
    return rings


def _project(lat: float, lon: float) -> tuple[float, float] | None:
    """lat/lon -> (row, col) fractional grid coordinates, or None if out of view."""
    if not (LAT_MIN <= lat <= LAT_MAX):
        return None
    lon = ((lon + 180.0) % 360.0) - 180.0
    col = (lon + 180.0) / 360.0 * WIDTH
    row = (LAT_MAX - lat) / (LAT_MAX - LAT_MIN) * HEIGHT
    return row, col


def _draw_line(img: np.ndarray, r0: float, c0: float, r1: float, c1: float,
               color: tuple[float, float, float], alpha: float = 1.0) -> None:
    """Bresenham-ish segment with alpha blending, clipped to the grid."""
    steps = max(1, int(math.hypot(r1 - r0, c1 - c0) * 2))
    rows = np.clip(np.linspace(r0, r1, steps + 1).round().astype(np.int64), 0, HEIGHT - 1)
    cols = np.clip(np.linspace(c0, c1, steps + 1).round().astype(np.int64), 0, WIDTH - 1)
    img[rows, cols, 0] = img[rows, cols, 0] * (1 - alpha) + color[0] * alpha
    img[rows, cols, 1] = img[rows, cols, 1] * (1 - alpha) + color[1] * alpha
    img[rows, cols, 2] = img[rows, cols, 2] * (1 - alpha) + color[2] * alpha


def _draw_heat(img: np.ndarray, lat: float, lon: float,
               color: tuple[float, float, float], strength: float) -> None:
    """Radial blob whose radius/intensity scale with the conflict's severity."""
    pos = _project(lat, lon)
    if pos is None:
        return
    row, col = pos
    radius = 12.0 + strength * 26.0     # level 1 small halo, level 5 big bloom
    r0, r1 = max(0, int(row - radius)), min(HEIGHT, int(row + radius) + 1)
    c0, c1 = max(0, int(col - radius)), min(WIDTH, int(col + radius) + 1)
    if r0 >= r1 or c0 >= c1:
        return
    yy, xx = np.mgrid[r0:r1, c0:c1]
    dist = np.hypot(yy - row, xx - col)
    falloff = np.clip(1.0 - dist / radius, 0.0, 1.0) ** 2 * strength
    for ch in range(3):
        patch = img[r0:r1, c0:c1, ch]
        img[r0:r1, c0:c1, ch] = patch * (1 - falloff * 0.85) + color[ch] * falloff * 0.85


def _draw_disc(img: np.ndarray, lat: float, lon: float,
               color: tuple[float, float, float], radius: float = 2.5) -> None:
    pos = _project(lat, lon)
    if pos is None:
        return
    row, col = pos
    r0, r1 = max(0, int(row - radius)), min(HEIGHT, int(row + radius) + 1)
    c0, c1 = max(0, int(col - radius)), min(WIDTH, int(col + radius) + 1)
    if r0 >= r1 or c0 >= c1:
        return
    yy, xx = np.mgrid[r0:r1, c0:c1]
    mask = np.hypot(yy - row, xx - col) <= radius
    for ch in range(3):
        patch = img[r0:r1, c0:c1, ch]
        img[r0:r1, c0:c1, ch] = np.where(mask, color[ch], patch)


def great_circle(lat1, lon1, lat2, lon2, steps: int = 64):
    """Slerp between two points; returns [(lat, lon), ...]."""
    l1, l2 = math.radians(lon1), math.radians(lon2)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    v1 = (math.cos(p1) * math.cos(l1), math.cos(p1) * math.sin(l1), math.sin(p1))
    v2 = (math.cos(p2) * math.cos(l2), math.cos(p2) * math.sin(l2), math.sin(p2))
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(v1, v2))))
    d = math.acos(dot)
    if d < 1e-9:
        return [(lat2, lon2)]
    sin_d = math.sin(d)
    out = []
    for f in np.linspace(0.0, 1.0, steps):
        a = math.sin((1 - f) * d) / sin_d
        b = math.sin(f * d) / sin_d
        x, y, z = a * v1[0] + b * v2[0], a * v1[1] + b * v2[1], a * v1[2] + b * v2[2]
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))),
                    math.degrees(math.atan2(y, x))))
    return out


def render(conflicts: list[dict], events: list[dict], links: list[dict],
           resolve) -> np.ndarray:
    """Render one frame.

    conflicts: ranked dicts (key, conflict, situation_level, lat, lon)
    events:    UCDP dicts (lat, lon)
    links:     correlation dicts (a, b as 'conflict:key' specs, kind)
    resolve:   spec -> (lat, lon) | None
    Returns F32 tensor [HEIGHT, WIDTH, 4] in 0..1 (alpha=1).
    """
    img = np.zeros((HEIGHT, WIDTH, 4), dtype=np.float32)
    img[:, :, :3] = OCEAN_COLOR
    img[:, :, 3] = 1.0

    # Country outlines (subsampled for speed; the geojson is 1:50m).
    for ring in _load_countries():
        previous = None
        for i in range(0, len(ring), 2):
            lon, lat = ring[i]
            pos = _project(lat, lon)
            if pos is not None and previous is not None:
                if abs(pos[1] - previous[1]) < WIDTH / 2:   # skip antimeridian wrap
                    _draw_line(img, previous[0], previous[1], pos[0], pos[1],
                               LAND_COLOR, alpha=0.55)
            previous = pos

    # Conflict heat blobs, strongest last (drawn on top).
    for conflict in sorted(conflicts, key=lambda c: c.get("situation_level", 0)):
        lat, lon = conflict.get("lat"), conflict.get("lon")
        if lat is None or lon is None:
            continue
        level = int(conflict.get("situation_level", 1))
        color = LEVEL_COLORS.get(level, LEVEL_COLORS[1])
        _draw_heat(img, lat, lon, color, strength=0.35 + 0.13 * level)

    # UCDP incident dots.
    for event in events:
        lat, lon = event.get("lat"), event.get("lon")
        if lat is None or lon is None:
            continue
        _draw_disc(img, lat, lon, EVENT_COLOR, radius=2.0)

    # Correlation great-circle lines.
    for link in links:
        if link.get("kind") != "conflict":
            continue
        a, b = resolve(link.get("a", "")), resolve(link.get("b", ""))
        if a is None or b is None:
            continue
        previous = None
        for lat, lon in great_circle(a[0], a[1], b[0], b[1]):
            pos = _project(lat, lon)
            if pos is not None and previous is not None:
                if abs(pos[1] - previous[1]) < WIDTH / 2:
                    _draw_line(img, previous[0], previous[1], pos[0], pos[1],
                               LINK_COLOR, alpha=0.8)
            previous = pos

    return img
