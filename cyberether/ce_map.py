"""Conflict map source for CyberEther.

This is the module the flowgraph's Python blocks import. It owns:

- PROJECT paths (DB, geo data) resolved from this file's location
- load_snapshot()      -> {conflicts, events, links} from newsgather.db
- resolve() factories  -> anchor spec -> (lat, lon)
- render_frame()       -> F32 [480, 960, 4] tensor for the `frame` block

The block code in the flowgraph YAML is small: it imports this module,
calls load_snapshot() on an interval, renders, and publishes ranking text
to ctx.env for display blocks.
"""

from __future__ import annotations

import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in __import__("sys").path:
    import sys
    sys.path.insert(0, str(PACKAGE_ROOT))

# Companion layout: lives inside the conflictmap repo, consumes the TUI's DB
# (conflictmap/data/newsgather.db) — one source of truth, cwd-independent.
TUI_ROOT = PACKAGE_ROOT.parent
DB_PATH = TUI_ROOT / "data" / "newsgather.db"
REFRESH_SECONDS = 30.0          # re-read the DB at most this often
_ranking_cache: list[str] = []
_last_load = 0.0
_snapshot: dict | None = None


def _by_key(conflicts: list[dict]) -> dict[str, tuple[float, float]]:
    return {c["key"]: (c["lat"], c["lon"])
            for c in conflicts if c.get("lat") is not None and c.get("lon") is not None}


def make_resolver(conflicts: list[dict]):
    """spec like 'conflict:haiti' -> (lat, lon) | None."""
    table = {f"conflict:{k}": v for k, v in _by_key(conflicts).items()}
    return lambda spec: table.get(spec)


def load_snapshot(force: bool = False) -> dict | None:
    """{conflicts, events, links, ranking_lines, assessed_at} or the cached one."""
    global _snapshot, _last_load, _ranking_cache
    now = time.monotonic()
    if not force and _snapshot is not None and now - _last_load < REFRESH_SECONDS:
        return _snapshot
    from intel import correlate, pipeline

    data = pipeline.load_latest(DB_PATH)
    events = pipeline.recent_events(DB_PATH, days=30)
    conflicts = (data or {}).get("conflicts", [])

    links = correlate.conflict_links(data, DB_PATH, min_strength=2.0, min_level=1)
    links = [{"a": l.a, "b": l.b, "kind": l.kind, "strength": l.strength,
              "reason": (l.short if hasattr(l, "short") else "")} for l in links]

    ranking = [f"{r['rank']:>2}. L{r['situation_level']} {r['conflict']}"
               for r in conflicts[:12]]
    _ranking_cache = ranking
    _snapshot = {
        "conflicts": conflicts,
        "events": events,
        "links": links,
        "ranking_lines": ranking,
        "assessed_at": (data or {}).get("assessed_at", "never"),
        "ranked_by": (data or {}).get("ranked_by", "-"),
    }
    _last_load = now
    return _snapshot


def render_frame(snapshot: dict):
    """RGBA frame tensor via mapraster (imported lazily: heavy geojson parse)."""
    import mapraster
    conflicts = snapshot["conflicts"]
    return mapraster.render(conflicts, snapshot["events"], snapshot["links"],
                            make_resolver(conflicts))


def ranking_text(snapshot: dict) -> str:
    """Multiline ranking summary for a note block."""
    lines = snapshot["ranking_lines"]
    assessed = snapshot["assessed_at"][:16].replace("T", " ") if snapshot["assessed_at"] != "never" else "no run yet"
    header = f"CONFLICTS — assessed {assessed}Z (ranked by {snapshot['ranked_by']})"
    body = "\n".join(lines) if lines else "no ranked run — run the intel pipeline"
    return f"{header}\n{body}"
