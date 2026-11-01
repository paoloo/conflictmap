"""CoT XML parse and build."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .model import CoTEvent


def parse_event(xml_bytes: bytes) -> CoTEvent | None:
    """Parse a CoT event from raw XML bytes. Returns None if unusable."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    if root.tag != "event":
        return None
    uid = root.get("uid")
    if not uid:
        return None
    point = root.find("point")
    try:
        lat = float(point.get("lat") or "0") if point is not None else 0.0
        lon = float(point.get("lon") or "0") if point is not None else 0.0
    except (TypeError, ValueError):
        return None
    hae = ce = le = 9999999.0
    if point is not None:
        try:
            hae = float(point.get("hae", "9999999"))
            ce = float(point.get("ce", "9999999"))
            le = float(point.get("le", "9999999"))
        except ValueError:
            pass

    callsign = ""
    remarks = ""
    detail_extra = ""
    detail = root.find("detail")
    if detail is not None:
        contact = detail.find("contact")
        if contact is not None:
            callsign = contact.get("callsign", "")
        remarks_el = detail.find("remarks")
        if remarks_el is not None:
            remarks = remarks_el.text or ""
        # preserve other detail children verbatim (used for node round-trip)
        keep = []
        for child in detail:
            if child.tag not in ("contact", "remarks"):
                keep.append(ET.tostring(child, encoding="unicode"))
        detail_extra = "".join(keep)

    return CoTEvent(
        uid=uid, lat=lat, lon=lon,
        cot_type=root.get("type", "a-u-G"),
        callsign=callsign, remarks=remarks,
        time=root.get("time", root.get("start", "")),
        start=root.get("start", ""),
        stale=root.get("stale", ""),
        how=root.get("how", "m-g"),
        detail_extra=detail_extra,
        hae=hae, ce=ce, le=le,
    )


def build_event(ev: CoTEvent) -> bytes:
    """Serialize a CoTEvent to XML bytes ready for the wire."""
    root = ET.Element("event", {
        "version": "2.0",
        "uid": ev.uid,
        "type": ev.cot_type,
        "how": ev.how,
        "time": ev.time,
        "start": ev.start,
        "stale": ev.stale,
    })
    ET.SubElement(root, "point", {
        "lat": f"{ev.lat:.7f}", "lon": f"{ev.lon:.7f}",
        "hae": f"{ev.hae:.1f}", "ce": f"{ev.ce:.1f}", "le": f"{ev.le:.1f}",
    })
    detail = ET.SubElement(root, "detail")
    contact = ET.SubElement(detail, "contact")
    if ev.callsign:
        contact.set("callsign", ev.callsign)
    if ev.detail_extra:
        # re-inject preserved detail children (e.g. node marker payload)
        try:
            extra = ET.fromstring(f"<root>{ev.detail_extra}</root>")
            for child in extra:
                detail.append(child)
        except ET.ParseError:
            pass
    if ev.remarks:
        r = ET.SubElement(detail, "remarks")
        r.text = ev.remarks
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)
