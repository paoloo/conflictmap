#!/usr/bin/env python3
"""Newsgather - standalone news/event gatherer backed by SQLite.

A single-point data project derived from the epsilon BBS feeds, expanded with
more sources, persisted into one SQLite database (WAL mode) instead of JSON
snapshots. Fully offline-capable: everything lives in the DB for later
analysis by the SA ranker or the agent.

Schema highlights:
  sources        - registered feed/event sources + per-run health
  articles       - normalized news articles/headlines (deduped by url_hash)
  events         - structured conflict events (UCDP GED, earthquakes, FIRMS)
  gathers        - one row per gather run (source, counts, errors)
  articles_fts   - FTS5 full-text index over title+summary

Usage:
  python gather.py                     # fetch everything new
  python gather.py --source war_news   # only one source
  python gather.py --stats             # DB statistics
  python gather.py --search "red sea"  # full-text search
  python gather.py --latest 20         # latest articles
  python gather.py --regions           # per-region counts
  python gather.py --purge-days 30     # prune articles older than N days
  python gather.py --db path.db        # custom DB path (default data/newsgather.db)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests

# ---------------------------------------------------------------------------
# Config: sources (BBS originals + expanded set)
# ---------------------------------------------------------------------------

DB_PATH = Path("data/newsgather.db")
FETCH_TIMEOUT = 20
USER_AGENT = "newsgather/1.0 (+local osint research)"
MAX_PER_FEED = 40

WAR_RSS_FEEDS: list[tuple[str, str]] = [
    # BBS originals
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("WSJ World", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"),
    ("Military Times", "https://www.militarytimes.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Army Times", "https://www.armytimes.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Long War Journal", "https://www.longwarjournal.org/feed"),
    # expanded
    ("Reuters World", "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en"),
    ("Guardian World", "https://www.theguardian.com/world/rss"),
    ("France24", "https://www.france24.com/en/rss"),
    ("Deutsche Welle", "https://rss.dw.com/rdf/rss-en-world"),
    ("Times of Israel", "https://www.timesofisrael.com/feed/"),
    ("Ukrainska Pravda EN", "https://www.pravda.com.ua/eng/rss/"),
    ("Meduza EN", "https://meduza.io/rss/en/all"),
    ("Middle East Eye", "https://www.middleeasteye.net/rss"),
]

MARITIME_RSS_FEEDS: list[tuple[str, str]] = [
    ("gCaptain", "https://gcaptain.com/feed/"),
    ("Splash247", "https://splash247.com/feed/"),
]

HUMANITARIAN_RSS_FEEDS: list[tuple[str, str]] = [
    ("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml"),
]

REGIONAL_RSS_FEEDS: list[tuple[str, str]] = [
    ("Crisis Group", "https://www.crisisgroup.org/rss"),
    ("Foreign Affairs", "https://www.foreignaffairs.com/rss.xml"),
    ("Modern War Institute", "https://mwi.westpoint.edu/feed/"),
    ("CEPA", "https://cepa.org/feed/"),
    ("Atlantic Council", "https://www.atlanticcouncil.org/feed/"),
    # Europe, Balkans, North Africa, South Asia (added 2026-10-26; all checked live)
    ("Euronews", "https://www.euronews.com/rss?level=theme&name=news"),
    ("Politico Europe", "https://www.politico.eu/feed/"),
    ("Le Monde EN", "https://www.lemonde.fr/en/rss/une.xml"),
    ("RFI EN", "https://www.rfi.fr/en/rss"),
    ("Balkan Insight", "https://balkaninsight.com/feed/"),
    ("Prishtina Insight", "https://prishtinainsight.com/feed/"),
    ("Sarajevo Times", "https://sarajevotimes.com/feed/"),
    ("Notes from Poland", "https://notesfrompoland.com/feed/"),
    ("ERR News", "https://news.err.ee/rss"),
    ("Libya Herald", "https://libyaherald.com/feed/"),
    ("Africanews", "https://www.africanews.com/feed/rss"),
    ("Dawn", "https://www.dawn.com/feeds/home"),
]

GOOGLE_NEWS_QUERIES: list[str] = [
    # BBS originals
    "war military conflict strike attack",
    "drone missile strike war",
    "front line offensive military",
    # expanded
    "ceasefire negotiations peace talks",
    "sanctions embassy expulsion diplomats",
    "military exercise naval deployment carrier",
    "coup attempt coup d'etat junta",
    "mass protests riots police clashes state of emergency",
    "Serbia Kosovo Albania Bosnia tensions",
    "Libya Algeria Tunisia Morocco security military",
    "Russia drones sabotage Poland Baltic NATO airspace",
]

GDELT_QUERIES: list[str] = [
    "war OR conflict OR strike OR attack OR ceasefire",
    "protest OR unrest OR riot OR demonstration OR coup",
    "sanction OR diplomacy OR negotiations OR summit",
]

TELEGRAM_CHANNELS: list[str] = [
    "WarMonitor",
    "OSINTdefender",
]

MARITIME_QUERIES: list[str] = [
    "strait of hormuz shipping disruption",
    "red sea shipping attack port closure",
    "black sea naval movement war shipping",
]

HUMANITARIAN_QUERIES: list[str] = [
    "humanitarian crisis OR displacement OR refugees OR aid",
    "food insecurity OR shelter OR water crisis OR health crisis",
]

UCDP_DOWNLOADS_PAGE = "https://ucdp.uu.se/downloads/"
UCDP_CANDIDATE_CSV_URL = (
    "https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_01_26_06.csv"
)
UCDP_RECENT_DAYS = 60

USGS_QUAKE_URL = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
)

# RSS category -> canonical region tag used when the feed is region-specific
FEED_REGION: dict[str, str] = {}

# --- canonical regions (same taxonomy as the BBS) ---------------------------

NORTH_AMERICA = "North America"
LATAM = "Latin America & Caribbean"
EUROPE = "Europe"
EURASIA = "Eurasia & Central Asia"
MENA = "Middle East & North Africa"
SSA = "Sub-Saharan Africa"
SOUTH_ASIA = "South Asia"
EAST_ASIA_PAC = "East & SE Asia & Pacific"
MARITIME = "Maritime"
UNKNOWN = "Unknown"

REGIONS = [NORTH_AMERICA, LATAM, EUROPE, EURASIA, MENA, SSA, SOUTH_ASIA, EAST_ASIA_PAC]

_ISO2_BY_REGION: dict[str, tuple[str, ...]] = {
    NORTH_AMERICA: ("US", "CA", "GL", "BM", "PM"),
    LATAM: (
        "MX", "GT", "SV", "HN", "NI", "CR", "PA", "BZ",
        "BR", "CO", "VE", "EC", "PE", "BO", "CL", "AR", "UY", "PY", "GY", "SR", "GF",
        "CU", "DO", "HT", "JM", "BS", "TT", "PR",
    ),
    EUROPE: (
        "GB", "IE", "FR", "DE", "NL", "BE", "LU", "CH", "AT", "IT", "ES", "PT",
        "DK", "NO", "SE", "FI", "IS", "EE", "LV", "LT", "PL", "CZ", "SK", "HU",
        "RO", "BG", "GR", "SI", "HR", "RS", "BA", "MK", "AL", "ME", "XK", "MD", "UA", "BY",
    ),
    EURASIA: (
        "RU", "GE", "AM", "AZ", "KZ", "UZ", "TM", "KG", "TJ", "MN",
    ),
    MENA: (
        "MA", "DZ", "TN", "LY", "EG", "SD", "TR", "SY", "LB", "IL", "PS", "JO",
        "IQ", "IR", "SA", "YE", "OM", "AE", "QA", "BH", "KW", "EH",
    ),
    SSA: (
        "MR", "ML", "NE", "TD", "BF", "SN", "GM", "GW", "GN", "SL", "LR", "CI",
        "GH", "TG", "BJ", "NG", "CM", "CF", "GQ", "GA", "CG", "CD", "AO", "ZM",
        "MW", "MZ", "ZW", "BW", "NA", "ZA", "LS", "SZ", "MG", "KM", "SC", "MU",
        "ET", "ER", "DJ", "SO", "SS", "KE", "UG", "RW", "BI", "TZ", "CV", "ST",
    ),
    SOUTH_ASIA: ("AF", "PK", "IN", "BD", "NP", "BT", "LK", "MV"),
    EAST_ASIA_PAC: (
        "CN", "TW", "JP", "KP", "KR", "MN", "HK", "MO",
        "TH", "LA", "VN", "KH", "MM", "MY", "SG", "ID", "PH", "BN", "TL",
        "PG", "SB", "VU", "FJ", "NZ", "AU",
    ),
}

_ISO2_TO_REGION: dict[str, str] = {
    code: region for region, codes in _ISO2_BY_REGION.items() for code in codes
}

# English country name -> ISO2 (partial, conflict-relevant subset)
_COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "ukraine": "UA", "russia": "RU", "israel": "IL", "palestine": "PS",
    "gaza": "PS", "west bank": "PS", "iran": "IR", "iraq": "IQ",
    "syria": "SY", "lebanon": "LB", "yemen": "YE", "saudi arabia": "SA",
    "turkey": "TR", "turkiye": "TR", "egypt": "EG", "libya": "LY",
    "sudan": "SD", "south sudan": "SS", "ethiopia": "ET", "eritrea": "ER",
    "somalia": "SO", "kenya": "KE", "mali": "ML", "niger": "NE",
    "nigeria": "NG", "burkina faso": "BF", "chad": "TD", "cameroon": "CM",
    "democratic republic of congo": "CD", "congo": "CD", "drc": "CD",
    "central african republic": "CF", "rwanda": "RW", "burundi": "BI",
    "uganda": "UG", "tanzania": "TZ", "mozambique": "MZ", "zimbabwe": "ZW",
    "south africa": "ZA", "china": "CN", "taiwan": "TW", "japan": "JP",
    "north korea": "KP", "south korea": "KR", "korea": "KR",
    "india": "IN", "pakistan": "PK", "afghanistan": "AF", "bangladesh": "BD",
    "nepal": "NP", "sri lanka": "LK", "myanmar": "MM", "burma": "MM",
    "thailand": "TH", "vietnam": "VN", "cambodia": "KH", "laos": "LA",
    "malaysia": "MY", "singapore": "SG", "indonesia": "ID",
    "philippines": "PH", "australia": "AU", "new zealand": "NZ",
    "united states": "US", "usa": "US", "canada": "CA", "mexico": "MX",
    "guatemala": "GT", "honduras": "HN", "el salvador": "SV",
    "nicaragua": "NI", "panama": "PA", "cuba": "CU", "haiti": "HT",
    "dominican republic": "DO", "jamaica": "JM",
    "venezuela": "VE", "colombia": "CO", "ecuador": "EC", "peru": "PE",
    "bolivia": "BO", "brazil": "BR", "chile": "CL", "argentina": "AR",
    "uruguay": "UY", "paraguay": "PY", "guyana": "GY", "suriname": "SR",
    "armenia": "AM", "azerbaijan": "AZ", "georgia": "GE",
    "kazakhstan": "KZ", "uzbekistan": "UZ", "turkmenistan": "TM",
    "kyrgyzstan": "KG", "tajikistan": "TJ", "belarus": "BY",
    "moldova": "MD", "poland": "PL", "romania": "RO", "bulgaria": "BG",
    "hungary": "HU", "serbia": "RS", "bosnia": "BA", "croatia": "HR",
    "greece": "GR", "germany": "DE", "france": "FR", "spain": "ES",
    "italy": "IT", "united kingdom": "GB", "uk": "GB", "britain": "GB",
}

# Coarse source labels -> canonical
COARSE_ALIASES: dict[str, str] = {
    "middle east": MENA,
    "middle east and north africa": MENA,
    "mena": MENA,
    "europe": EUROPE,
    "europe and eurasia": EUROPE,
    "asia": EAST_ASIA_PAC,
    "asia pacific": EAST_ASIA_PAC,
    "americas": LATAM,
    "north america": NORTH_AMERICA,
    "africa": SSA,
    "sub-saharan africa": SSA,
    "central asia": EURASIA,
    "south asia": SOUTH_ASIA,
    "maritime": MARITIME,
    "unknown": UNKNOWN,
}


def region_for_iso2(code: str) -> str:
    return _ISO2_TO_REGION.get((code or "").strip().upper(), UNKNOWN)


def region_for_name(name: str) -> str:
    key = (name or "").strip().lower()
    if not key:
        return UNKNOWN
    iso2 = _COUNTRY_NAME_TO_ISO2.get(key)
    if iso2:
        return _ISO2_TO_REGION.get(iso2, UNKNOWN)
    for variant, iso2 in _COUNTRY_NAME_TO_ISO2.items():
        if variant in key or key in variant:
            return _ISO2_TO_REGION.get(iso2, UNKNOWN)
    return COARSE_ALIASES.get(key, UNKNOWN)


def region_for_latlon(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return MARITIME
    la, lo = float(lat), float(lon)
    if 7 <= la <= 30 and 34 <= lo <= 60:
        return MENA
    if 36 <= la <= 72 and -12 <= lo <= 40:
        return EUROPE
    if 40 <= la <= 78 and 40 <= lo <= 180:
        return EURASIA
    if 5 <= la <= 40 and 60 <= lo <= 95:
        return SOUTH_ASIA
    if -50 <= la <= 55 and 95 <= lo <= 180:
        return EAST_ASIA_PAC
    if -50 <= la <= 10 and -180 <= lo <= -150:
        return EAST_ASIA_PAC
    if 15 <= la <= 84 and -170 <= lo <= -52:
        if la >= 30 or lo <= -130:
            return NORTH_AMERICA
        return LATAM
    if -57 <= la <= 15 and -82 <= lo <= -34:
        return LATAM
    if 7 <= la <= 30 and -120 <= lo <= -60:
        return LATAM
    return MARITIME


def canonical_region(
    raw: str | None = "",
    *,
    iso2: str = "",
    name: str = "",
    lat: float | None = None,
    lon: float | None = None,
) -> str:
    region = region_for_iso2(iso2)
    if region == UNKNOWN:
        region = region_for_name(name)
    if region == UNKNOWN and lat is not None and lon is not None:
        region = region_for_latlon(lat, lon)
    if region == UNKNOWN and raw:
        region = COARSE_ALIASES.get(raw.strip().lower(), UNKNOWN)
    return region


# --- data records -----------------------------------------------------------

@dataclass
class Article:
    source: str
    title: str
    link: str = ""
    published_at: str = ""     # ISO-8601 UTC when parseable, else raw
    summary: str = ""
    author: str = ""
    country: str = ""
    region: str = UNKNOWN
    category: str = "news"
    query: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Event:
    source: str
    kind: str                  # 'ucdp' | 'earthquake' | 'firms' | ...
    title: str
    occurred_at: str = ""
    country: str = ""
    region: str = UNKNOWN
    lat: float | None = None
    lon: float | None = None
    fatalities: int = 0
    severity: float = 0.0      # magnitude for quakes, FRP for fires, etc.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    source: str
    articles: list[Article] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    error: str = ""
    duration_s: float = 0.0



# ---------------------------------------------------------------------------
# HTTP + parsing helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})


def _get(url: str, timeout: int = FETCH_TIMEOUT, **kwargs) -> requests.Response:
    resp = SESSION.get(url, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def _iso_from_email_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw.strip())
        if dt is None:
            return raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return raw


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


def _iso_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if _ISO_RE.match(raw):
        return raw.replace(" ", "T", 1) if " " in raw and "T" not in raw[:11] else raw
    return _iso_from_email_date(raw)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    return _WS_RE.sub(" ", text).strip()


def parse_rss(text: str, source: str, limit: int = MAX_PER_FEED) -> list[Article]:
    """Parse RSS/RDF/Atom into normalized Articles."""
    root = ET.fromstring(text)
    for el in root.iter():          # drop namespaces: RDF (DW) and Atom items are namespaced
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    elements = root.findall(".//item") or root.findall(".//entry")
    articles: list[Article] = []
    for item in elements[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link:
            link_el = item.find("link")
            if link_el is not None:
                link = (link_el.get("href") or "").strip()
        pub_raw = (
            item.findtext("pubDate")
            or item.findtext("published")
            or item.findtext("updated")
            or item.findtext("date")           # dc:date (RDF)
            or ""
        ).strip()
        summary = strip_html(
            item.findtext("description") or item.findtext("summary") or ""
        )[:2000]
        if title:
            articles.append(
                Article(
                    source=source,
                    title=title,
                    link=link,
                    published_at=_iso_date(pub_raw) if pub_raw else "",
                    summary=summary,
                )
            )
    return articles


def dedupe(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    out: list[Article] = []
    for article in articles:
        key = re.sub(r"\W+", " ", article.title.lower()).strip()[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(article)
    return out


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_rss_list(feeds: list[tuple[str, str]], category: str) -> Result:
    """Fetch a batch of RSS feeds, tolerating individual failures.

    error is set only when every feed in the batch failed."""
    result = Result(source=category)
    errors: list[str] = []
    articles: list[Article] = []
    for name, url in feeds:
        try:
            text = _get(url, headers={"Accept": "application/rss+xml, text/xml, */*"}).text
            got = parse_rss(text, name)
            articles.extend(got)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    result.articles = dedupe(articles)
    result.error = "; ".join(errors)[:400] if errors and not result.articles else ""
    return result


def fetch_google_news() -> Result:
    result = Result(source="google_news")
    articles: list[Article] = []
    errors: list[str] = []
    for query in GOOGLE_NEWS_QUERIES:
        url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            text = _get(url, headers={"Accept": "application/rss+xml, text/xml, */*"}).text
            root = ET.fromstring(text)
            for item in root.findall(".//item")[:20]:
                title = (item.findtext("title") or "").strip()
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0]
                link = (item.findtext("link") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    articles.append(
                        Article(
                            source="Google News",
                            title=title,
                            link=link,
                            published_at=_iso_date(pub),
                            query=query,
                        )
                    )
        except Exception as exc:
            errors.append(f"{query}: {exc}")
    result.articles = dedupe(articles)
    result.error = "; ".join(errors)[:400] if errors and not result.articles else ""
    return result


GDELT_BACKOFF_SECONDS = 60


def _get_gdelt(url: str) -> dict:
    """GDELT rate-limits aggressively (one request per ~5 s, and sustained
    polling can trigger a block that outlasts several minutes). One patient
    retry; after that the caller records the error and moves on - the next
    gather run will backfill."""
    for attempt in range(2):
        try:
            resp = _get(url, headers={"Accept": "application/json"})
            return resp.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429 and attempt == 0:
                time.sleep(GDELT_BACKOFF_SECONDS)
                continue
            raise
    raise RuntimeError("gdelt fetch failed")  # unreachable


def fetch_gdelt() -> Result:
    result = Result(source="gdelt")
    broad = " OR ".join(f"({q})" for q in GDELT_QUERIES)
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={quote_plus(broad)}&mode=artlist&maxrecords=75&format=json&sort=datedesc"
    )
    try:
        raw = _get_gdelt(url)
    except Exception as exc:
        result.error = str(exc)[:400]
        return result
    articles: list[Article] = []
    for art in raw.get("articles", []) or []:
        title = str(art.get("title") or "").strip()
        if not title:
            continue
        country = str(art.get("sourcecountry") or "").strip()
        seen_raw = str(art.get("seendate") or "")
        # GDELT seendate: 20260919T120000Z
        iso = ""
        m = re.match(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", seen_raw)
        if m:
            iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}+00:00"
        articles.append(
            Article(
                source="GDELT",
                title=title,
                link=str(art.get("url") or ""),
                published_at=iso or seen_raw,
                country=country,
                region=region_for_name(country) if country else UNKNOWN,
                summary=str(art.get("domain") or ""),
            )
        )
    result.articles = dedupe(articles)
    return result


def fetch_telegram() -> Result:
    """Public Telegram embed scrape (no API key)."""
    result = Result(source="telegram")
    articles: list[Article] = []
    errors: list[str] = []
    for channel in TELEGRAM_CHANNELS:
        try:
            resp = _get(f"https://t.me/s/{channel}", headers={"Accept": "text/html"})
            # posts: <div class="tgme_widget_message_text ...">text</div>
            blocks = re.findall(
                r'tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', resp.text, re.DOTALL
            )
            for block in blocks[:10]:
                text = strip_html(block)
                if len(text) < 40:
                    continue
                articles.append(
                    Article(source=f"Telegram/{channel}", title=text[:160], summary=text[:2000])
                )
        except Exception as exc:
            errors.append(f"{channel}: {exc}")
    result.articles = dedupe(articles)
    result.error = "; ".join(errors)[:400] if errors and not result.articles else ""
    return result


def fetch_ucdp() -> Result:
    """UCDP Candidate GED conflict events."""
    result = Result(source="ucdp")
    url = UCDP_CANDIDATE_CSV_URL
    try:
        html = _get(UCDP_DOWNLOADS_PAGE, timeout=30).text
        candidates = set(re.findall(r"candidateged/GEDEvent_v[0-9_]+\.csv", html))
        if candidates:
            def _key(name: str) -> tuple[int, int]:
                m = re.search(r"_(\d{2})_(\d{2})\.csv$", name)
                return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            url = "https://ucdp.uu.se/downloads/" + max(candidates, key=_key)
    except Exception:
        pass
    try:
        text = _get(url, timeout=180).text
        if text.startswith("\ufeff"):
            text = text[1:]
    except Exception as exc:
        result.error = str(exc)[:400]
        return result
    cutoff = date.today() - timedelta(days=UCDP_RECENT_DAYS)
    events: list[Event] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        ds = (row.get("date_start") or "").split(" ")[0]
        try:
            event_date = datetime.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            continue
        if event_date < cutoff:
            continue
        country = (row.get("country") or "").strip()
        lat = row.get("latitude") or None
        lon = row.get("longitude") or None
        events.append(
            Event(
                source="UCDP",
                kind="ucdp",
                title=((row.get("source_headline") or row.get("conflict_name") or "").strip() or country)[:160],
                occurred_at=ds,
                country=country,
                region=canonical_region(
                    row.get("region") or "", name=country, lat=lat, lon=lon
                ),
                lat=float(lat) if lat else None,
                lon=float(lon) if lon else None,
                fatalities=_int(row.get("best")),
                detail={
                    "conflict_name": (row.get("conflict_name") or "").strip(),
                    "type_of_violence": (row.get("type_of_violence") or "").strip(),
                    "deaths_civilians": _int(row.get("deaths_civilians")),
                    "adm_1": (row.get("adm_1") or "").strip(),
                    "where": (row.get("where_description") or "").strip()[:200],
                },
            )
        )
    events.sort(key=lambda e: e.fatalities, reverse=True)
    result.events = events[:400]
    return result


def fetch_usgs() -> Result:
    """Significant earthquakes (M4.5+ last 24h)."""
    result = Result(source="usgs")
    try:
        raw = _get(USGS_QUAKE_URL, timeout=30).json()
    except Exception as exc:
        result.error = str(exc)[:400]
        return result
    for feature in raw.get("features", []) or []:
        props = feature.get("properties", {}) or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        place = str(props.get("place") or "").strip()
        result.events.append(
            Event(
                source="USGS",
                kind="earthquake",
                title=f"M{props.get('mag', '?')} earthquake - {place}"[:160],
                occurred_at=datetime.fromtimestamp(
                    props.get("time", 0) / 1000, tz=timezone.utc
                ).isoformat() if props.get("time") else "",
                region=region_for_latlon(lat, lon),
                lat=lat,
                lon=lon,
                severity=float(props.get("mag") or 0),
                detail={"place": place, "url": props.get("url", ""), "tsunami": props.get("tsunami", 0)},
            )
        )
    return result


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_SOURCES: dict[str, Any] = {
    "war_news": lambda: fetch_rss_list(WAR_RSS_FEEDS, "war_news"),
    "maritime_news": lambda: fetch_rss_list(MARITIME_RSS_FEEDS, "maritime_news"),
    "humanitarian": lambda: fetch_rss_list(HUMANITARIAN_RSS_FEEDS, "humanitarian"),
    "regional_analysis": lambda: fetch_rss_list(REGIONAL_RSS_FEEDS, "regional_analysis"),
    "google_news": fetch_google_news,
    "gdelt": fetch_gdelt,  # DISABLED by default: GDELT rate-limits per-IP;
                           # sustained polling triggers multi-hour blocks.
                           # Re-enable per-run with --source gdelt.
    "telegram": fetch_telegram,
    "ucdp": fetch_ucdp,
    "usgs": fetch_usgs,
}

#: Sources skipped unless explicitly requested via --source.
DISABLED_BY_DEFAULT = {"gdelt"}


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,            -- rss_batch | api | scrape
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    last_ok_at  TEXT,
    last_error  TEXT,
    total_articles INTEGER NOT NULL DEFAULT 0,
    total_events   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY,
    url_hash     TEXT NOT NULL UNIQUE,    -- sha1(link or title)
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    link         TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    country      TEXT NOT NULL DEFAULT '',
    region       TEXT NOT NULL DEFAULT 'Unknown',
    category     TEXT NOT NULL DEFAULT 'news',
    query        TEXT NOT NULL DEFAULT '',
    extra        TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
CREATE INDEX IF NOT EXISTS idx_articles_region ON articles(region);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    event_hash  TEXT NOT NULL UNIQUE,     -- sha1(source+kind+title+occurred)
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    country     TEXT NOT NULL DEFAULT '',
    region      TEXT NOT NULL DEFAULT 'Unknown',
    lat         REAL,
    lon         REAL,
    fatalities  INTEGER NOT NULL DEFAULT 0,
    severity    REAL NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_region ON events(region);

CREATE TABLE IF NOT EXISTS gathers (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    errors      TEXT NOT NULL DEFAULT '',
    duration_s  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conflict_sa (
    id            INTEGER PRIMARY KEY,
    assessed_at   TEXT NOT NULL,
    key           TEXT NOT NULL,
    conflict      TEXT NOT NULL,
    situation_level INTEGER NOT NULL,
    trend         TEXT NOT NULL,
    confidence    REAL NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(assessed_at, key)
);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    article_id  INTEGER NOT NULL UNIQUE REFERENCES articles(id),
    actors_json TEXT NOT NULL DEFAULT '[]',
    countries_json TEXT NOT NULL DEFAULT '[]',
    event_type  TEXT NOT NULL DEFAULT 'other',
    military    INTEGER NOT NULL DEFAULT 0,
    model       TEXT NOT NULL DEFAULT '',
    extracted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_article ON entities(article_id);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(event_type);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, summary, link,
    content='articles', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary, link)
    VALUES (new.id, new.title, new.summary, new.link);
END;

CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, link)
    VALUES ('delete', old.id, old.title, old.summary, old.link);
END;

CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, link)
    VALUES ('delete', old.id, old.title, old.summary, old.link);
    INSERT INTO articles_fts(rowid, title, summary, link)
    VALUES (new.id, new.title, new.summary, new.link);
END;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


DB_LOCK = __import__("threading").Lock()


def url_hash(article: Article) -> str:
    basis = article.link or f"{article.source}|{article.title}"
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()


def event_hash(event: Event) -> str:
    basis = f"{event.source}|{event.kind}|{event.title[:160]}|{event.occurred_at[:10]}"
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()


def save_articles(conn: sqlite3.Connection, articles: list[Article]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    inserted = updated = 0
    for article in articles:
        if not article.title:
            continue
        if article.region == UNKNOWN:
            # last-chance classification from title/summary text
            article.region = region_for_name(article.title) or region_for_name(article.summary[:200])
        h = url_hash(article)
        cur = conn.execute(
            "SELECT id FROM articles WHERE url_hash = ?", (h,)
        )
        existing = cur.fetchone()
        if existing:
            conn.execute(
                "UPDATE articles SET last_seen_at = ?, "
                "summary = CASE WHEN ? != '' THEN ? ELSE summary END "
                "WHERE id = ?",
                (now, article.summary, article.summary, existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO articles
                   (url_hash, source, title, link, published_at, summary, author,
                    country, region, category, query, extra, first_seen_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    h, article.source, article.title, article.link,
                    article.published_at, article.summary, article.author,
                    article.country, article.region, article.category,
                    article.query, json.dumps(article.extra, ensure_ascii=False),
                    now, now,
                ),
            )
            inserted += 1
    return inserted, updated


def save_events(conn: sqlite3.Connection, events: list[Event]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    inserted = updated = 0
    for event in events:
        if not event.title:
            continue
        h = event_hash(event)
        cur = conn.execute("SELECT id FROM events WHERE event_hash = ?", (h,))
        if cur.fetchone():
            updated += 1
            continue
        conn.execute(
            """INSERT INTO events
               (event_hash, source, kind, title, occurred_at, country, region,
                lat, lon, fatalities, severity, detail, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                h, event.source, event.kind, event.title, event.occurred_at,
                event.country, event.region, event.lat, event.lon,
                event.fatalities, event.severity,
                json.dumps(event.detail, ensure_ascii=False), now,
            ),
        )
        inserted += 1
    return inserted, updated


