"""Local-model conflict situational-awareness classifier.

Reads articles/events from newsgather.db, builds per-conflict news digests,
and asks a small local model (Ollama) to score each situation for level,
trend and confidence with evidence. Results persist into the `conflict_sa`
table in the same database - a single point of storage.

situation_level: 0 quiet .. 5 full-scale war / humanitarian catastrophe
trend:           improving | stable | degrading

Usage:
  python classify.py                     # classify and save
  python classify.py --model <ollama tag> # other local model
  python classify.py --print             # also print a text report
  python classify.py --history           # show ranking history from DB
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .llm import DEFAULT_MODEL, LLM, LLMError

DEFAULT_DB = Path("data/newsgather.db")
MAX_ITEMS_PER_CONFLICT = 24
EVIDENCE_CLIP = 400

# Conflicts tracked. Keys are stable identifiers; keywords drive the
# per-conflict digest built from the articles table.
TRACKED_CONFLICTS: tuple[dict[str, str], ...] = (
    {"key": "ukraine_russia", "label": "Ukraine / Russia", "countries": "RU, UA"},
    {"key": "gaza_israel", "label": "Israel / Palestine (Gaza)", "countries": "IL, PS"},
    {"key": "iran_israel", "label": "Iran / Israel / US", "countries": "IR, IL, US"},
    {"key": "lebanon_hezbollah", "label": "Lebanon / Hezbollah", "countries": "LB, IL"},
    {"key": "yemen_red_sea", "label": "Yemen / Red Sea (Houthi)", "countries": "YE"},
    {"key": "taiwan_china", "label": "Taiwan / China strait", "countries": "CN, TW"},
    {"key": "south_china_sea", "label": "South China Sea disputes", "countries": "CN, PH, VN, MY"},
    {"key": "korea", "label": "Korean peninsula", "countries": "KP, KR"},
    {"key": "india_pakistan", "label": "India / Pakistan", "countries": "IN, PK"},
    {"key": "sudan", "label": "Sudan civil war", "countries": "SD, SS"},
    {"key": "sahel", "label": "Sahel extremism", "countries": "ML, BF, NE, NG, TD"},
    {"key": "congo", "label": "DR Congo east", "countries": "CD, RW"},
    {"key": "myanmar", "label": "Myanmar civil war", "countries": "MM"},
    {"key": "haiti", "label": "Haiti gang violence", "countries": "HT"},
    {"key": "mexico_cartels", "label": "Mexico cartel violence", "countries": "MX"},
    {"key": "venezuela", "label": "Venezuela / US confrontation", "countries": "VE, US"},
    {"key": "syria", "label": "Syria transition", "countries": "SY"},
    {"key": "iraq", "label": "Iraq instability", "countries": "IQ"},
    {"key": "armenia_azerbaijan", "label": "Armenia / Azerbaijan", "countries": "AM, AZ"},
    {"key": "somalia_alshabaab", "label": "Somalia / Al-Shabaab", "countries": "SO"},
    # Europe / North Africa / gaps found in UCDP fatalities (added 2026-10-26).
    # "focus" overrides the reranker's armed-conflict query (intel.pipeline.rerank_query)
    # for situations that are unrest or hybrid pressure rather than open war.
    {"key": "france_unrest", "label": "France unrest", "countries": "FR",
     "focus": "Mass protests, riots, police clashes, strikes, state of emergency, political crisis in France"},
    {"key": "balkans", "label": "Western Balkans (Serbia / Kosovo / Albania / Bosnia)",
     "countries": "RS, XK, AL, BA, ME, MK",
     "focus": "Political crisis, mass protests, ethnic tensions, Serbia-Kosovo incidents, Bosnia secession, "
              "Albania, troop deployments in the Western Balkans"},
    {"key": "nato_east", "label": "Russia / NATO eastern flank (Poland, Baltics, Moldova)",
     "countries": "PL, EE, LV, LT, FI, MD, BY, RU",
     "focus": "Russian hybrid attacks, drone and airspace incursions, sabotage, military threats against "
              "Poland, the Baltic states, Finland or Moldova"},
    {"key": "libya", "label": "Libya", "countries": "LY",
     "focus": "Armed clashes, militias, Haftar, rival governments, political crisis or oil blockade in Libya"},
    {"key": "maghreb", "label": "Algeria / Maghreb (Morocco, W. Sahara, Tunisia)", "countries": "DZ, MA, EH, TN",
     "focus": "Algerian military deployments, Algeria-Morocco tensions, Western Sahara and Polisario, "
              "Tunisia unrest, security operations in North Africa"},
    {"key": "ethiopia", "label": "Ethiopia / Eritrea (Amhara, Tigray)", "countries": "ET, ER"},
    {"key": "afghanistan_pakistan", "label": "Afghanistan / Pakistan (TTP, Baloch)", "countries": "AF, PK"},
)

CONFLICT_KEYWORDS: dict[str, str] = {
    "ukraine_russia": "ukraine,russia,russian,kharkiv,donetsk,zaporizhzhia,crimea,kiev,kyiv",
    "gaza_israel": "gaza,palestinian,israel,west bank,hamas,rafah,jerusalem",
    "iran_israel": "iran,iranian,tehran,irgc,basij",
    "lebanon_hezbollah": "lebanon,hezbollah,beirut,south lebanon",
    "yemen_red_sea": "yemen,houthi,red sea,bab el-mandeb,bab al-mandeb,aden,sanaa,ukmto,gulf of aden",
    "taiwan_china": "taiwan,taipei,strait,pla,people's liberation",
    "south_china_sea": "south china sea,scarborough,spratly,paracel,philippine,vietnam,manila",
    "korea": "north korea,kim jong,pyongyang,missile test,dprk",
    "india_pakistan": "india,pakistan,kashmir,islamabad,delhi",
    "sudan": "sudan,darfur,khartoum,rsf,rapid support",
    "sahel": "sahel,mali,burkina,niger,nigeria,boko haram,jnim,chad",
    "congo": "congo,drc,m23,goma,rwanda,kinshasa",
    "myanmar": "myanmar,burma,rakhine,rohingya,junta",
    "haiti": "haiti,port-au-prince,gang",
    "mexico_cartels": "mexico,cartel,sinaloa,jalisco,guerrero,michoacan",
    "venezuela": "venezuela,caracas,maduro,essequibo",
    "syria": "syria,damascus,aleppo,assad,hts,idlib",
    "iraq": "iraq,baghdad,erbil,kurdish",
    "armenia_azerbaijan": "armenia,azerbaijan,nagorno-karabakh,yerevan,baku",
    "somalia_alshabaab": "somalia,mogadishu,al-shabaab,al shabaab",
    "france_unrest": "france,french,paris,macron,le pen,marseille,lyon,melenchon,mélenchon,gendarmerie",
    "balkans": "serbia,belgrade,vucic,vučić,kosovo,pristina,albania,tirana,bosnia,republika srpska,"
               "dodik,sarajevo,montenegro,north macedonia",
    "nato_east": "baltic,estonia,latvia,lithuania,poland,polish,moldova,transnistria,kaliningrad,"
                 "suwalki,belarus,lukashenko,finland,airspace violation",
    "libya": "libya,tripoli,benghazi,haftar,misrata,dbeibah",
    "maghreb": "algeria,algiers,tebboune,morocco,moroccan,rabat,western sahara,polisario,sahrawi,tunisia,tunis",
    "ethiopia": "ethiopia,eritrea,tigray,amhara,fano,oromia,addis ababa,abiy",
    "afghanistan_pakistan": "afghanistan,afghan,taliban,kabul,tehrik,tehreek,balochistan,baloch,khyber,waziristan",
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "situation_level": {"type": "integer", "minimum": 0, "maximum": 5},
                    "trend": {"type": "string", "enum": ["improving", "stable", "degrading"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "summary": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "signals": {
                        "type": "object",
                        "properties": {
                            "military": {"type": "boolean"},
                            "diplomatic": {"type": "boolean"},
                            "humanitarian": {"type": "boolean"},
                            "economic": {"type": "boolean"},
                            "information": {"type": "boolean"},
                        },
                        "required": [],
                    },
                },
                "required": ["key", "situation_level", "trend", "confidence", "summary", "evidence"],
            },
        }
    },
    "required": ["conflicts"],
}

SYSTEM_PROMPT = """\
You are a geopolitical analyst producing a situational-awareness brief from raw \
news items. You will receive a list of conflicts and a digest of recent news \
items per conflict. For each conflict you must assess:

- situation_level: overall intensity 0-5
  0 quiet/no activity, 1 latent tension, 2 rising tension/protests/incidents,
  3 active hostilities/limited clashes, 4 sustained combat/major escalation risk,
  5 full-scale war/humanitarian catastrophe.
- trend: whether the situation is improving, stable, or degrading versus the \
recent past implied by the news volume and tone.
- confidence: how well the digest supports your assessment (0-1). Low if few \
or thin items.
- summary: one or two sentences, factual, citing concrete events.
- evidence: short quotes or paraphrases prefixed with their source tag, only \
from the digest. Never invent events.
- signals: which domains show activity (military, diplomatic, humanitarian, \
economic, information).

Be conservative: base every judgement only on the provided items. If a conflict \
has no items, use situation_level 0 or 1 with low confidence and say why. \
Reply with JSON only, matching the requested schema."""


# ---------------------------------------------------------------------------
# Digest building from newsgather.db
# ---------------------------------------------------------------------------


def _clip(value: Any, limit: int = EVIDENCE_CLIP) -> str:
    return str(value or "").strip()[:limit]


def build_digest(conn: sqlite3.Connection, hours: int = 48) -> dict[str, list[str]]:
    """Article lines per conflict, newest first (see match_articles)."""
    return {
        key: [f"{a['source']}: {a['title']}. {_clip(a['summary'], 200)}" for a in items]
        for key, items in match_articles(conn, hours, MAX_ITEMS_PER_CONFLICT).items()
    }


def match_articles(
    conn: sqlite3.Connection, hours: int = 48, max_items: int = MAX_ITEMS_PER_CONFLICT,
    max_age_days: int = 14,
) -> dict[str, list[dict[str, Any]]]:
    """Candidate articles per conflict, newest first.

    Matching is two-layer:
    1. Semantic (preferred): articles whose extracted countries/actors
       intersect the conflict's country list (from the entities table,
       populated by extract_entities.py).
    2. Keyword fallback: substring match on CONFLICT_KEYWORDS for articles
       not yet entity-extracted.
    Substring matching is loose ("pla" hits "playing"); intel.pipeline
    reranks these candidates before anything reaches the model.
    """
    matched_items: dict[str, list[dict[str, Any]]] = {c["key"]: [] for c in TRACKED_CONFLICTS}
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT a.id, a.source, a.title, a.summary, a.link, a.published_at,
                  a.first_seen_at, e.actors_json, e.countries_json
           FROM articles a
           LEFT JOIN entities e ON e.article_id = a.id
           WHERE a.first_seen_at >= datetime('now', ?)
             -- first_seen alone lets search results resurface years-old pages as
             -- "new"; drop those whose ISO publish date is older than max_age_days
             -- (not `hours`: a late gather legitimately brings in last week's backlog)
             AND (a.published_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                  OR substr(a.published_at, 1, 10) >= date('now', ?))
           ORDER BY a.first_seen_at DESC""",
        (f"-{hours} hours", f"-{max_age_days} days"),
    ).fetchall()
    for row in rows:
        title = _clip(row["title"], 200)
        if not title:
            continue
        article = {
            "id": row["id"], "source": row["source"], "title": title,
            "summary": _clip(row["summary"], 300), "link": row["link"] or "",
            "published_at": row["published_at"] or row["first_seen_at"],
        }
        extracted_countries: set[str] = set()
        if row["countries_json"]:
            try:
                extracted_countries = {
                    c.strip().lower()
                    for c in json.loads(row["countries_json"])
                }
            except (ValueError, TypeError):
                pass
        extracted_actors: set[str] = set()
        if row["actors_json"]:
            try:
                extracted_actors = {
                    a.strip().lower()
                    for a in json.loads(row["actors_json"])
                }
            except (ValueError, TypeError):
                pass
        text = f"{title} {article['summary']}".lower()
        for conflict in TRACKED_CONFLICTS:
            key = conflict["key"]
            if len(matched_items[key]) >= max_items:
                continue
            matched = False
            # 1. semantic: extracted countries/actors vs conflict's countries
            if extracted_countries or extracted_actors:
                conflict_countries = {
                    c.strip().lower()
                    for c in conflict["countries"].split(",")
                }
                actor_seeds = _conflict_actor_seeds(key)
                if extracted_countries & conflict_countries:
                    matched = True
                elif extracted_actors & actor_seeds:
                    matched = True
            # 2. keyword fallback
            if not matched:
                for word in CONFLICT_KEYWORDS.get(key, "").split(","):
                    word = word.strip().lower()
                    if word and word in text:
                        matched = True
                        break
            if matched:
                matched_items[key].append(article)
    return matched_items


