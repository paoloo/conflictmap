"""Correlation links layer: braille lines between related entities.

Port of the Qt app's CorrelationLayer semantics onto Terrascope's line
mechanism (earthquake-free): `LayerRender.lines` braille rows are the line
raster (draw_layer_symbols draws them over the heat background), the label is
the strongest reason placed at the line's midpoint, colour by kind
(conflict<->conflict purple solid, entity<->conflict blue) exactly like
app/conflicts/links.py COLORS. Line presence follows the aggression switch:
when a diamond is hidden its links are too.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image

from ..core.config import LINK_COLORS
from ..core.layer import Layer, LayerRender, Marker
from ..core.view import View, WRAP_SHIFTS

LABEL_COLOR = "#d8c7ff"
ENTITY_LABEL_COLOR = "#7fd4ff"


def great_circle(lat1, lon1, lat2, lon2, steps: int = 48):
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
    points = []
    for f in np.linspace(0.0, 1.0, steps):
        A = math.sin((1 - f) * d) / sin_d
        B = math.sin(f * d) / sin_d
        x = A * v1[0] + B * v2[0]
        y = A * v1[1] + B * v2[1]
        z = A * v1[2] + B * v2[2]
        lat = math.degrees(math.atan2(z, math.hypot(x, y)))
        lon = math.degrees(math.atan2(y, x))
        points.append((lat, lon))
    return points


class LinkLine:
    """One drawn correlation: endpoints, strength, reasons."""
    id_prefix_conflict = "conflict:"

    def __init__(self, corr) -> None:
        self.corr = corr
        self.a = corr.a
        self.b = corr.b

    @property
    def color(self) -> str:
        return LINK_COLORS.get(self.corr.kind, "#888888")

    @property
    def label_color(self) -> str:
        return LABEL_COLOR if self.corr.kind == "conflict" else ENTITY_LABEL_COLOR


class LinksLayer(Layer):
    name = "links"
    toggle_key = "l"

    def __init__(self, resolve) -> None:
        super().__init__()
        self.resolve = resolve            # anchor spec -> (lat, lon) | None
        self.links: dict[str, LinkLine] = {}
        self.labels = True

    def set_links(self, correlations) -> None:
        self.links = {c.id: LinkLine(c) for c in correlations}
        self.dirty = True

    def visible_links(self) -> list[LinkLine]:
        if not self.enabled:
            return []
        out = []
        for line in self.links.values():
            a, b = self.resolve(line.a), self.resolve(line.b)
            if a is not None and b is not None:
                out.append(line)
        return out

    def _draw_lines(self, view: View) -> list[str]:
        image = view.new_image()
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        for line in self.visible_links():
            a, b = self.resolve(line.a), self.resolve(line.b)
            points = great_circle(a[0], a[1], b[0], b[1])
            # Draw per wrap-shift so lines crossing the antimeridian segment
            # correctly (same as Terrascope's flight tracks).
            for shift in WRAP_SHIFTS:
                pixels = [view.to_pixel(lat_lon[1], lat_lon[0], shift)
                          for lat_lon in points]
                previous = None
                for position in pixels:
                    x, y = position
                    if (max(x, 0 if previous is None else previous[0]) < -2
                            or min(x, previous[0] if previous is not None else 0)
                            > view.pixel_width + 2):
                        previous = None
                        continue
                    if previous is not None:
                        draw.line((*previous, position), fill=255, width=1)
                    previous = position
        return view.to_lines(image)

    def build(self, view: View) -> LayerRender:
        markers: list[Marker] = []
        if not self.enabled or not self.links:
            return LayerRender(None, "#000000", markers)
        lines = self._draw_lines(view)
        for line in self.visible_links():
            corr = line.corr
            a, b = self.resolve(corr.a), self.resolve(corr.b)
            mid_lat, mid_lon = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            cell = view.to_cell(mid_lon, mid_lat)
            if cell is None or not self.labels or not corr.short:
                continue
            x, y = cell
            text = corr.short
            x = max(0, min(view.width - len(text), x - len(text) // 2))
            markers.append(Marker(x=x, y=y, color=line.label_color, label=text,
                                  item=corr))
        return LayerRender(lines, line_color_for(self), markers)


def line_color_for(layer: LinksLayer) -> str:
    """Dominant line colour (the braille raster draws in one colour per
    render; the reason labels carry the per-kind colour on top)."""
    if not layer.links:
        return "#000000"
    kinds = {line.corr.kind for line in layer.visible_links()}
    if kinds == {"entity"}:
        return LINK_COLORS["entity"]
    return LINK_COLORS["conflict"]