def ensure_sources(conn: sqlite3.Connection) -> None:
    kinds = {
        "war_news": "rss_batch", "maritime_news": "rss_batch",
        "humanitarian": "rss_batch", "regional_analysis": "rss_batch",
        "google_news": "api", "gdelt": "api", "telegram": "scrape",
        "ucdp": "api", "usgs": "api",
    }
    for name, kind in kinds.items():
        conn.execute(
            "INSERT OR IGNORE INTO sources (name, kind) VALUES (?, ?)", (name, kind)
        )


def update_source_health(
    conn: sqlite3.Connection,
    name: str,
    ok: bool,
    error: str,
    articles: int,
    events: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE sources SET
             last_run_at = ?,
             last_ok_at = CASE WHEN ? THEN ? ELSE last_ok_at END,
             last_error = ?,
             total_articles = total_articles + ?,
             total_events = total_events + ?
           WHERE name = ?""",
        (now, ok, now, error if not ok else "", articles, events, name),
    )


# ---------------------------------------------------------------------------
# Gather orchestration
# ---------------------------------------------------------------------------


def run_source(conn: sqlite3.Connection, name: str, fetcher: Any) -> dict[str, Any]:
    started = time.time()
    result = fetcher()
    with DB_LOCK:
        inserted_a, updated_a = save_articles(conn, result.articles)
        inserted_e, updated_e = save_events(conn, result.events)
        duration = time.time() - started
        ok = not result.error or bool(result.articles or result.events)
        update_source_health(
            conn, name, ok, result.error, inserted_a, inserted_e
        )
        conn.execute(
            """INSERT INTO gathers (started_at, finished_at, source, inserted, updated, errors, duration_s)
               VALUES (?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
                name, inserted_a + inserted_e, updated_a + updated_e,
                result.error, duration,
            ),
        )
        conn.commit()
    return {
        "source": name,
        "articles": len(result.articles),
        "events": len(result.events),
        "inserted": inserted_a + inserted_e,
        "errors": result.error,
        "duration_s": round(duration, 1),
    }