# actor names that identify a conflict when extracted from an article
_CONFLICT_ACTOR_SEEDS: dict[str, set[str]] = {
    "ukraine_russia": {"russia", "russian armed forces", "kremlin", "wagner"},
    "gaza_israel": {"hamas", "idf", "israel defense forces"},
    "iran_israel": {"irgc", "iranian revolutionary guard"},
    "lebanon_hezbollah": {"hezbollah"},
    "yemen_red_sea": {"houthis", "houthi"},
    "korea": {"dprk", "korean people's army"},
    "sudan": {"rsf", "rapid support forces", "saf"},
    "sahel": {"boko haram", "jnim", "islamic state sahel"},
    "congo": {"m23"},
    "myanmar": {"tatmadaw", "myanmar junta"},
    "somalia_alshabaab": {"al-shabaab", "al shabaab"},
    "venezuela": {"maduro"},
    "mexico_cartels": {"sinaloa cartel", "cjng"},
    "libya": {"libyan national army", "haftar"},
    "maghreb": {"polisario front", "polisario"},
    "ethiopia": {"fano", "tplf", "eritrean defence forces", "oromo liberation army"},
    "afghanistan_pakistan": {"ttp", "tehrik-i-taliban pakistan", "baloch liberation army", "iskp",
                             "islamic state khorasan"},
}


