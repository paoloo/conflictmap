"""Entry point: load config.toml, build the stack, run curses."""
from __future__ import annotations

import argparse
import curses
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]   # tui/

from conflictmap.app.app import ConflictApp            # noqa: E402
from conflictmap.app import data as data_mod           # noqa: E402
from conflictmap.app.data import DataLoader               # noqa: E402
from conflictmap.app.refresh import RefreshWorker         # noqa: E402
from conflictmap.app.tracks import TrackFeed           # noqa: E402
from conflictmap.core import world as world_mod        # noqa: E402
from conflictmap.core.config import MAP_CONFIG         # noqa: E402
from conflictmap.core.mapdata import (MapDataError,     # noqa: E402
                                               load_country_labels, load_places,
                                               load_polygons)
from conflictmap.layers.basemap import BasemapLayer     # noqa: E402
from conflictmap.layers.conflicts import ConflictLayer  # noqa: E402
from conflictmap.layers.links import LinksLayer         # noqa: E402
from conflictmap.layers.tracks import TracksLayer       # noqa: E402


def build(config_path: Path | None):
    from conflictmap.app.config import load_config, AppConfig
    cfg = load_config(str(config_path)) if config_path and config_path.exists() else AppConfig()

    # intel.db is relative to the package root, never the CWD (refresh and
    # reads must hit the same file regardless of where the app was started).
    db = Path(cfg.intel.db)
    data_mod.PROJECT_DB = db if db.is_absolute() else (PACKAGE_ROOT / db).resolve()
    ConflictApp._load_conflicts.__globals__["PROJECT_DB"] = data_mod.PROJECT_DB

    basemap = BasemapLayer()
    conflicts = ConflictLayer()
    tracks_layer = TracksLayer()
    links = LinksLayer(resolve=_make_resolver(conflicts, tracks_layer, data_mod))
    return cfg, basemap, conflicts, links, tracks_layer


def _make_resolver(conflicts_layer, tracks_layer, data_mod):
    nodes_cache: list[dict] | None = None

    def resolve(spec: str):
        nonlocal nodes_cache
        kind, _, ident = spec.partition(":")
        if kind == "conflict":
            point = next((p for p in conflicts_layer.conflicts if p.key == ident), None)
            if point is None or point.lat is None:
                return None
            return (point.lat, point.lon)
        if kind == "node":
            if nodes_cache is None:
                try:
                    nodes_cache = data_mod.load_nodes(data_mod.PROJECT_NODES_DB)
                except Exception:
                    nodes_cache = []
            node = next((n for n in nodes_cache if n["uid"] == ident), None)
            if node is None:
                return None
            return (node["lat"], node["lon"])
        if kind == "track":
            track = tracks_layer.tracks.get(ident)
            if track is None:
                return None
            return (track["lat"], track["lon"])
        return None

    return resolve


def load_map_data():
    rings = load_polygons("ne_50m_admin_0_countries.geojson")
    labels = load_country_labels()
    places = load_places()
    return rings, labels, places


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Terminal world-conflict map (Terrascope-style braille rendering of the intel pipeline)")
    parser.add_argument("--config", default=str(PACKAGE_ROOT / "config.toml"),
                        help="config.toml (default: config.toml next to the package)")
    parser.add_argument("--no-cot", action="store_true", help="disable the CoT listener")
    args = parser.parse_args(argv)

    # curses needs a real terminal on stdin/stdout. `conda run` without
    # --no-capture-output pipes them and cbreak() fails instantly.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("conflictmap needs a terminal (stdin/stdout are not a TTY).\n"
              "Run it as:\n"
              "  conda run --no-capture-output -n core python -m conflictmap\n"
              "or activate the env first:\n"
              "  conda activate core && python -m conflictmap",
              file=sys.stderr)
        return 2

    try:
        rings, labels, places = load_map_data()
    except MapDataError as error:
        print(f"map data: {error}", file=sys.stderr)
        return 1

    cfg, basemap, conflicts, links, tracks_layer = build(Path(args.config))
    data_mod.ensure_nodes_db(data_mod.PROJECT_NODES_DB)
    basemap.set_data(labels, places)

    layers = [basemap, links, conflicts, tracks_layer]
    prepared = world_mod.prepare_ring_set(rings)
    world = world_mod.WorldMap([], layers)
    world.set_prepared_countries(prepared)

    # Receive-only CoT: no self-SA beacon — the TUI doesn't broadcast a position.
    feed = TrackFeed(enabled=cfg.cot.enabled and not args.no_cot,
                     group=cfg.cot.group, port=cfg.cot.port)
    feed.start()

    loader = DataLoader()
    refresh = RefreshWorker(cfg.intel, PACKAGE_ROOT)

    def curses_main(stdscr) -> None:
        app = ConflictApp(stdscr, world, cfg, feed, loader,
                          conflicts, links, tracks_layer, basemap, refresh)
        app.feed_error = feed.error
        app.run()

    try:
        curses.wrapper(curses_main)
    finally:
        feed.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