def gather(db_path: Path, only: list[str], workers: int = 4) -> None:
    conn = connect(db_path)
    ensure_sources(conn)
    if only:
        # Explicit --source always wins (opt-in re-enable of disabled sources).
        unknown = set(only) - set(ALL_SOURCES)
        if unknown:
            print(f"error: unknown source(s): {', '.join(sorted(unknown))}",
                  file=sys.stderr)
            print(f"available: {', '.join(ALL_SOURCES)}", file=sys.stderr)
            conn.close()
            raise SystemExit(2)
        sources = {name: ALL_SOURCES[name] for name in only}
        skipped = set()
    else:
        sources = {
            name: fetcher
            for name, fetcher in ALL_SOURCES.items()
            if name not in DISABLED_BY_DEFAULT
        }
        skipped = DISABLED_BY_DEFAULT
    print(f"gathering {len(sources)} sources -> {db_path}", flush=True)
    if skipped:
        print(f"  (disabled by default, use --source <name> to run: "
              f"{', '.join(sorted(skipped))})", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_source, conn, name, fetcher): name
                   for name, fetcher in sources.items()}
        # SQLite access is serialized by run_source executing inside threads
        # only via GIL; to be safe we collect results and print as they finish.
        for future in as_completed(futures):
            info = future.result()
            if info["errors"]:
                note = (
                    f" FAILED: {info['errors'][:120]}" if not (info["articles"] or info["events"])
                    else f" (partial: {info['errors'][:100]})"
                )
            else:
                note = ""
            print(
                f"  {info['source']:<20} {info['articles']:>4} articles, "
                f"{info['events']:>4} events, +{info['inserted']} new "
                f"({info['duration_s']}s){note}",
                flush=True,
            )
    conn.close()


