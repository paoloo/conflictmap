"""Cursor-on-Target event model and affiliation guessing."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class CoTEvent:
    uid: str
    lat: float
    lon: float
    cot_type: str = "a-U-G"           # e.g. a-f-G-E / a-h-A / a-n-G / a-u-G
    callsign: str = ""                # from <detail><contact>
    remarks: str = ""
    time: str = field(default_factory=_now_iso)
    start: str = field(default_factory=_now_iso)
    stale: str = field(default_factory=lambda: _now_iso())
    how: str = "m-g"
    detail_extra: str = ""            # raw XML inserted into <detail> (e.g. node markers)
    # on parse: preserve unknown detail children; not re-serialized
    hae: float = 9999999.0
    ce: float = 9999999.0
    le: float = 9999999.0

    def is_stale(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            return _parse_cot_time(self.stale) < now
        except ValueError:
            return True


AFFILIATION_COLORS = {
    "friendly": "#33cc66",
    "hostile": "#e03c3c",
    "neutral": "#33bbee",
    "unknown": "#eecc33",
}


def affiliation(cot_type: str) -> str:
    """Second dot-field of the CoT type: a-f-G -> friendly, a-h -> hostile ..."""
    parts = cot_type.split("-")
    if len(parts) < 2:
        return "unknown"
    return {"f": "friendly", "h": "hostile", "n": "neutral",
            "u": "unknown", "s": "unknown"}.get(parts[1][:1].lower(), "unknown")


def _parse_cot_time(s: str) -> datetime:
    # 2024-01-01T12:00:00.000Z (CoT uses ISO8601 with Z)
    s = s.strip().rstrip("Z")
    if "." in s:
        base, frac = s.split(".", 1)
        frac = (frac + "000000")[:6]
        s = f"{base}.{frac}"
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
