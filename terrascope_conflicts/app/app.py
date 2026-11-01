"""The application: curses setup, main loop, screen composition.

Structure follows Terrascope's app package (base/runtime/rendering/input/mouse
mixins folded into one class for size): a poll loop that drains data results,
CoT input and keyboard/mouse events, then repaints only when something
changed. draw() composes: tab bar -> map box (background, heat, links,
markers, legend, scale) -> bottom panel (info/brief/ranking).
"""
from __future__ import annotations

import curses
import time
from datetime import datetime, timezone

from ..core import world as world_mod
from ..core.config import APP_CONFIG, LEVEL_COLORS, TREND_ARROW, UI_CONFIG
from ..core.mapdata import MapDataError
from ..ui.colors import ColorManager
from ..ui.layout import (OUTER_MARGIN, Rect, compute_layout, draw_box, safe_addstr)
from ..ui.map import (_MapArea, draw_map_box, draw_map_overlay_text)

HEARTBEAT_SECONDS = float(APP_CONFIG["heartbeat_seconds"])
FRAME_DELAY_MS = int(APP_CONFIG["frame_delay_ms"])
DRAG_FRAME_DELAY_MS = int(APP_CONFIG["drag_frame_delay_ms"])
LEGEND_TEXT_COLOR = UI_CONFIG["legend_text_color"]

MARKER_GLYPHS = {   # kind -> (glyph, colour key) for selected markers
    "conflict": "\u25c6", "event": "\u25cf", "track": "\u25c6", "node": "\u25a0",
}

PANEL_TABS = ("RANKING", "INFO", "BRIEF")