# ---------------------------------------------------------------------------
# Query CLI
# ---------------------------------------------------------------------------


def show_stats(db_path: Path) -> None:
    conn = connect(db_path)
    articles = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
    events = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    extracted = 0
    try:
        extracted = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
    except sqlite3.OperationalError:
        pass
    last24 = conn.execute(
        "SELECT COUNT(*) c FROM articles WHERE first_seen_at >= datetime('now', '-1 day')"
    ).fetchone()["c"]
    print(f"articles: {articles} (last 24h: {last24}) | entities: {extracted}")
    print(f"events:   {events}")
    print("\nby source:")
    for row in conn.execute(
        "SELECT source, COUNT(*) c, MAX(first_seen_at) latest FROM articles "
        "GROUP BY source ORDER BY c DESC"
    ):
        print(f"  {row['source']:<28} {row['c']:>6}  latest {row['latest'][:16]}")
    print("\nby region:")
    for row in conn.execute(
        "SELECT region, COUNT(*) c FROM articles GROUP BY region ORDER BY c DESC LIMIT 12"
    ):
        print(f"  {row['region']:<28} {row['c']:>6}")
    print("\nevents by kind:")
    for row in conn.execute(
        "SELECT kind, COUNT(*) c, SUM(fatalities) f FROM events GROUP BY kind"
    ):
        print(f"  {row['kind']:<20} {row['c']:>6}  fatalities {row['f'] or 0}")
    conn.close()


