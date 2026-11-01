"""Tracks and nodes layer: CoT track glyphs and node markers.

Track colours follow app.cot.model.AFFILIATION_COLORS (the Qt app's track
icons); nodes use app/nodes glyphs (text=amber, video=blue). Both come through
the shared stores — the TUI polls them from its reload thread instead of
Qt signals.
"""
from __future__ import annotations

from ..core.config import AFFILIATION_COLORS, NODE_COLORS, NODE_GLYPH, TRACK_GLYPH
from ..core.layer import Layer, LayerRender, Marker


class TracksLayer(Layer):
    name = "tracks"
    toggle_key = "t"

    def __init__(self) -> None:
        super().__init__()
        self.tracks: dict[str, dict] = {}      # uid -> {lat, lon, callsign, affiliation}
        self.nodes: list[dict] = []            # {uid, lat, lon, title, type}

    def set_data(self, tracks: dict[str, dict], nodes: list[dict]) -> None:
        self.tracks = tracks
        self.nodes = nodes
        self.dirty = True

    def build(self, view) -> LayerRender:
        markers: list[Marker] = []
        if not self.enabled:
            return LayerRender(None, "#000000", markers)
        for uid, track in self.tracks.items():
            cell = view.to_cell(track["lon"], track["lat"])
            if cell is None:
                continue
            color = AFFILIATION_COLORS.get(track.get("affiliation") or "unknown", "#eecc33")
            selected = self.selected is not None and isinstance(self.selected, tuple) \
                and self.selected[0] == "track" and self.selected[1] == uid
            callsign = track.get("callsign") or uid
            markers.append(Marker(x=cell[0], y=cell[1], color=color, glyph=TRACK_GLYPH,
                                  item=("track", uid), selected=selected))
            markers.append(Marker(x=cell[0], y=cell[1], color=color, label=callsign,
                                  item=("track", uid), selected=selected))
        for node in self.nodes:
            cell = view.to_cell(node["lon"], node["lat"])
            if cell is None:
                continue
            color = NODE_COLORS.get(node.get("type") or "text", "#f2c14e")
            selected = self.selected is not None and isinstance(self.selected, tuple) \
                and self.selected[0] == "node" and self.selected[1] == node["uid"]
            markers.append(Marker(x=cell[0], y=cell[1], color=color, glyph=NODE_GLYPH,
                                  item=("node", node["uid"]), selected=selected))
            markers.append(Marker(x=cell[0], y=cell[1], color=color,
                                  label=node.get("title") or node["uid"],
                                  item=("node", node["uid"]), selected=selected))
        return LayerRender(None, "#000000", markers)

    def info_rows(self) -> list[tuple[str, str]]:
        if not isinstance(self.selected, tuple):
            return []
        kind, uid = self.selected
        if kind == "track":
            track = self.tracks.get(uid, {})
            return [("TRACK", track.get("callsign") or uid),
                    ("AFFIL", track.get("affiliation") or "unknown"),
                    ("POS", f"{track.get('lat', 0):.4f}, {track.get('lon', 0):.4f}")]
        node = next((n for n in self.nodes if n["uid"] == uid), None)
        if node is None:
            return []
        return [("NODE", node.get("title") or uid),
                ("TYPE", node.get("type") or "text"),
                ("POS", f"{node.get('lat', 0):.4f}, {node.get('lon', 0):.4f}")]
