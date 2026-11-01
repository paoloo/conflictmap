"""Multicast CoT: listener thread and position sender thread.

Both push raw XML/CoTEvents to the UI through a callback; the UI marshals
onto the Qt main thread (queued signal/slot).
"""
from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Callable

from .model import CoTEvent, _now_iso
from .parse import build_event


class CoTListener(threading.Thread):
    """UDP multicast listener. Calls on_event(CoTEvent) per received event."""

    def __init__(self, group: str, port: int, on_event: Callable[[CoTEvent], None]) -> None:
        super().__init__(name="cot-listener", daemon=True)
        self.group = group
        self.port = port
        self.on_event = on_event
        self._stop = threading.Event()

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", self.port))
        except OSError:
            return  # port taken (e.g. another instance); stay quiet
        mreq = struct.pack("4sL", socket.inet_aton(self.group), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(0.5)
        from .parse import parse_event
        while not self._stop.is_set():
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            ev = parse_event(data)
            if ev is not None:
                try:
                    self.on_event(ev)
                except Exception:
                    pass  # never kill the listener on a UI hiccup
        sock.close()

    def stop(self) -> None:
        self._stop.set()


class CoTSender(threading.Thread):
    """Periodically broadcasts own position as a CoT marker."""

    def __init__(self, group: str, port: int, callsign: str,
                 get_position: Callable[[], tuple[float, float]],
                 interval_s: float) -> None:
        super().__init__(name="cot-sender", daemon=True)
        self.group = group
        self.port = port
        self.callsign = callsign
        self.get_position = get_position
        self.interval_s = max(0.5, interval_s)
        self.uid = f"{callsign}.self"
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # send even if we are the only host: allow loopback for local testing
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    def send_once(self) -> None:
        lat, lon = self.get_position()
        stale = _iso_later(60)
        ev = CoTEvent(
            uid=self.uid, lat=lat, lon=lon,
            cot_type="a-f-G", callsign=self.callsign,
            remarks="self position",
            stale=stale,
        )
        self._sock.sendto(build_event(ev), (self.group, self.port))

    def run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.send_once()
            except Exception:
                pass  # keep the sender alive on transient network errors
        self._sock.close()

    def stop(self) -> None:
        self._stop.set()


def _iso_later(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