class ConflictApp:
    def __init__(self, stdscr, world: world_mod.WorldMap, cfg, feed,
                 loader, conflicts_layer, links_layer, tracks_layer, basemap_layer,
                 refresh) -> None:
        self.stdscr = stdscr
        self.world = world
        self.cfg = cfg
        self.feed = feed
        self.loader = loader
        self.refresh = refresh
        self.conflicts_layer = conflicts_layer
        self.links_layer = links_layer
        self.tracks_layer = tracks_layer
        self.basemap_layer = basemap_layer
        self.colors = ColorManager()
        self.running = True
        self.frame = None
        self._frame_size = None
        self.panel_visible = True
        self.panel_tab = 0
        self.info_visible = False
        self.status_message = "loading map data..."
        self.map_error = ""
        self.loading = True
        self.refresh_status = ""      # pipeline progress while a refresh runs
        self._refresh_done_clear_at = 0.0
        self._refresh_button: tuple[int, int, int] | None = None
        # Left-button drag state: [last_x, last_y, start_x, start_y, moved]
        self._drag = None
        self._mouse_events: list = []
        self.rank_scroll = 0
        self.brief_scroll = 0

    # ---- setup / teardown --------------------------------------------------

    def setup(self) -> None:
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        self.colors.setup()
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED
                         | curses.BUTTON4_PRESSED
                         | getattr(curses, "BUTTON5_PRESSED", 0))
        self._request_data()

    def _request_data(self) -> None:
        self.loader.request("conflicts", self._load_conflicts)

    def _load_conflicts(self):
        from .data import load_conflicts
        return load_conflicts(
            PROJECT_DB, self.cfg.aggression.ttl_hours,
            self.cfg.aggression.min_level, self.cfg.aggression.events_days,
            self.cfg.links)

    # ---- main loop ----------------------------------------------------------

    def run(self) -> None:
        self.setup()
        try:
            self._loop()
        finally:
            try:
                self.stdscr.clear()
                self.stdscr.refresh()
            except curses.error:
                pass
            self.colors.restore()

    def _loop(self) -> None:
        last_draw = 0.0
        last_size = None
        while self.running:
            redraw = self._collect()
            redraw = self._poll_refresh() or redraw
            self._poll_tracks()
            for layer in self.world.layers:
                layer.tick()
            for _ in range(32):
                key = self.stdscr.getch()
                if key == -1:
                    break
                if key == curses.KEY_MOUSE:
                    self._handle_mouse()
                else:
                    self.handle_key(key)
                redraw = True
                if not self.running:
                    return
            size = self.stdscr.getmaxyx()
            now = time.monotonic()
            if (redraw or self.frame is None or size != last_size
                    or self.world.needs_render()
                    or now - last_draw >= HEARTBEAT_SECONDS):
                self.draw()
                last_draw = now
                last_size = size
            time.sleep((DRAG_FRAME_DELAY_MS if self._drag else FRAME_DELAY_MS) / 1000)

    def _collect(self) -> bool:
        redraw = False
        for kind, name, payload in self.loader.collect():
            redraw = True
            if kind == "error":
                self.map_error = f"{name}: {payload}"
                continue
            if name == "conflicts":
                self.loading = False
                self._last_data = payload["data"]
                self._brief = (payload["data"] or {}).get("brief", "")
                self.conflicts_layer.set_data(payload["data"],
                                              self.cfg.aggression.ttl_hours,
                                              self.cfg.aggression.min_level)
                self.conflicts_layer.set_events(
                    payload["events"] if self.cfg.aggression.show_events else [])
                self.links_layer.set_links(payload["links"])
                self.status_message = ""
                if payload["data"]:
                    self.status_message = (f"assessed {payload['data']['assessed_at'][:16]}"
                                           f"Z · ranked by {payload['data'].get('ranked_by') or '?'}")
                    if self.refresh_status.startswith("refreshed"):
                        self.refresh_status = (f"refreshed {time.strftime('%H:%M:%S')} "
                                               f"· {len(payload['data']['conflicts'])} conflicts")
                        self._refresh_done_clear_at = time.monotonic() + 8.0
                else:
                    self.status_message = "no ranked run yet — press U to refresh"
        return redraw

    def _poll_refresh(self) -> bool:
        """Drain intel pipeline progress; reload data when the run finishes."""
        redraw = False
        for item in self.refresh.poll():
            redraw = True
            if isinstance(item, tuple):
                kind, detail = item
                if kind == "done":
                    self.refresh_status = (f"refreshed {time.strftime('%H:%M:%S')} "
                                           "— reload from database...")
                    self.loader.request("conflicts", self._load_conflicts)
                elif kind == "error":
                    self.refresh_status = f"refresh failed: {detail}"
                else:
                    self.refresh_status = str(detail)
            else:
                self.refresh_status = str(item)
        if (self.refresh_status.startswith("refreshed")
                and time.monotonic() >= getattr(self, "_refresh_done_clear_at", 0.0)):
            self.refresh_status = ""
            redraw = True
        return redraw

    def _start_refresh(self) -> None:
        if self.refresh.busy:
            return
        self.refresh_status = "refresh: starting pipeline (gather → rank → summarize)..."
        self.refresh.start(gather=True)

    def _poll_tracks(self) -> None:
        snapshot = self.feed.snapshot()
        nodes = self._cached_nodes()
        current = (len(snapshot), len(nodes))
        if current != getattr(self, "_last_track_state", None):
            self._last_track_state = current
            self.tracks_layer.set_data(snapshot, nodes)

    def _cached_nodes(self) -> list[dict]:
        if not hasattr(self, "_nodes"):
            try:
                from .data import load_nodes
                self._nodes = load_nodes(PROJECT_NODES_DB)
            except Exception:
                self._nodes = []
        return self._nodes

    # ---- keys ----------------------------------------------------------------

    def handle_key(self, key: int) -> None:
        if key in (ord("q"), ord("Q")):
            self.running = False
            return
        if key in (ord("u"), ord("U")):
            self._start_refresh()
            return
        if key in (ord("p"), ord("P")):
            self.panel_visible = not self.panel_visible
            self.world.dirty = True
            return
        if key == curses.KEY_F1 or key == ord("!"):
            self.panel_tab = (self.panel_tab + 1) % len(PANEL_TABS)
            self.world.dirty = True
            return
        if key == 12:  # Ctrl+L repaint
            self.stdscr.clearok(True)
            self.world.dirty = True
            return
        if key == curses.KEY_RESIZE:
            self.stdscr.clearok(True)
            self.world.dirty = True
            return
        if key in (ord("\t"), 9):
            # Tab cycles selection (< > also work, like Terrascope)
            self.world.cycle_selection(1)
            return
        if key in (ord("r"), ord("R")):
            self.world.reset_view()
            return
        if key in (ord("+"), ord("=")):
            self.world.zoom_in()
            return
        if key in (ord("-"), ord("_")):
            self.world.zoom_out()
            return
        if key == curses.KEY_UP or key in (ord("w"), ord("W")):
            self.world.pan(0, 1)
            return
        if key == curses.KEY_DOWN or key in (ord("s"), ord("S")):
            self.world.pan(0, -1)
            return
        if key == curses.KEY_LEFT or key in (ord("j"), ord("J")):
            self.world.pan(-1, 0)
            return
        if key == curses.KEY_RIGHT or key in (ord("k"), ord("K")):
            self.world.pan(1, 0)
            return
        if key in (ord("<"), ord(",")):
            self.world.cycle_selection(-1)
            return
        if key in (ord(">"), ord(".")):
            self.world.cycle_selection(1)
            return
        if key in (curses.KEY_ENTER, 10, 13):
            self._focus_selected()
            return
        if key == 27:  # Esc
            self.world.clear_selection()
            self.info_visible = False
            return
        # Layer toggle keys
        for layer in self.world.layers:
            if layer.handle_key(key):
                self.world.clear_selection()
                self.info_visible = False
                self.world.dirty = True
                return

    def _focus_selected(self) -> None:
        layer = self.world.selected_layer
        item = layer.selected if layer else None
        if isinstance(item, tuple) and item[0] in ("track", "node"):
            from .data import load_nodes
            if item[0] == "track":
                track = self.tracks_layer.tracks.get(item[1])
                if track:
                    self.world.focus_location(track["lat"], track["lon"], zoom=8.0)
            else:
                node = next((n for n in self._cached_nodes() if n["uid"] == item[1]), None)
                if node:
                    self.world.focus_location(node["lat"], node["lon"], zoom=8.0)
        elif item is not None and hasattr(item, "lat"):
            self.world.focus_location(item.lat, item.lon, zoom=5.0)

    # ---- mouse ---------------------------------------------------------------

    def _handle_mouse(self) -> None:
        try:
            event_id, x, y, z, state = curses.getmouse()
        except curses.error:
            return
        layout = compute_layout(*self.stdscr.getmaxyx()[::-1], self.panel_visible)
        map_rect = layout[0]
        if state & curses.BUTTON1_PRESSED:
            self._drag = [x, y, x, y, False]
            if self._on_refresh_button(x, y):
                self._drag = None
                return
        elif state & curses.BUTTON1_RELEASED and self._drag:
            self._drag[0], self._drag[1] = x, y
            if not self._drag[4] and self._inside_map(map_rect, x, y):
                self._click_map(x, y, map_rect)
            self._drag = None
        elif state & curses.BUTTON4_PRESSED:
            if self._inside_map(map_rect, x, y):
                self.world.zoom_at(1, x - map_rect[1] - 2, y - map_rect[0] - 1,
                                   map_rect[3] - 4, map_rect[2] - 2)
        elif state & getattr(curses, "BUTTON5_PRESSED", 0):
            if self._inside_map(map_rect, x, y):
                self.world.zoom_at(-1, x - map_rect[1] - 2, y - map_rect[0] - 1,
                                   map_rect[3] - 4, map_rect[2] - 2)

    def _on_refresh_button(self, x: int, y: int) -> bool:
        """True if (x, y) is on the [U] REFRESH border button (and handles it)."""
        button = getattr(self, "_refresh_button", None)
        if button is None:
            return False
        row, x0, x1 = button
        if y == row and x0 <= x < x1:
            self._start_refresh()
            return True
        return False

    @staticmethod
    def _inside_map(rect: Rect, x: int, y: int) -> bool:
        top, left, height, width = rect
        return (top < y < top + height - 1 and left + 2 <= x < left + width - 2)

    def _click_map(self, x: int, y: int, map_rect: Rect) -> None:
        col = x - map_rect[1] - 2
        row = y - map_rect[0] - 1
        self.world.select_at(col, row)
        self.info_visible = self.world.selected_layer is not None
        self.panel_tab = 1 if self.info_visible else self.panel_tab

    # ---- drawing ---------------------------------------------------------------

    def draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        if width < APP_CONFIG["min_terminal_width"] or height < APP_CONFIG["min_terminal_height"]:
            safe_addstr(self.stdscr, 0, 0, "Terminal too small")
            self.stdscr.refresh()
            return
        map_rect, panel_rect = compute_layout(height, width, self.panel_visible)
        frame_size = (map_rect[3] - 4, map_rect[2] - 2)
        if self._frame_size != frame_size:
            self.world.dirty = True
        if self.frame is None or self.world.needs_render():
            self.frame = self.world.render(*frame_size)
            self._frame_size = frame_size
        legend = [line for layer in self.world.layers if layer.enabled
                  for line in layer.legend_lines()]
        draw_map_box(self.stdscr, *map_rect, self.frame, self.colors,
                     self.world.zoom, self.world.center_lat,
                     attribution=self._attribution(), legend=legend)
        self._refresh_button = self._draw_refresh_button(map_rect)
        self._draw_tab_bar(width)
        self._draw_status(map_rect, self.frame)
        if panel_rect is not None:
            self._draw_panel(panel_rect)
        self.stdscr.refresh()

    def _draw_refresh_button(self, map_rect: Rect) -> tuple[int, int, int] | None:
        """[U] REFRESH on the map box's bottom border (Terrascope's border
        buttons). Returns (row, x0, x1) for click handling, or None."""
        if self.refresh.busy:
            label = " [U] REFRESHING… "
            attrs = curses.A_BOLD | curses.A_BLINK
        else:
            label = " [U] REFRESH "
            attrs = curses.A_BOLD
        top, left, height, width = map_rect
        row = top + height - 1
        x = left + 10        # past the "z N.NN" zoom readout
        if x + len(label) > left + width - 30:
            return None
        safe_addstr(self.stdscr, row, x, label, attrs)
        return (row, x, x + len(label))

    def _attribution(self) -> str:
        parts = ["Natural Earth", "UCDP", "intel pipeline"]
        now = datetime.now(timezone.utc)
        return " · ".join(parts) + f"  ·  {now:%H:%M:%S}Z"

    def _draw_tab_bar(self, width: int) -> None:
        margin_x, margin_y = OUTER_MARGIN
        tabs = [("1", "MAP", "#6bcb77"), ("2", "CONFLICTS", "#e0302a")]
        x = margin_x
        text = " CONFLICTS TUI  "
        safe_addstr(self.stdscr, margin_y, x, text, curses.A_BOLD)
        x += len(text)
        for _num, label, color in tabs:
            pass    # single view: the tab bar is a title row
        right = f"A heat={self._heat_state()} links={self._links_state()} tracks={self._tracks_state()}"
        safe_addstr(self.stdscr, margin_y, max(x, width - len(right) - 2), right,
                    curses.A_DIM)

    def _heat_state(self) -> str:
        return "on" if self.conflicts_layer.show_heat else "off"

    def _links_state(self) -> str:
        return "on" if self.links_layer.enabled else "off"

    def _tracks_state(self) -> str:
        return "on" if self.tracks_layer.enabled else "off"

    def _draw_status(self, map_rect: Rect, frame) -> None:
        top, left, height, width = map_rect
        messages = []
        if self.loading:
            messages.append("LOADING INTEL DATA...")
        if self.status_message:
            messages.append(self.status_message)
        if self.refresh_status:
            messages.append(self.refresh_status)
        if self.map_error:
            messages.append(self.map_error)
        for layer in self.world.active_layers():
            messages.extend(layer.loading_messages())
        for index, message in enumerate(messages[:4]):
            draw_map_overlay_text(self.stdscr, map_rect, frame, self.colors,
                                  top + 1 + index, left + 3, message,
                                  curses.A_BOLD if message == self.map_error else curses.A_DIM)
        # Cursor readout, bottom-left over the map like Terrascope's lat/lon.
        zoom_text = f"z {self.world.zoom:5.2f}"
        safe_addstr(self.stdscr, top + height - 1, left + 2, zoom_text, curses.A_DIM)

    def _draw_panel(self, rect: Rect) -> None:
        top, left, height, width = rect
        title = f" {PANEL_TABS[self.panel_tab]}  (F1 switch · P hide) "
        draw_box(self.stdscr, top, left, height, width, title)
        inner_top, inner_height = top + 1, height - 2
        inner_width = width - 4
        if self.panel_tab == 0:
            self._draw_ranking(inner_top, left + 2, inner_height, inner_width)
        elif self.panel_tab == 1:
            self._draw_info(inner_top, left + 2, inner_height, inner_width)
        else:
            self._draw_brief(inner_top, left + 2, inner_height, inner_width)

    def _draw_ranking(self, top: int, left: int, height: int, width: int) -> None:
        points = sorted(self.conflicts_layer.conflicts, key=lambda p: p.rank)
        if not points:
            safe_addstr(self.stdscr, top, left,
                        "No conflict assessment yet — run: make -C new update-fast", curses.A_DIM)
            return
        visible = points[self.rank_scroll: self.rank_scroll + height]
        for offset, point in enumerate(visible):
            bar = "■" * point.level + "□" * (5 - point.level)
            arrow = TREND_ARROW.get(point.trend, "")
            color = LEVEL_COLORS[point.level]
            state = "" if not point.expired else "  EXPIRED"
            text = f"{point.rank:>2}. {bar} {arrow} {point.label[:width - 16]}{state}"
            attributes = self.colors.get_pair(color) if point.level else curses.A_DIM
            if point.expired:
                attributes = curses.A_DIM
            safe_addstr(self.stdscr, top + offset, left, text[:width], attributes)

    def _draw_info(self, top: int, left: int, height: int, width: int) -> None:
        layer = self.world.selected_layer
        item = layer.selected if layer else None
        if layer is None or item is None:
            safe_addstr(self.stdscr, top, left, "click a diamond, track or node", curses.A_DIM)
            return
        rows = layer.info_rows()
        for offset, (label, value) in enumerate(rows[: height]):
            safe_addstr(self.stdscr, top + offset, left, f"{label:<10}", curses.A_BOLD)
            safe_addstr(self.stdscr, top + offset, left + 11, value[: max(0, width - 11)])
        if not isinstance(item, tuple) and hasattr(item, "summary"):
            summary_top = top + min(height, len(rows) + 1)
            self._wrap_text(item.summary, summary_top, left,
                            height - (summary_top - top), width)

    def _draw_brief(self, top: int, left: int, height: int, width: int) -> None:
        brief = getattr(self, "_brief", "")
        if not brief:
            safe_addstr(self.stdscr, top, left, "(no world brief in this run)", curses.A_DIM)
            return
        self._wrap_text(brief, top, left, height, width)

    def _wrap_text(self, text: str, top: int, left: int, height: int, width: int) -> None:
        import textwrap
        for offset, line in enumerate(textwrap.wrap(text, width)[: height]):
            safe_addstr(self.stdscr, top + offset, left, line)


# set by main.py after config load (module-level for _load_conflicts)
PROJECT_DB = None
PROJECT_NODES_DB = None