def _conflict_actor_seeds(key: str) -> set[str]:
    return _CONFLICT_ACTOR_SEEDS.get(key, set())


def build_prompt(digest: dict[str, list[str]]) -> str:
    sections: list[str] = []
    for conflict in TRACKED_CONFLICTS:
        key = conflict["key"]
        items = digest.get(key) or []
        head = f"### {conflict['label']} (key={key})"
        if not items:
            sections.append(head + "\n(no recent news items in database)")
            continue
        body = "\n".join(f"- {line}" for line in items)
        sections.append(f"{head}\n{body}")
    return (
        "Assess every conflict below using only the provided news digest.\n\n"
        + "\n\n".join(sections)
        + '\n\nReturn JSON: {"conflicts": [{"key", "situation_level", "trend", '
        '"confidence", "summary", "evidence", "signals"}]} with one entry per '
        "conflict key above."
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(llm: LLM, digest: dict[str, list[str]], num_ctx: int = 16384) -> list[dict[str, Any]]:
    """Score every tracked conflict. Falls back to a neutral record when the
    model misses a key."""
    prompt = build_prompt(digest)
    parsed = llm.complete_json(SYSTEM_PROMPT, prompt, SCHEMA, num_ctx=num_ctx)
    by_key = {c.get("key"): c for c in parsed.get("conflicts", []) if isinstance(c, dict)}
    now = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for conflict in TRACKED_CONFLICTS:
        key = conflict["key"]
        model_record = by_key.get(key)
        item_count = len(digest.get(key) or [])
        if model_record is None:
            records.append(
                {
                    "key": key,
                    "conflict": conflict["label"],
                    "countries": conflict["countries"],
                    "situation_level": 1,
                    "trend": "stable",
                    "confidence": 0.0,
                    "summary": "Model returned no assessment.",
                    "evidence": [],
                    "signals": {},
                    "item_count": item_count,
                    "assessed_at": now,
                }
            )
            continue
        records.append(
            {
                "key": key,
                "conflict": conflict["label"],
                "countries": conflict["countries"],
                "situation_level": int(model_record.get("situation_level", 1)),
                "trend": str(model_record.get("trend", "stable")),
                "confidence": float(model_record.get("confidence", 0.0)),
                "summary": str(model_record.get("summary", "")).strip(),
                "evidence": [str(e) for e in (model_record.get("evidence") or [])][:8],
                "signals": dict(model_record.get("signals") or {}),
                "item_count": item_count,
                "assessed_at": now,
            }
        )
    records.sort(key=lambda r: (r["situation_level"], r["confidence"]), reverse=True)
    return records


def save_classification(db_path: Path, records: list[dict[str, Any]], model: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for record in records:
            conn.execute(
                """INSERT OR REPLACE INTO conflict_sa
                   (assessed_at, key, conflict, situation_level, trend,
                    confidence, summary, evidence_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    record["assessed_at"], record["key"], record["conflict"],
                    record["situation_level"], record["trend"], record["confidence"],
                    record["summary"], json.dumps(
                        {"evidence": record["evidence"], "signals": record["signals"],
                         "item_count": record["item_count"], "model": model},
                        ensure_ascii=False,
                    ),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def load_latest(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute("SELECT MAX(assessed_at) t FROM conflict_sa").fetchone()["t"]
        if not latest:
            return []
        return conn.execute(
            "SELECT * FROM conflict_sa WHERE assessed_at = ? "
            "ORDER BY situation_level DESC, confidence DESC",
            (latest,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

TREND_MARK = {"improving": "+", "stable": "=", "degrading": "-"}


def render_report(rows: list[Any]) -> str:
    lines = ["Situational awareness ranking (local model)", ""]
    for index, row in enumerate(rows, 1):
        if isinstance(row, sqlite3.Row):
            level = row["situation_level"]
            trend = row["trend"]
            conf = row["confidence"]
            name = row["conflict"]
            summary = row["summary"]
            extra = json.loads(row["evidence_json"] or "{}")
            signals = [k for k, v in (extra.get("signals") or {}).items() if v]
        else:
            level = row["situation_level"]
            trend = row["trend"]
            conf = row["confidence"]
            name = row["conflict"]
            summary = row["summary"]
            signals = [k for k, v in (row.get("signals") or {}).items() if v]
        bar = "#" * level + "." * (5 - level)
        lines.append(f"{index:>2}. [{bar}] {TREND_MARK.get(trend, '?')} conf={conf:.1f}  {name}")
        if summary:
            lines.extend("    " + l for l in textwrap.wrap(summary, 76))
        if signals:
            lines.append(f"    signals: {', '.join(signals)}")
        lines.append("")
    return "\n".join(lines)


def show_history(db_path: Path, limit: int = 20) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT assessed_at, key, conflict, situation_level, trend, confidence
           FROM conflict_sa ORDER BY assessed_at DESC, situation_level DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    if not rows:
        print("(no classifications yet)")
        return
    current_day = ""
    for row in rows:
        day = row["assessed_at"][:16]
        if day != current_day:
            print(f"\n{day}Z")
            current_day = day
        mark = TREND_MARK.get(row["trend"], "?")
        print(
            f"  [{row['situation_level']}] {mark} conf={row['confidence']:.1f}  "
            f"{row['conflict']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite path")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model tag")
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--hours", type=int, default=48, help="digest window in hours")
    parser.add_argument("--print", action="store_true", help="print report to stdout")
    parser.add_argument("--history", action="store_true", help="show history and exit")
    parser.add_argument("--dry-run", action="store_true", help="show digest counts only")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=__import__("sys").stderr)
        return 1

    if args.history:
        show_history(db_path)
        return 0

    conn = sqlite3.connect(str(db_path))
    digest = build_digest(conn, hours=args.hours)
    conn.close()

    if args.dry_run:
        for key, items in digest.items():
            print(f"{key}: {len(items)} items")
            for line in items[:2]:
                print(f"  {line[:120]}")
        return 0

    llm = LLM(model=args.model, host=args.host)
    try:
        records = classify(llm, digest, num_ctx=args.num_ctx)
    except LLMError as exc:
        print(f"error: {exc}")
        return 1
    save_classification(db_path, records, args.model)
    if args.print:
        print(render_report(records))
    else:
        print(f"classified {len(records)} conflicts -> {db_path} conflict_sa")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
