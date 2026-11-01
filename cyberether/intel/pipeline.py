"""World-conflict intel pipeline: gather -> rank -> summarize -> brief.

One entry point (`run`) used by the map app's background worker and by the CLI:

  1. gather     intel.gather pulls RSS/APIs/UCDP into data/newsgather.db
  2. candidates classify.match_articles: loose entity/keyword match per conflict
  3. rank       bge-reranker-v2-m3 (intel.reranker) scores every candidate
                against the conflict's query; off-topic keyword hits drop out
                below `min_score`; the best `keep` survive. Without a reranker
                the newest `keep` candidates are used (ranked_by = "recency").
  4. summarize  the local chat model scores each conflict that has evidence
                (level 0-5, trend, confidence, summary, evidence) with its last
                assessment as context for the trend. Conflicts without
                evidence get level 0 without a model call.
  5. score      score = 40*level + trend +/-6 + 10*confidence + UCDP fatalities
                (log, <=10) + article volume (<=4); the bonuses (max 36) never
                outweigh one level, they order conflicts within a level.
  6. brief      one model call writes a short world brief over the top conflicts.

Everything persists in the same DB: conflict_sa (one row per conflict per run,
now with rank/score/lat/lon/articles) and intel_briefs.

  python -m intel.pipeline                 # full run, prints the ranking
  python -m intel.pipeline --no-gather     # rank + summarize existing corpus
  python -m intel.pipeline --no-llm        # gather + rank only (no model calls)
  python -m intel.pipeline --latest        # print the latest stored result
  python -m intel.pipeline --config config.toml   # use the app's [intel] settings
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import classify
from .llm import DEFAULT_MODEL, LLM, LLMError
from .reranker import DEFAULT_MODEL as DEFAULT_RERANKER
from .reranker import DEFAULT_PORT as DEFAULT_RERANKER_PORT
from .reranker import Reranker, RerankerUnavailable

DEFAULT_DB = Path("data/newsgather.db")

# Map anchor (lat, lon), UCDP `country` prefixes, and the reranker query per
# tracked conflict (keys from classify.TRACKED_CONFLICTS).
CONFLICT_GEO: dict[str, dict[str, Any]] = {
    "ukraine_russia": {"at": (48.0, 37.8), "ucdp": ("Ukraine", "Russia")},
    "gaza_israel": {"at": (31.42, 34.38), "ucdp": ("Israel",)},
    "iran_israel": {"at": (33.0, 51.0), "ucdp": ("Iran",)},
    "lebanon_hezbollah": {"at": (33.27, 35.4), "ucdp": ("Lebanon",)},
    "yemen_red_sea": {"at": (15.35, 44.2), "ucdp": ("Yemen",)},
    "taiwan_china": {"at": (24.0, 119.8), "ucdp": ("Taiwan",)},
    "south_china_sea": {"at": (12.0, 114.0), "ucdp": ("Philippines",)},
    "korea": {"at": (38.3, 127.0), "ucdp": ("North Korea", "South Korea")},
    "india_pakistan": {"at": (34.1, 74.8), "ucdp": ("India",)},     # Pakistan's internal war: afghanistan_pakistan
    "sudan": {"at": (13.6, 25.35), "ucdp": ("Sudan", "South Sudan")},
    "sahel": {"at": (14.5, 0.5), "ucdp": ("Mali", "Burkina Faso", "Niger", "Nigeria", "Chad")},
    "congo": {"at": (-1.68, 29.22), "ucdp": ("DR Congo",)},
    "myanmar": {"at": (21.0, 96.0), "ucdp": ("Myanmar",)},
    "haiti": {"at": (18.54, -72.34), "ucdp": ("Haiti",)},
    "mexico_cartels": {"at": (24.8, -107.4), "ucdp": ("Mexico",)},
    "venezuela": {"at": (10.5, -66.9), "ucdp": ("Venezuela",)},
    "syria": {"at": (35.0, 38.5), "ucdp": ("Syria",)},
    "iraq": {"at": (33.3, 44.4), "ucdp": ("Iraq",)},
    "armenia_azerbaijan": {"at": (39.8, 46.75), "ucdp": ("Armenia", "Azerbaijan")},
    "somalia_alshabaab": {"at": (2.05, 45.32), "ucdp": ("Somalia",)},
    "france_unrest": {"at": (48.86, 2.35), "ucdp": ("France",)},
    "balkans": {"at": (42.66, 21.16), "ucdp": ("Serbia", "Kosovo", "Albania", "Bosnia", "Montenegro",
                                               "Macedonia", "North Macedonia")},
    "nato_east": {"at": (54.4, 23.0), "ucdp": ("Poland", "Estonia", "Latvia", "Lithuania", "Finland",
                                               "Moldova", "Belarus")},
    "libya": {"at": (32.89, 13.19), "ucdp": ("Libya",)},
    "maghreb": {"at": (36.75, 3.06), "ucdp": ("Algeria", "Morocco", "Tunisia", "Western Sahara")},
    "ethiopia": {"at": (11.6, 37.4), "ucdp": ("Ethiopia", "Eritrea")},
    "afghanistan_pakistan": {"at": (33.9, 70.5), "ucdp": ("Afghanistan", "Pakistan")},
}

TREND_POINTS = {"degrading": 6.0, "stable": 0.0, "improving": -6.0}

CONFLICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "situation_level": {"type": "integer", "minimum": 0, "maximum": 5},
        "trend": {"type": "string", "enum": ["improving", "stable", "degrading"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["situation_level", "trend", "confidence", "summary", "evidence"],
}

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"brief": {"type": "string"}},
    "required": ["brief"],
}

CONFLICT_SYSTEM = """\
You are a geopolitical analyst writing a situational-awareness assessment of ONE \
conflict from a ranked list of recent news items (most relevant first) and \
UCDP conflict-event statistics. Assess:
- situation_level 0-5: 0 quiet, 1 latent tension, 2 rising tension/incidents, \
3 active hostilities/limited clashes, 4 sustained combat/major escalation risk, \
5 full-scale war/humanitarian catastrophe.
- trend vs the previous assessment (if given) and the tone of the items: \
improving | stable | degrading.
- confidence 0-1: how well the items support the assessment; low if few or thin.
- summary: two or three factual sentences naming concrete recent events.
- evidence: up to 5 short quotes/paraphrases, each prefixed with its [n] item \
number and source. Only from the items. Never invent events.
Reply with JSON only."""

BRIEF_SYSTEM = """\
You write a world conflict brief for an operator. You get ranked conflict \
assessments (rank, level 0-5, trend, summary). Write 4-6 sentences: what is \
most severe, what is escalating, what changed. Use only the given assessments. \
Reply with JSON only: {"brief": "..."}"""


@dataclass
class Settings:
    db: Path = DEFAULT_DB
    gather: bool = True
    llm: bool = True
    model: str = DEFAULT_MODEL
    ollama_host: str = "http://127.0.0.1:11434"
    reranker_model: str = DEFAULT_RERANKER
    reranker_port: int = DEFAULT_RERANKER_PORT
    hours: int = 72          # article window
    candidates: int = 80     # loose matches per conflict sent to the reranker
    max_age_days: int = 14   # publish-date cap: search feeds resurface years-old pages
    keep: int = 12           # ranked articles per conflict kept as evidence
    min_score: float = -5.0  # reranker logit floor; below = off-topic
    ucdp_days: int = 60


@dataclass
class RunResult:
    assessed_at: str
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    brief: str = ""
    ranked_by: str = "reranker"
    log: list[str] = field(default_factory=list)


Progress = Callable[[str], None]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

_NEW_COLUMNS = {
    "rank": "INTEGER", "score": "REAL", "lat": "REAL", "lon": "REAL",
    "articles_json": "TEXT NOT NULL DEFAULT '[]'", "fatalities": "INTEGER NOT NULL DEFAULT 0",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive migration of the newsgather schema (old DBs keep working)."""
    from .gather import SCHEMA
    conn.executescript(SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(conflict_sa)")}
    for name, decl in _NEW_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE conflict_sa ADD COLUMN {name} {decl}")
    conn.execute("""CREATE TABLE IF NOT EXISTS intel_briefs (
        assessed_at TEXT PRIMARY KEY, brief TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '', ranked_by TEXT NOT NULL DEFAULT '',
        duration_s REAL NOT NULL DEFAULT 0)""")
    conn.commit()


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def rerank_query(conflict: dict[str, str]) -> str:
    if conflict.get("focus"):           # unrest / hybrid situations: not phrased as war
        return f"{conflict['focus']}: {conflict['label']}"
    return (f"Armed conflict, military operations, attacks, casualties, escalation or "
            f"ceasefire talks: {conflict['label']}")


def rank_candidates(candidates: dict[str, list[dict[str, Any]]], settings: Settings,
                    reranker: Reranker | None, progress: Progress) -> tuple[dict[str, list[dict]], str]:
    """Keep the `keep` most relevant articles per conflict. Returns (kept, ranked_by)."""
    kept: dict[str, list[dict[str, Any]]] = {}
    ranked_by = "reranker" if reranker is not None else "recency"
    for conflict in classify.TRACKED_CONFLICTS:
        key = conflict["key"]
        # one article can match several conflicts; copy so scores don't collide
        items = [dict(a) for a in candidates.get(key) or []]
        if not items:
            kept[key] = []
            continue
        if ranked_by == "reranker":
            try:
                scores = reranker.scores(rerank_query(conflict),  # type: ignore[union-attr]
                                         [f"{a['title']}. {a['summary']}" for a in items])
                for article, score in zip(items, scores):
                    article["relevance"] = round(score, 3)
                relevant = [a for a in items if a["relevance"] >= settings.min_score]
                relevant.sort(key=lambda a: a["relevance"], reverse=True)
                kept[key] = relevant[:settings.keep]
                progress(f"rank {key}: {len(items)} candidates -> {len(kept[key])} relevant")
                continue
            except RerankerUnavailable as exc:
                progress(f"reranker unavailable ({exc}); falling back to recency")
                ranked_by = "recency"
        kept[key] = items[:settings.keep]
    return kept, ranked_by


def ucdp_stats(conn: sqlite3.Connection, days: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT country, title, fatalities, occurred_at FROM events WHERE kind='ucdp' "
        "AND occurred_at >= date('now', ?) ORDER BY fatalities DESC", (f"-{days} day",)).fetchall()
    stats: dict[str, dict[str, Any]] = {}
    for key, geo in CONFLICT_GEO.items():
        mine = [r for r in rows if str(r["country"]).startswith(geo["ucdp"])]
        stats[key] = {
            "events": len(mine),
            "fatalities": sum(int(r["fatalities"] or 0) for r in mine),
            "top": [f"{r['occurred_at']}: {r['title']} ({r['fatalities']} killed)" for r in mine[:3]],
        }
    return stats


def previous_assessments(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    latest = conn.execute("SELECT MAX(assessed_at) FROM conflict_sa").fetchone()[0]
    if not latest:
        return {}
    return {r["key"]: r for r in conn.execute(
        "SELECT * FROM conflict_sa WHERE assessed_at = ?", (latest,))}


def conflict_prompt(conflict: dict[str, str], articles: list[dict[str, Any]],
                    ucdp: dict[str, Any], previous: sqlite3.Row | None, days: int) -> str:
    lines = [f"Conflict: {conflict['label']} (countries {conflict['countries']})", ""]
    if previous is not None:
        lines += [f"Previous assessment ({previous['assessed_at'][:16]}Z): level "
                  f"{previous['situation_level']}, {previous['trend']}. {previous['summary']}", ""]
    lines.append(f"UCDP last {days} days: {ucdp['events']} events, {ucdp['fatalities']} fatalities.")
    lines += [f"  - {t}" for t in ucdp["top"]]
    lines += ["", "News items (most relevant first):"]
    for i, a in enumerate(articles, 1):
        lines.append(f"[{i}] {a['source']} ({str(a['published_at'])[:10]}): {a['title']}. {a['summary']}")
    if not articles:
        lines.append("(none)")
    return "\n".join(lines)


def score(record: dict[str, Any]) -> float:
    value = (40.0 * record["situation_level"] + TREND_POINTS.get(record["trend"], 0.0)
             + 10.0 * record["confidence"]
             + min(10.0, 2.0 * math.log10(1 + record["fatalities"]))
             + min(4.0, len(record["articles"]) / 3.0))
    return round(value, 2)


def summarize(llm: LLM | None, conflict: dict[str, str], articles: list[dict[str, Any]],
              ucdp: dict[str, Any], previous: sqlite3.Row | None, settings: Settings) -> dict[str, Any]:
    base = {"situation_level": 0, "trend": "stable", "confidence": 0.0,
            "summary": "No relevant reporting in the window.", "evidence": []}
    if not articles and not ucdp["events"]:
        return base
    if llm is None:
        return {**base, "summary": f"{len(articles)} relevant items, {ucdp['events']} UCDP events "
                                   "(summarization off).",
                "situation_level": previous["situation_level"] if previous is not None else 0}
    prompt = conflict_prompt(conflict, articles, ucdp, previous, settings.ucdp_days)
    parsed = llm.complete_json(CONFLICT_SYSTEM, prompt, CONFLICT_SCHEMA, num_ctx=8192)
    return {
        "situation_level": max(0, min(5, int(parsed["situation_level"]))),
        "trend": str(parsed["trend"]),
        "confidence": max(0.0, min(1.0, float(parsed["confidence"]))),
        "summary": str(parsed["summary"]).strip(),
        "evidence": [str(e) for e in parsed.get("evidence") or []][:5],
    }


def write_brief(llm: LLM, records: list[dict[str, Any]]) -> str:
    top = [r for r in records if r["situation_level"] > 0][:8]
    if not top:
        return "No conflict with relevant reporting in the window."
    text = "\n".join(f"#{r['rank']} {r['conflict']}: level {r['situation_level']}, {r['trend']}. "
                     f"{r['summary']}" for r in top)
    return str(llm.complete_json(BRIEF_SYSTEM, text, BRIEF_SCHEMA, num_ctx=8192)["brief"]).strip()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def run(settings: Settings, progress: Progress = print,
        cancelled: Callable[[], bool] = lambda: False) -> RunResult:
    started = time.monotonic()
    result = RunResult(assessed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def say(msg: str) -> None:
        result.log.append(msg)
        progress(msg)

    if settings.gather:
        from . import gather as gather_mod
        say("gather: fetching sources")
        gather_mod.gather(settings.db, [])
    conn = _connect(settings.db)
    reranker: Reranker | None = None
    try:
        candidates = classify.match_articles(conn, settings.hours, settings.candidates,
                                             settings.max_age_days)
        say(f"candidates: {sum(len(v) for v in candidates.values())} matches "
            f"over {len(candidates)} conflicts ({settings.hours} h window)")
        reranker = Reranker(settings.reranker_model, settings.reranker_port,
                            log_path=settings.db.parent / "reranker.log")
        kept, result.ranked_by = rank_candidates(candidates, settings, reranker, say)
        reranker.close()
        stats = ucdp_stats(conn, settings.ucdp_days)
        previous = previous_assessments(conn)
        llm = LLM(model=settings.model, host=settings.ollama_host, keep_alive="2m") if settings.llm else None
        for conflict in classify.TRACKED_CONFLICTS:
            if cancelled():
                say("cancelled")
                return result
            key = conflict["key"]
            try:
                assessed = summarize(llm, conflict, kept[key], stats[key], previous.get(key), settings)
            except (LLMError, OSError, ValueError, KeyError) as exc:
                say(f"summarize {key}: model failed ({str(exc)[:120]})")
                assessed = {"situation_level": 0, "trend": "stable", "confidence": 0.0,
                            "summary": "Model failed; see run log.", "evidence": []}
            lat, lon = CONFLICT_GEO[key]["at"]
            record = {"key": key, "conflict": conflict["label"], "countries": conflict["countries"],
                      **assessed, "lat": lat, "lon": lon, "fatalities": stats[key]["fatalities"],
                      "ucdp_events": stats[key]["events"], "articles": kept[key],
                      "assessed_at": result.assessed_at}
            record["score"] = score(record)
            result.conflicts.append(record)
            say(f"summarize {key}: level {record['situation_level']} {record['trend']}")
        result.conflicts.sort(key=lambda r: r["score"], reverse=True)
        for rank, record in enumerate(result.conflicts, 1):
            record["rank"] = rank
        if llm is not None:
            try:
                result.brief = write_brief(llm, result.conflicts)
            except (LLMError, OSError, ValueError, KeyError) as exc:
                say(f"brief: model failed ({str(exc)[:120]})")
        save(conn, result, settings.model if settings.llm else "", time.monotonic() - started)
        say(f"done: {len(result.conflicts)} conflicts ranked by {result.ranked_by} "
            f"in {time.monotonic() - started:.0f} s")
        return result
    finally:
        if reranker is not None:
            reranker.close()
        conn.close()


def save(conn: sqlite3.Connection, result: RunResult, model: str, duration: float) -> None:
    for r in result.conflicts:
        conn.execute(
            """INSERT OR REPLACE INTO conflict_sa
               (assessed_at, key, conflict, situation_level, trend, confidence, summary,
                evidence_json, rank, score, lat, lon, articles_json, fatalities)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["assessed_at"], r["key"], r["conflict"], r["situation_level"], r["trend"],
             r["confidence"], r["summary"],
             json.dumps({"evidence": r["evidence"], "signals": {}, "item_count": len(r["articles"]),
                         "ucdp_events": r["ucdp_events"], "model": model,
                         "ranked_by": result.ranked_by}, ensure_ascii=False),
             r["rank"], r["score"], r["lat"], r["lon"],
             json.dumps([{k: a.get(k) for k in ("id", "source", "title", "link", "published_at",
                                                "relevance")} for a in r["articles"]],
                        ensure_ascii=False),
             r["fatalities"]))
    conn.execute("INSERT OR REPLACE INTO intel_briefs VALUES (?,?,?,?,?)",
                 (result.assessed_at, result.brief, model, result.ranked_by, round(duration, 1)))
    conn.commit()


def load_latest(db: Path) -> dict[str, Any] | None:
    """Latest ranked run as plain dicts (rank order), or None. Read-only."""
    if not Path(db).exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(conflict_sa)")}
        if "intel_briefs" not in tables or "rank" not in cols:
            return None
        latest = conn.execute("SELECT MAX(assessed_at) FROM conflict_sa WHERE rank IS NOT NULL").fetchone()[0]
        if not latest:
            return None
        brief = conn.execute("SELECT * FROM intel_briefs WHERE assessed_at=?", (latest,)).fetchone()
        conflicts = []
        for row in conn.execute("SELECT * FROM conflict_sa WHERE assessed_at=? ORDER BY rank", (latest,)):
            extra = json.loads(row["evidence_json"] or "{}")
            conflicts.append({
                "key": row["key"], "conflict": row["conflict"], "rank": row["rank"],
                "score": row["score"], "situation_level": row["situation_level"],
                "trend": row["trend"], "confidence": row["confidence"], "summary": row["summary"],
                "evidence": extra.get("evidence") or [], "ucdp_events": extra.get("ucdp_events", 0),
                "lat": row["lat"], "lon": row["lon"], "fatalities": row["fatalities"],
                "articles": json.loads(row["articles_json"] or "[]"), "assessed_at": row["assessed_at"],
            })
        return {"assessed_at": latest, "brief": brief["brief"] if brief else "",
                "ranked_by": brief["ranked_by"] if brief else "", "model": brief["model"] if brief else "",
                "conflicts": conflicts}
    finally:
        conn.close()


def recent_events(db: Path, days: int = 30, min_fatalities: int = 1) -> list[dict[str, Any]]:
    """UCDP events with coordinates for the map's event dots. Read-only."""
    if not Path(db).exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, country, occurred_at, lat, lon, fatalities FROM events "
            "WHERE kind='ucdp' AND lat IS NOT NULL AND lon IS NOT NULL AND fatalities >= ? "
            "AND occurred_at >= date((SELECT MAX(occurred_at) FROM events WHERE kind='ucdp'), ?)",
            (min_fatalities, f"-{days} day")).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def render(data: dict[str, Any]) -> str:
    out = [f"World conflict ranking {data['assessed_at']} (ranked by {data.get('ranked_by', '?')})", ""]
    if data.get("brief"):
        out += [data["brief"], ""]
    mark = {"improving": "+", "stable": "=", "degrading": "-"}
    for r in data["conflicts"]:
        bar = "#" * r["situation_level"] + "." * (5 - r["situation_level"])
        out.append(f"{r['rank']:>2}. [{bar}] {mark.get(r['trend'], '?')} score={r['score']:5.1f} "
                   f"conf={r['confidence']:.1f} arts={len(r['articles']):>2} "
                   f"ucdp_dead={r['fatalities']:<5} {r['conflict']}")
    return "\n".join(out)


def settings_from_config(path: str | Path) -> Settings:
    """[intel] of the app's config.toml (same mapping as app/conflicts/worker.py)."""
    from conflictmap_ce.app_config import load_config
    c = load_config(str(path)).intel
    return Settings(db=Path(c.db), gather=c.gather, llm=c.summarize, model=c.model,
                    ollama_host=c.ollama_host, reranker_model=c.reranker_model,
                    reranker_port=c.reranker_port, hours=c.hours, max_age_days=c.max_age_days,
                    keep=c.keep, min_score=c.min_score)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="take settings from the app's config.toml [intel]; flags override")
    p.add_argument("--db")
    p.add_argument("--no-gather", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--model")
    p.add_argument("--hours", type=int)
    p.add_argument("--keep", type=int)
    p.add_argument("--min-score", type=float)
    p.add_argument("--latest", action="store_true", help="print the latest stored run and exit")
    args = p.parse_args()
    settings = settings_from_config(args.config) if args.config else Settings()
    if args.db:
        settings.db = Path(args.db)
    if args.no_gather:
        settings.gather = False
    if args.no_llm:
        settings.llm = False
    for name in ("model", "hours", "keep", "min_score"):
        if getattr(args, name) is not None:
            setattr(settings, name, getattr(args, name))
    db = settings.db
    if args.latest:
        data = load_latest(db)
        print(render(data) if data else "(no ranked run yet)")
        return 0
    run(settings)
    data = load_latest(db)
    if data:
        print()
        print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
