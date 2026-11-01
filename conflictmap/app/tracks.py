"""CoT multicast listener (thin adapter over app.cot.transport).

Receive-only: the TUI shows where *other* things are, not where you are —
no self-SA beacon (the Qt app's CoTSender is intentionally not used here).
"""
from __future__ import annotations

import threading


class TrackFeed:
    """CoT events keyed by uid, with stale pruning (TrackStore parity)."""

    def __init__(self, enabled: bool, group: str, port: int) -> None:
        self.enabled = enabled
        self.group = group
        self.port = port
        self.tracks: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.error = ""
        self._listener = None

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            from ..cot.model import CoTEvent
            from ..cot.transport import CoTListener
        except ImportError as error:
            self.error = f"cot unavailable: {error}"
            return

        def on_event(ev: CoTEvent) -> None:
            from ..cot.model import affiliation
            with self.lock:
                self.tracks[ev.uid] = {
                    "lat": ev.lat, "lon": ev.lon, "callsign": ev.callsign,
                    "affiliation": affiliation(ev.cot_type),
                    "remarks": ev.remarks, "stale": ev.stale,
                }

        try:
            self._listener = CoTListener(self.group, self.port, on_event)
            self._listener.daemon = True
            self._listener.start()
        except (OSError, PermissionError) as error:
            self.error = f"cot failed: {error}"

    def snapshot(self) -> dict[str, dict]:
        """Live tracks only (stale ones pruned like TrackStore._prune)."""
        from ..cot.model import CoTEvent
        from datetime import datetime, timezone
        out = {}
        now = datetime.now(timezone.utc)
        with self.lock:
            for uid, track in list(self.tracks.items()):
                ev = CoTEvent(uid=uid, lat=track["lat"], lon=track["lon"],
                              stale=track.get("stale") or "")
                if ev.is_stale(now):
                    continue
                out[uid] = track
        return out

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
