"""Natural Earth loading, adapted from Terrascope's mapdata.py but reading the
bundled GeoJSON in data/naturalearth/ (no first-run download; the 1:50m
countries file ships with the app — countries, labels, places all come from
it).

Ring format identical to Terrascope: (points, min_lon, max_lon, min_lat,
max_lat) with antimeridian-unwrapped longitudes.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

# tui/terrascope_conflicts/core/mapdata.py → package root (tui/) → data/.
DATA_DIRS = [
    Path(__file__).resolve().parents[2] / "data" / "naturalearth",
    Path(__file__).resolve().parents[2] / "data",
]

Ring = tuple[list[tuple[float, float]], float, float, float, float]
CountryLabel = tuple[float, float, str, str]           # lon, lat, short code, full name
Place = tuple[float, float, str, bool, int]            # lon, lat, name, is_capital, population


class MapDataError(Exception):
    pass


def _find(filename: str) -> Path:
    for directory in DATA_DIRS:
        path = directory / filename
        if path.exists():
            return path
    raise MapDataError(f"{filename} not found (looked in {[str(d) for d in DATA_DIRS]})")


def unwrap_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return points
    result = [points[0]]
    previous_lon = points[0][0]
    offset = 0.0
    for lon, lat in points[1:]:
        delta = lon - previous_lon
        if delta > 180:
            offset -= 360.0
        elif delta < -180:
            offset += 360.0
        result.append((lon + offset, lat))
        previous_lon = lon
    return result


def make_ring(points) -> Ring | None:
    converted = []
    for point in points:
        if len(point) < 2:
            continue
        try:
            lon, lat = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(lon) and math.isfinite(lat):
            converted.append((lon, lat))
    if len(converted) < 2:
        return None
    converted = unwrap_ring(converted)
    lons = [p[0] for p in converted]
    lats = [p[1] for p in converted]
    return (converted, min(lons), max(lons), min(lats), max(lats))


def _is_antarctica(properties: object) -> bool:
    if not isinstance(properties, dict):
        return False
    props = {str(k).upper(): v for k, v in properties.items()}
    return (props.get("CONTINENT") == "Antarctica" or props.get("ADM0_A3") == "ATA"
            or props.get("ISO_A3") == "ATA"
            or "Antarctica" in (props.get("ADMIN"), props.get("NAME")))


def _features(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MapDataError(f"Could not parse {path.name}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise MapDataError(f"Could not parse {path.name}: unexpected GeoJSON format")
    return data["features"]


def load_polygons(filename: str) -> list[Ring]:
    """Filled country polygons (outlines drawn from the same rings)."""
    rings: list[Ring] = []
    for feature in _features(_find(filename)):
        if not isinstance(feature, dict) or _is_antarctica(feature.get("properties")):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        if geometry.get("type") == "Polygon" and coordinates:
            ring = make_ring(coordinates[0])
            if ring:
                rings.append(ring)
        elif geometry.get("type") == "MultiPolygon" and coordinates:
            for polygon in coordinates:
                if polygon:
                    ring = make_ring(polygon[0])
                    if ring:
                        rings.append(ring)
    return rings


def load_country_labels() -> list[CountryLabel]:
    """(lon, lat, ISO-A2 code, full name) from the countries file's label points."""
    labels: list[CountryLabel] = []
    seen: set[str] = set()
    for feature in _features(_find("ne_50m_admin_0_countries.geojson")):
        props = feature.get("properties") or {}
        props = {str(k).upper(): v for k, v in props.items()}
        name = str(props.get("ADMIN") or props.get("NAME") or "")
        code = str(props.get("ISO_A2") or props.get("ADM0_A3") or "")
        lon, lat = props.get("LABEL_X"), props.get("LABEL_Y")
        if not name or code in seen or lon is None or lat is None:
            continue
        try:
            lon_f, lat_f = float(lon), float(lat)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
            continue
        seen.add(code)
        labels.append((lon_f, lat_f, code, name))
    return labels


def load_places() -> list[Place]:
    """(lon, lat, name, is_capital, population), biggest first for the caller."""
    places: list[Place] = []
    for feature in _features(_find("ne_10m_populated_places_simple.geojson")):
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            lon, lat = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        props = {str(k).lower(): v for k, v in properties.items()}
        name = props.get("name") or props.get("nameascii")
        if not name:
            continue
        is_capital = str(props.get("featurecla", "")).lower().startswith("admin-0 capital")
        if str(props.get("adm0cap", "")).strip() in ("1", "1.0"):
            is_capital = True
        population = 0
        for key in ("pop_max", "pop_min"):
            try:
                population = max(population, int(float(props.get(key) or 0)))
            except (TypeError, ValueError):
                pass
        places.append((lon, lat, str(name), is_capital, population))
    places.sort(key=lambda p: p[4], reverse=True)
    return places
