"""Map backgrounds, overlays, markers, legend, scale bar.

Ported from Terrascope's ui/map.py (MIT): the same water/land background
interpolation, coastline shade fade (░▒▓), background-run batching, and the
draw order background -> layer symbols -> markers -> attribution/scale/legend.
"""
from __future__ import annotations

import curses
import math

import numpy as np

from ..core.config import MAP_CONFIG, UI_CONFIG
from ..core.layer import LayerRender, Marker
from ..core.view import BLANK_BRAILLE
from .colors import ColorManager, hex_to_rgb, parse_rgba
from .layout import MAP_EDGE_PAD_X, MAP_EDGE_PAD_Y, Rect, draw_box, safe_addstr

MAP_CONFIG_LOCAL = MAP_CONFIG
LEGEND_TEXT_COLOR = UI_CONFIG["legend_text_color"]
SHADE_GLYPHS = ("\u2591", "\u2592", "\u2593")
SHADE_MIN_DENSITY = 0.12
SHADE_SOLID_DENSITY = 0.87
EDGE_STEPS = int(MAP_CONFIG["coast_fade_steps"])
ATTRIBUTION_COLOR = "#{:02x}{:02x}{:02x}".format(
    *(channel // 2 for channel in hex_to_rgb(LEGEND_TEXT_COLOR)))


def _parse_legend_item(item) -> tuple[str, str | None, str]:
    parts = [str(part) for part in item]
    color_index = next((i for i, part in enumerate(parts) if part.startswith("#")), None)
    color = parts.pop(color_index) if color_index is not None else None
    if len(parts) >= 2:
        parts.sort(key=len)
        return parts[0], color, parts[1]
    return "", color, parts[0] if parts else ""


def _edge_palette(water, land) -> list[str | None]:
    """Background colour for each land-coverage step 0 (water) .. EDGE_STEPS."""
    blend = MAP_CONFIG.get("transparent_blend_color") or "#000000"
    blend_rgb = parse_rgba(blend)[:3]
    wr, wg, wb, wa = parse_rgba(water)
    lr, lg, lb, la = parse_rgba(land)
    palette: list[str | None] = []
    for step in range(EDGE_STEPS + 1):
        t = step / EDGE_STEPS if EDGE_STEPS else 1.0
        alpha = wa + (la - wa) * t
        if round(alpha * 255) == 0:
            palette.append(None)
            continue
        mixed = [(w * wa + (l * la - w * wa) * t) / alpha
                 for w, l in ((wr, lr), (wg, lg), (wb, lb))]
        final = [round(c * alpha + b * (1 - alpha)) for c, b in zip(mixed, blend_rgb)]
        palette.append("#{:02x}{:02x}{:02x}".format(*final))
    return palette


def _edge_cells(water, land):
    """Per step: (background colour or None, shade glyph or None) + shade colour."""
    palette = _edge_palette(water, land)
    water_alpha = parse_rgba(water)[3]
    land_alpha = parse_rgba(land)[3]
    water_clear = round(water_alpha * 255) == 0
    land_clear = round(land_alpha * 255) == 0
    if water_clear == land_clear:
        return [(color, None) for color in palette], None
    solid, max_alpha = (land, land_alpha) if water_clear else (water, water_alpha)
    blend_rgb = parse_rgba(MAP_CONFIG.get("transparent_blend_color") or "#000000")[:3]
    shade_color = "#{:02x}{:02x}{:02x}".format(
        *(round(c * max_alpha + b * (1 - max_alpha))
          for c, b in zip(parse_rgba(solid)[:3], blend_rgb)))
    cells = []
    for step in range(EDGE_STEPS + 1):
        t = step / EDGE_STEPS if EDGE_STEPS else 1.0
        alpha = water_alpha + (land_alpha - water_alpha) * t
        density = max(0.0, min(1.0, alpha / max_alpha)) if max_alpha > 0 else 0.0
        if density < SHADE_MIN_DENSITY:
            cells.append((None, None))
        elif density >= SHADE_SOLID_DENSITY:
            cells.append((shade_color, None))
        elif density < 0.31:
            cells.append((None, SHADE_GLYPHS[0]))
        elif density < 0.56:
            cells.append((None, SHADE_GLYPHS[1]))
        else:
            cells.append((None, SHADE_GLYPHS[2]))
    return cells, shade_color


def _background_data(frame, water, land):
    key = (water, land)
    cached = frame.background_cache
    if cached is not None and len(cached) == 3 and cached[0] == key:
        return cached
    cells, shade_color = _edge_cells(water, land)
    cover = frame.land_cover
    shades = None
    if cover is None:
        grid: list[list[str | None]] = []
        if shade_color is not None:
            shades = ([], shade_color)
    else:
        steps = np.clip(np.rint(cover * EDGE_STEPS), 0, EDGE_STEPS).astype(int).tolist()
        grid = [[cells[step][0] for step in row] for row in steps]
        if shade_color is not None:
            shades = ([[cells[step][1] for step in row] for row in steps], shade_color)
    data = (key, grid, shades)
    frame.background_cache = data
    return data


def background_grid(frame, water, land) -> list[list[str | None]]:
    return _background_data(frame, water, land)[1]


def edge_shades(frame, water, land):
    return _background_data(frame, water, land)[2]


class _MapArea:
    """Geometry + background lookup for the inside of the map box."""

    def __init__(self, window, top, left, height, width, frame, colors: ColorManager) -> None:
        self.window = window
        self.colors = colors
        self.frame = frame
        self.top = top + 1
        self.left = left + 2
        self.width = width - 4
        self.height = height - 2
        self.water = MAP_CONFIG["water_color"]
        self.land = MAP_CONFIG["land_color"]
        self.grid = background_grid(frame, self.water, self.land)
        overrides: dict[tuple[int, int], str] = {}
        for layer_render in frame.layers:
            if layer_render.bg_cells:
                overrides.update(layer_render.bg_cells)
        if overrides:
            grid = [row[:] for row in self.grid]
            for (x, y), color in overrides.items():
                if 0 <= y < len(grid) and 0 <= x < len(grid[y]):
                    grid[y][x] = color
            self.grid = grid
        self._fallback = [_edge_palette(self.water, self.land)[0]] * self.width
        self.override_cells = set(overrides)
        self.shades = edge_shades(frame, self.water, self.land)

    def shade_at(self, row: int, col: int) -> tuple[str, str] | None:
        if not self.shades or (col, row) in self.override_cells:
            return None
        rows, color = self.shades
        if 0 <= row < len(rows) and 0 <= col < len(rows[row]):
            glyph = rows[row][col]
            if glyph:
                return glyph, color
        return None

    def bg_row(self, row: int) -> list[str | None]:
        if row < len(self.grid) and len(self.grid[row]) >= self.width:
            return self.grid[row]
        return self._fallback

    def bg_at(self, row: int, col: int) -> str | None:
        return self.bg_row(row)[col]

    def put(self, row: int, col: int, text: str, color: str, bg: str | None, extra: int = 0) -> None:
        safe_addstr(self.window, self.top + row, self.left + col, text,
                    self.colors.get_pair(color, bg) | extra)


def draw_map_overlay_text(window, rect: Rect, frame, colors: ColorManager,
                          y: int, x: int, text: str, extra: int = 0) -> None:
    """Status text over the map, each char keeping the map cell's background."""
    area = _MapArea(window, *rect, frame, colors)
    row = y - area.top
    col = x - area.left
    if not 0 <= row < area.height:
        return
    for offset, char in enumerate(text):
        target = col + offset
        if 0 <= target < area.width:
            area.put(row, target, char, LEGEND_TEXT_COLOR, area.bg_at(row, target), extra)


def draw_background(area: _MapArea) -> None:
    """Country outlines (braille) over the water/land background."""
    country_color = MAP_CONFIG["country_color"]
    empty = (BLANK_BRAILLE, " ")
    for row in range(area.height):
        line = area.frame.country_lines[row] if row < len(area.frame.country_lines) else ""
        text = line[: area.width].ljust(area.width)
        cells = []
        for col in range(area.width):
            char = text[col]
            if char not in empty:
                cells.append((char, country_color, 0))
            else:
                shade = area.shade_at(row, col)
                cells.append((shade[0], shade[1], 0) if shade else (char, country_color, 0))
        bg_row = area.bg_row(row)
        col = 0
        while col < area.width:
            bg = bg_row[col]
            _, color, extra = cells[col]
            stop = col + 1
            while (stop < area.width and bg_row[stop] == bg
                   and cells[stop][1:] == (color, extra)):
                stop += 1
            area.put(row, col, "".join(cell[0] for cell in cells[col:stop]), color, bg, extra)
            col = stop


def draw_layer_symbols(area: _MapArea) -> None:
    """Braille content each layer produced (link lines, heat edges, ...)."""
    for layer_render in area.frame.layers:
        if layer_render.lines is None:
            continue
        color = layer_render.line_color
        for row in range(min(area.height, len(layer_render.lines))):
            line = layer_render.lines[row]
            bg_row = area.bg_row(row)
            limit = min(area.width, len(line))
            col = 0
            while col < limit:
                if line[col] == BLANK_BRAILLE:
                    col += 1
                    continue
                bg = bg_row[col]
                stop = col + 1
                while (stop < limit and line[stop] != BLANK_BRAILLE
                       and bg_row[stop] == bg):
                    stop += 1
                area.put(row, col, line[col:stop], color, bg)
                col = stop


def draw_markers(area: _MapArea) -> None:
    """Symbols first, then text labels, so a glyph never cuts a label."""
    _draw_markers_pass(area, labels=False)
    _draw_markers_pass(area, labels=True)


def _draw_markers_pass(area: _MapArea, labels: bool) -> None:
    for layer_render in area.frame.layers:
        for marker in layer_render.markers:
            if bool(marker.label) != labels:
                continue
            x, y = int(marker.x), int(marker.y)
            if x < 0 or x >= area.width or y < 0 or y >= area.height:
                continue
            bg = marker.bg_color if marker.bg_color is not None else area.bg_at(y, x)
            attributes = area.colors.get_pair(marker.color, bg)
            if not (marker.glyph and "\u2800" <= marker.glyph[0] <= "\u28ff"):
                attributes |= curses.A_BOLD
            if marker.dim:
                attributes = (attributes & ~curses.A_BOLD) | curses.A_DIM
            if marker.selected:
                attributes |= curses.A_REVERSE
            if marker.label:
                label = marker.label
                if x + len(label) > area.width:
                    label = label[: max(1, area.width - x)]
                extra = attributes & ~curses.A_COLOR
                for offset, char in enumerate(label):
                    cell_bg = (marker.bg_color if marker.bg_color is not None
                               else area.bg_at(y, x + offset))
                    cell_attributes = extra | area.colors.get_pair(marker.color, cell_bg)
                    safe_addstr(area.window, area.top + y, area.left + x + offset,
                                char, cell_attributes)
                continue
            character = marker.glyph
            if character is None:
                lines = layer_render.lines
                if lines is None or y >= len(lines) or x >= len(lines[y]):
                    continue
                character = lines[y][x]
                if character == BLANK_BRAILLE:
                    continue
            elif character == BLANK_BRAILLE and marker.bg_color is None:
                shade = area.shade_at(y, x)
                if shade:
                    character = shade[0]
                    attributes = area.colors.get_pair(shade[1], bg)
            safe_addstr(area.window, area.top + y, area.left + x, character, attributes)


def _legend_segments(line) -> list[tuple[str, str | None, int]]:
    segments: list[tuple[str, str | None, int]] = []
    segments.append((line.text or "", None, curses.A_BOLD if line.bold else 0))
    for swatch in line.swatches:
        segments.append((" ", None, 0))
        segments.append((line.swatch_glyph, swatch, 0))
    if line.choices:
        segments.append(("[", None, curses.A_DIM))
        for index, choice in enumerate(line.choices):
            lit = index == line.choice_index
            segments.append((f" {choice} ", None,
                             curses.A_REVERSE | curses.A_BOLD if lit else curses.A_DIM))
        segments.append(("]", None, curses.A_DIM))
    for item in getattr(line, "legend_items", ()):
        glyph, color, label = _parse_legend_item(item)
        if any(segment[0] for segment in segments):
            segments.append(("  ", None, 0))
        if glyph:
            segments.append((glyph + " ", color, 0))
        segments.append((label, None, 0))
    return [segment for segment in segments if segment[0]]


def draw_map_legend(area: _MapArea, lines) -> None:
    """Legend of the active layers, stacked above the fixed bottom row."""
    if not lines or area.height < 5 + MAP_EDGE_PAD_Y:
        return
    lines = lines[: area.height - 2 - MAP_EDGE_PAD_Y]
    last_row = area.height - 2 - MAP_EDGE_PAD_Y
    room = area.width - MAP_EDGE_PAD_X
    first_row = last_row - len(lines) + 1
    for offset, line in enumerate(lines):
        segments = _legend_segments(line)
        total = sum(len(text) for text, _, _ in segments)
        if total == 0 or total > room:
            continue
        row = first_row + offset
        col = room - total
        for text, color, extra in segments:
            for char in text:
                area.put(row, col, char, color or LEGEND_TEXT_COLOR,
                         area.bg_at(row, col), extra)
                col += 1


def _nice_round_km(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    residual = value / magnitude
    if residual < 1.5:
        nice = 1
    elif residual < 3.5:
        nice = 2
    elif residual < 7.5:
        nice = 5
    else:
        nice = 10
    return nice * magnitude


def _format_km(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f} km"
    if value >= 1:
        return f"{value:.0f} km"
    return f"{value * 1000:.0f} m"


def compute_scale_bar(lon_span: float, render_width: int, center_lat: float,
                      max_columns: int) -> tuple[int, str] | None:
    sb = UI_CONFIG["scale_bar"]
    if render_width <= 0 or lon_span <= 0 or max_columns < sb["min_columns"]:
        return None
    km_per_pixel = (lon_span * 111.32 * math.cos(math.radians(center_lat))) / render_width
    target_km = km_per_pixel * sb["target_columns"]
    nice_km = _nice_round_km(target_km)
    bar_columns = int(round(nice_km / km_per_pixel))
    if bar_columns < sb["min_bar_columns"]:
        return None
    bar_columns = min(bar_columns, max_columns - 4)
    if bar_columns < sb["min_bar_columns"]:
        return None
    bar = sb["end"] + sb["fill"] * (bar_columns - 2) + sb["end"]
    return bar_columns, f"{bar} {_format_km(nice_km)}"


def _put_on_map(area: _MapArea, row: int, col: int, text: str,
                color: str = LEGEND_TEXT_COLOR, extra: int = 0) -> None:
    for offset, char in enumerate(text):
        target = col + offset
        if 0 <= target < area.width:
            area.put(row, target, char, color, area.bg_at(row, target), extra)


def draw_scale_bar(area: _MapArea, center_lat: float) -> None:
    result = compute_scale_bar(area.frame.lon_span, area.frame.render_width,
                               center_lat, area.width - MAP_EDGE_PAD_X)
    if result is None:
        return
    _bar_columns, text = result
    _put_on_map(area, area.height - 1 - MAP_EDGE_PAD_Y,
                area.width - MAP_EDGE_PAD_X - len(text), text, extra=curses.A_DIM)


def draw_attribution(area: _MapArea, text: str) -> None:
    if text:
        room = max(0, area.width - 2 * MAP_EDGE_PAD_X)
        bottom = area.height - 1 - MAP_EDGE_PAD_Y
        _put_on_map(area, bottom, MAP_EDGE_PAD_X, text[:room], color=ATTRIBUTION_COLOR)


def draw_map_box(window, top: int, left: int, height: int, width: int, frame,
                 colors: ColorManager, zoom: float, center_lat: float,
                 attribution: str = "", legend=None) -> None:
    draw_box(window, top, left, height, width, "CONFLICT MAP")
    area = _MapArea(window, top, left, height, width, frame, colors)
    draw_background(area)
    draw_layer_symbols(area)
    draw_markers(area)
    draw_attribution(area, attribution)
    draw_scale_bar(area, center_lat)
    draw_map_legend(area, legend)
