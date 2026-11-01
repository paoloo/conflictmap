"""Correlation links between map entities (no models, no network, no Qt).

Two families of links, each with a strength and human-readable reasons:

conflict <-> conflict, from the latest ranked run:
  - shared reports:  the same article survived reranking for both (strongest:
    the reranker judged it relevant to each conflict independently);
  - cross-mentions:  a kept article of A names B (word-boundary keywords,
    ambiguous words like "strait" or "gang" ignored);
  - shared actors:   extracted actors (entities table, extract_entities.py)
    that appear in the kept articles of both;
  - shared country:  structural, from TRACKED_CONFLICTS country codes.

entity <-> conflict, for nodes, tracks and manual aggression points:
  - proximity: within `radius_km` of the conflict anchor or of one of its
    recent UCDP fatal incidents;
  - mention:   the entity's title/text names the conflict.

Endpoints are anchor specs shared with the map's link layer:
"conflict:<key>", "node:<uid>", "track:<uid>", "agg:<id>".
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import classify
from .pipeline import CONFLICT_GEO

W_SHARED_REPORT = 3.0
W_CROSS_MENTION = 1.0
W_SHARED_ACTOR = 1.0
W_SHARED_COUNTRY = 1.0
W_MENTION = 2.0
W_NEAR = 2.0

# keywords too generic to say "this article is about that conflict"
AMBIGUOUS = {"strait", "pla", "gang", "cartel", "kurdish", "missile test", "junta", "aden",
             "paris", "french", "tripoli", "polish", "finland", "afghan"}   # venue/nationality, not topic


@dataclass
class Correlation:
    a: str                      # anchor spec
    b: str
    strength: float = 0.0
    reasons: list[str] = field(default_factory=list)
    kind: str = "conflict"      # conflict (conflict<->conflict) | entity (entity<->conflict)

    @property
    def id(self) -> str:
        return f"corr:{self.a}|{self.b}"

    @property
    def short(self) -> str:
        """Label drawn on the map: the first (strongest) reason."""
        return self.reasons[0] if self.reasons else ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "a": self.a, "b": self.b, "kind": self.kind,
                "strength": round(self.strength, 2), "reasons": self.reasons}


def _keyword_patterns() -> dict[str, re.Pattern[str]]:
    out = {}
    for key, words in classify.CONFLICT_KEYWORDS.items():
        terms = [w.strip() for w in words.split(",") if w.strip() and w.strip() not in AMBIGUOUS]
        out[key] = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)
    return out


PATTERNS = _keyword_patterns()
LABELS = {c["key"]: c["label"] for c in classify.TRACKED_CONFLICTS}
COUNTRIES = {c["key"]: {x.strip() for x in c["countries"].split(",")} for c in classify.TRACKED_CONFLICTS}


def mentions(text: str) -> set[str]:
    """Conflict keys named in free text (word boundaries, ambiguous terms skipped)."""
    return {key for key, pat in PATTERNS.items() if text and pat.search(text)}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, h)))


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _article_texts(db: Path, ids: set[int]) -> tuple[dict[int, str], dict[int, set[str]]]:
    """title+summary and extracted actors per article id (read-only)."""
    texts: dict[int, str] = {}
    actors: dict[int, set[str]] = {}
    if not ids or not Path(db).exists():
        return texts, actors
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        marks = ",".join("?" * len(ids))
        for aid, title, summary in conn.execute(
                f"SELECT id, title, summary FROM articles WHERE id IN ({marks})", tuple(ids)):
            texts[aid] = f"{title}. {summary}"
        has_entities = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
        if has_entities:
            for aid, actors_json in conn.execute(
                    f"SELECT article_id, actors_json FROM entities WHERE article_id IN ({marks})", tuple(ids)):
                try:
                    actors[aid] = {a.strip().lower() for a in json.loads(actors_json) if a.strip()}
                except (json.JSONDecodeError, AttributeError):
                    pass
    finally:
        conn.close()
    return texts, actors


def conflict_links(data: dict[str, Any] | None, db: Path, min_strength: float = 2.0,
                   min_level: int = 0) -> list[Correlation]:
    """conflict<->conflict correlations for a ranked run (pipeline.load_latest)."""
    if not data:
        return []
    conflicts = [c for c in data["conflicts"] if c["situation_level"] >= min_level]
    keys = {c["key"] for c in conflicts}
    kept = {c["key"]: {a["id"] for a in c.get("articles") or [] if "id" in a} for c in conflicts}
    texts, actors = _article_texts(db, set().union(*kept.values()) if kept else set())
    links: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    actor_names: dict[tuple[str, str], set[str]] = defaultdict(set)

    ordered = sorted(keys)
    for i, ka in enumerate(ordered):
        for kb in ordered[i + 1:]:
            pair = (ka, kb)
            shared = kept[ka] & kept[kb]
            if shared:
                links[pair]["shared"] = len(shared)
            common = sorted(COUNTRIES[ka] & COUNTRIES[kb])
            if common:
                links[pair]["country:" + "/".join(common)] = len(common)
            acts_a = set().union(*(actors.get(x, set()) for x in kept[ka]))
            acts_b = set().union(*(actors.get(x, set()) for x in kept[kb]))
            if acts_a & acts_b:
                actor_names[pair] = acts_a & acts_b
    for key in ordered:
        for aid in kept[key]:
            for other in mentions(texts.get(aid, "")) - {key}:
                if other in keys and aid not in kept[other]:     # shared ones counted already
                    links[_pair(key, other)]["mention"] += 1

    out: list[Correlation] = []
    for pair in set(links) | set(actor_names):
        sig = links.get(pair, {})
        reasons: list[tuple[float, str]] = []
        if sig.get("shared"):
            n = int(sig["shared"])
            reasons.append((n * W_SHARED_REPORT, f"{n} shared report{'s' * (n > 1)}"))
        if sig.get("mention"):
            n = int(sig["mention"])
            reasons.append((n * W_CROSS_MENTION, f"{n} cross-mention{'s' * (n > 1)}"))
        if actor_names.get(pair):
            names = sorted(actor_names[pair])
            reasons.append((len(names) * W_SHARED_ACTOR, "actors: " + ", ".join(names[:4])))
        for name, n in sig.items():
            if name.startswith("country:"):
                reasons.append((n * W_SHARED_COUNTRY, f"shared country {name[8:]}"))
        strength = sum(w for w, _ in reasons)
        if strength >= min_strength:
            reasons.sort(key=lambda r: -r[0])
            out.append(Correlation(f"conflict:{pair[0]}", f"conflict:{pair[1]}", strength,
                                   [r for _, r in reasons], "conflict"))
    out.sort(key=lambda c: -c.strength)
    return out


def entity_links(entities: list[dict[str, Any]], conflicts: list[dict[str, Any]],
                 events: list[dict[str, Any]], radius_km: float = 150.0,
                 min_strength: float = 2.0) -> list[Correlation]:
    """entity<->conflict correlations.

    entities: [{"spec", "lat", "lon", "text"}]; conflicts: ranked run dicts
    (key, lat, lon); events: UCDP dicts (lat, lon, country) as from
    pipeline.recent_events — attributed to conflicts by CONFLICT_GEO prefixes.
    """
    anchors: dict[str, list[tuple[float, float]]] = {}      # [conflict anchor, *its incidents]
    for c in conflicts:
        if c.get("lat") is None:
            continue
        pts = [(c["lat"], c["lon"])]
        prefixes = CONFLICT_GEO.get(c["key"], {}).get("ucdp", ())
        pts += [(e["lat"], e["lon"]) for e in events
                if prefixes and str(e.get("country") or "").startswith(prefixes)]
        anchors[c["key"]] = pts
    out: list[Correlation] = []
    for ent in entities:
        named = mentions(ent.get("text") or "")
        for key, pts in anchors.items():
            reasons: list[tuple[float, str]] = []
            if key in named:
                reasons.append((W_MENTION, f"mentions {LABELS.get(key, key)}"))
            if radius_km > 0 and ent.get("lat") is not None:
                # cheap bounding-box reject before haversine (1 deg lat ~ 111 km)
                box = radius_km / 111.0
                d, i = min(((haversine_km(ent["lat"], ent["lon"], *p), i) for i, p in enumerate(pts)
                            if abs(p[0] - ent["lat"]) <= box), default=(math.inf, -1))
                if d <= radius_km:
                    # closer = stronger: W_NEAR at the edge, up to 2*W_NEAR on top of it
                    what = "conflict centre" if i == 0 else "UCDP incident"
                    reasons.append((W_NEAR * (2 - d / radius_km), f"{d:.0f} km from {what}"))
            strength = sum(w for w, _ in reasons)
            if reasons and strength >= min_strength:
                reasons.sort(key=lambda r: -r[0])
                out.append(Correlation(ent["spec"], f"conflict:{key}", round(strength, 1),
                                       [r for _, r in reasons], "entity"))
    out.sort(key=lambda c: -c.strength)
    return out
