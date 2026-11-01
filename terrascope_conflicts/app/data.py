"""Data coordination: loads intel data and node/track state off the UI thread.

The Qt app's IntelWorker + signal pattern becomes a simple pending-result
queue the main loop drains (same as Terrascope's map_data_queue):
  - conflict data from intel.pipeline.load_latest / recent_events
  - correlation links from intel.correlate.conflict_links / entity_links
  - nodes from data/nodes.db (sqlite; the app also writes its own markers
    there when the Nodes tab is used — this TUI only reads)
  - tracks arrive over CoT multicast through the vendored cot package

All paths resolve against the package root (parents[2]), never the CWD, so
the app works from any working directory.
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

# tui/terrascope_conflicts/app/data.py → parents: app, terrascope_conflicts, tui
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PACKAGE_ROOT / "data"
PROJECT_NODES_DB = DATA_DIR / "nodes.db"


class DataLoader:
    """Fetches snapshots in background threads; results on a queue."""

    def __init__(self) -> None:
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    def request(self, name: str, fn: Callable[[], Any], *args) -> None:
        with self._lock:
            if name in self._pending:
                return
            self._pending.add(name)

        def worker() -> None:
            try:
                result = ("ok", name, fn(*args))
            except Exception as exc:  # noqa: BLE001 - surfaced as a status line
                result = ("error", name, f"{type(exc).__name__}: {exc}")
            self.queue.put(result)

        threading.Thread(target=worker, name=f"load-{name}", daemon=True).start()

    def collect(self) -> list[tuple[str, str, Any]]:
        results = []
        while True:
            try:
                kind, name, payload = self.queue.get_nowait()
            except queue.Empty:
                return results
            with self._lock:
                self._pending.discard(name)
            results.append((kind, name, payload))


NODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    uid TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    attachment TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    remote INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL DEFAULT 'text',
    data TEXT NOT NULL DEFAULT '{}'
)
"""


def ensure_nodes_db(db_path: Path) -> None:
    """Create the nodes store on first run (it ships empty — runtime state is
    not committed). One placeholder row so the correlation layer has a target."""
    if db_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(NODES_SCHEMA)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT OR IGNORE INTO nodes (uid, lat, lon, title, text, created, updated,"
            " author, remote, type) VALUES ('home', 0.0, 0.0, 'Home',"
            " 'Starter node: your own marker. Edit or delete in any sqlite client.',"
            " ?, ?, 'starter', 0, 'marker')", (now, now))
        conn.commit()
    finally:
        conn.close()


def load_nodes(db_path: Path) -> list[dict]:
    """Nodes from the local store, read-only."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT uid, lat, lon, title, type FROM nodes").fetchall()
        return [dict(r) for r in rows if r["lat"] is not None and r["lon"] is not None]
    finally:
        conn.close()


def load_conflicts(intel_db: Path, ttl_hours: float, min_level: int,
                   events_days: int, links_cfg) -> dict[str, Any]:
    """One combined snapshot: ranking + events + both link families."""
    from ..intel import correlate, pipeline
    data = pipeline.load_latest(intel_db)
    events = pipeline.recent_events(intel_db, events_days)
    conflict_links = correlate.conflict_links(data, intel_db, links_cfg.min_strength, min_level)
    entities: list[dict] = []
    nodes = load_nodes(PROJECT_NODES_DB)
    if links_cfg.nodes:
        entities += [{"spec": f"node:{n['uid']}", "lat": n["lat"], "lon": n["lon"],
                      "text": f"{n.get('title') or ''}"} for n in nodes]
    entity_links = correlate.entity_links(
        entities, [c for c in (data or {}).get("conflicts", [])
                   if c["situation_level"] >= min_level],
        events, links_cfg.radius_km, links_cfg.entity_min_strength)
    return {"data": data, "events": events,
            "links": conflict_links + entity_links}
