"""Shared settings for the terminal app (defaults; a TOML section could
override later, but the Qt app's config.toml already carries the intel side).

Map look mirrors Terrascope's default.yaml `map:` block; conflict colours are
the aggression layer's LEVEL_COLORS so both UIs read the same.
"""
from __future__ import annotations

MAP_CONFIG: dict = {
    "country_color": "#ffffff",
    "water_color": "#1E2C39",
    "land_color": "#0F0F15",
    "transparent_blend_color": None,
    "cell_aspect": 2.0,
    "coast_fade_steps": 8,
    "coast_blur_horizontal": [1, 2, 3, 2, 1],
    "coast_blur_vertical": [1, 2, 1],
    "lat_min": -58.0,
    "lat_max": 84.0,
    "zoom": {"min": 1.0, "max": 64.0, "step": 1.5, "pan_step": 0.05},
}

UI_CONFIG: dict = {
    "outer_margin": (2, 1),
    "panel_height": 11,
    "panel_min_height": 5,
    "panel_min_map_height": 8,
    "map_edge_pad_x": 2,
    "map_edge_pad_y": 0,
    "legend_text_color": "#ffffff",
    "label_text_color": "#e5e9ee",
    "country_color": "#ffffff",
    "capital_color": "#e3e9ee",
    "city_colors": ((1_000_000, "#cfd8e3"), (100_000, "#a9b4c0"), (0, "#8c98a4")),
    "country_max_zoom": 8.0,
    "capital_min_zoom": 1.0,
    "city_levels": ((2.0, 5_000_000), (3.0, 2_000_000), (4.5, 1_000_000),
                    (7.0, 500_000), (10.0, 200_000)),
    "label_padding": 2,
    "max_labels": 250,
    "scale_bar": {"target_columns": 14, "min_columns": 6, "min_bar_columns": 3,
                  "end": "|", "fill": "─"},
}

APP_CONFIG: dict = {
    "min_terminal_width": 60,
    "min_terminal_height": 20,
    "heartbeat_seconds": 1.0,
    "frame_delay_ms": 50,
    "drag_frame_delay_ms": 5,
    "click_radius": 1.5,
    "retry_base_seconds": 15,
    "retry_max_seconds": 300,
}

# Conflict overlay (mirrors app.conflicts.layer.LEVEL_COLORS + TREND_ARROW).
LEVEL_COLORS = ["#8a8a8a", "#e6c34a", "#f29d38", "#ef6a2b", "#e0302a", "#a3121f"]
TREND_ARROW = {"degrading": "▲", "improving": "▼", "stable": ""}

# Correlation links (mirrors app.conflicts.links.COLORS).
LINK_COLORS = {"conflict": "#b14cff", "entity": "#0091c2"}

# Heatmap palette: intensity (score-normalised level*confidence) -> colour.
# Fixed 8 steps to stay well inside the curses pair budget (~255 for the
# whole screen; see ui/colors.py).
HEATMAP_STOPS = ((0.00, "#3a0d0f"), (0.25, "#7a1a17"), (0.50, "#c22e21"),
                 (0.75, "#ef6a2b"), (1.00, "#ffd36b"))
HEATMAP_STEPS = 8
# Heat cell fill: a full block on a coloured background (one curses pair per
# level, like Terrascope's radar).
HEAT_GLYPH = " "

# Track affiliation colours (mirrors app.cot.model.AFFILIATION_COLORS).
AFFILIATION_COLORS = {"friendly": "#33cc66", "hostile": "#e03c3c",
                      "neutral": "#33bbee", "unknown": "#eecc33"}
TRACK_GLYPH = "◆"

# Node markers.
NODE_COLORS = {"text": "#f2c14e", "video": "#41AEE1"}
NODE_GLYPH = "■"