def search(db_path: Path, query: str, limit: int = 20) -> None:
    conn = connect(db_path)
    rows = conn.execute(
        """SELECT a.source, a.title, a.link, a.published_at, a.region,
                  snippet(articles_fts, 1, '[', ']', '…', 12) AS snip
           FROM articles_fts f
           JOIN articles a ON a.id = f.rowid
           WHERE articles_fts MATCH ?
           ORDER BY rank LIMIT ?""",
        (query, limit),
    ).fetchall()
    if not rows:
        print("(no matches)")
    for row in rows:
        print(f"[{row['source']}] {row['title']}")
        print(f"  {row['snip']}")
        if row["link"]:
            print(f"  {row['link']}")
        print()
    conn.close()


def latest(db_path: Path, limit: int = 20, source: str = "") -> None:
    conn = connect(db_path)
    if source:
        rows = conn.execute(
            "SELECT source, title, published_at, region FROM articles "
            "WHERE source = ? ORDER BY first_seen_at DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source, title, published_at, region FROM articles "
            "ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    for row in rows:
        pub = (row["published_at"] or "")[:16]
        print(f"[{row['source']}] {row['title']}  ({pub})")
    conn.close()


def show_regions(db_path: Path) -> None:
    conn = connect(db_path)
    print(f"{'region':<30} {'articles':>9} {'events':>8} {'fatalities':>11}")
    rows = conn.execute(
        """SELECT r.region,
                  (SELECT COUNT(*) FROM articles a WHERE a.region = r.region) AS articles,
                  (SELECT COUNT(*) FROM events e WHERE e.region = r.region) AS events,
                  (SELECT COALESCE(SUM(fatalities),0) FROM events e WHERE e.region = r.region) AS deaths
           FROM (SELECT DISTINCT region FROM articles UNION SELECT DISTINCT region FROM events) r
           ORDER BY articles + events DESC"""
    ).fetchall()
    for row in rows:
        print(f"{row['region']:<30} {row['articles']:>9} {row['events']:>8} {row['deaths']:>11}")
    conn.close()


def purge(db_path: Path, days: int) -> None:
    conn = connect(db_path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    a = conn.execute("DELETE FROM articles WHERE first_seen_at < ?", (cutoff.isoformat(),)).rowcount
    e = conn.execute("DELETE FROM events WHERE first_seen_at < ?", (cutoff.isoformat(),)).rowcount
    conn.commit()
    conn.close()
    print(f"purged {a} articles, {e} events older than {days} days")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(prog="gather.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite path (default data/newsgather.db)")
    parser.add_argument("--source", action="append", dest="sources",
                        help="limit to source (repeatable): " + ", ".join(ALL_SOURCES))
    parser.add_argument("--stats", action="store_true", help="show DB statistics")
    parser.add_argument("--search", metavar="QUERY", help="FTS5 full-text search")
    parser.add_argument("--latest", type=int, metavar="N", help="show N latest articles")
    parser.add_argument("--regions", action="store_true", help="per-region counts")
    parser.add_argument("--purge-days", type=int, metavar="N", help="delete items older than N days")
    args = parser.parse_args()

    db_path = Path(args.db)

    if args.stats:
        show_stats(db_path)
        return 0
    if args.search:
        search(db_path, args.search)
        return 0
    if args.latest is not None:
        latest(db_path, args.latest)
        return 0
    if args.regions:
        show_regions(db_path)
        return 0
    if args.purge_days is not None:
        purge(db_path, args.purge_days)
        return 0

    gather(db_path, args.sources or [])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
