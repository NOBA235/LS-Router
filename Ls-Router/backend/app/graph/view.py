"""Builds the GET /graph payload from a graph snapshot plus the extra provenance/geometry fields.

Pure functions: no I/O, no Cypher. Everything shown comes from what the graph stores. Where a value is unknown it stays
``None`` (never defaulted, never inferred), and every unusual condition is reported in ``warnings`` rather than hidden.
"""
from __future__ import annotations

import json
import math

from .snapshot import Snapshot

PROVENANCE_FIELDS = ("source_url", "source_id", "retrieved_at", "last_verified", "confidence", "data_status")


def _provenance(entity, detail: dict) -> dict:
    return {
        "source_url": getattr(entity, "source_url", None),
        "source_id": detail.get("source_id"),
        "retrieved_at": detail.get("retrieved_at"),
        "last_verified": detail.get("last_verified"),
        "confidence": getattr(entity, "confidence", None),
        "data_status": getattr(entity, "data_status", None),
    }


def parse_geometry(raw) -> list | None:
    """Stored geometry is a JSON string ``[[lon, lat], ...]`` (GeoJSON order). Returns None for anything unusable.

    A straight line between two junctions is NOT substituted: ``None`` means the road shape is unknown, and a client must
    not present a schematic line as the real road.
    """
    if raw is None:
        return None
    try:
        pts = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(pts, list) or len(pts) < 2:
        return None
    out = []
    for p in pts:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p)):
            return None
        lon, lat = float(p[0]), float(p[1])
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return None
        out.append([lon, lat])
    return out


def build_graph_view(snap: Snapshot, details: dict, include_geometry: bool = True) -> dict:
    blocked = snap.blocked_map()
    warnings: list[str] = []
    ds = snap.dataset or {}
    kind = ds.get("kind")

    if not snap.segments:
        warnings.append("The graph holds no road segments: seed it first (python data\\seed.py).")
    if "synthetic" in (kind or "").lower():
        warnings.append("This graph is a SYNTHETIC TEST FIXTURE, not Dimapur-Kohima corridor data. "
                        "Its names, ids and source URLs are placeholders.")

    d_places, d_hosp = details.get("places", {}), details.get("hospitals", {})
    d_bridges, d_segs = details.get("bridges", {}), details.get("segments", {})

    places = []
    for p in sorted(snap.places.values(), key=lambda x: x.id):
        d = d_places.get(p.id, {})
        places.append({"id": p.id, "name": p.name, "type": p.type, "lat": p.lat, "lon": p.lon,
                       "population": d.get("population"), "reference_key": p.reference_key,
                       "provenance": _provenance(p, d)})

    hospitals = []
    for h in sorted(snap.hospitals.values(), key=lambda x: x.id):
        d = d_hosp.get(h.id, {})
        hospitals.append({"id": h.id, "name": h.name, "lat": h.lat, "lon": h.lon, "beds": h.beds,
                          "trauma_level": h.trauma_level, "access_place_id": h.access_place_id,
                          "snap_distance_m": h.snap_distance_m, "provenance": _provenance(h, d)})

    events = snap.events
    bridges = []
    for b in sorted(snap.bridges.values(), key=lambda x: x.id):
        d = d_bridges.get(b.id, {})
        history = [{"event_id": eid, "year": year,
                    "event_data_status": events[eid].data_status if eid in events else None,
                    "history_label": ("SIMULATED (demonstration scenario)"
                                      if eid in events and events[eid].data_status == "simulated"
                                      else "OBSERVED (historical report)"
                                      if eid in events and events[eid].data_status == "observed"
                                      else "UNKNOWN")}
                   for bid, eid, year in snap.failed_in if bid == b.id]
        bridges.append({"id": b.id, "name": b.name, "road_name": b.road_name, "lat": b.lat, "lon": b.lon,
                        "monsoon_risk": b.monsoon_risk, "segment_ids": list(b.segment_ids),
                        "failure_history": history, "provenance": _provenance(b, d)})

    segments, no_geometry, bad_geometry = [], [], []
    for s in sorted(snap.segments.values(), key=lambda x: x.id):
        d = d_segs.get(s.id, {})
        geometry = None
        if include_geometry:
            geometry = parse_geometry(d.get("geometry"))
            if geometry is None:
                (no_geometry if d.get("geometry") in (None, "", "[]") else bad_geometry).append(s.id)
        by = blocked.get(s.id, [])
        segments.append({"id": s.id, "name": s.name, "ref": s.ref, "highway": s.highway, "surface": d.get("surface"),
                         "length_km": s.length_km, "travel_time_min": s.travel_time_min,
                         "travel_time_basis": s.travel_time_basis, "is_bidirectional": s.is_bidirectional,
                         "on_nh29": s.on_nh29, "monsoon_risk": s.monsoon_risk, "corridor_id": s.corridor_id,
                         "from_place_id": s.from_place_id, "to_place_id": s.to_place_id,
                         "status": "blocked" if by else "open", "blocked_by": by, "geometry": geometry,
                         "provenance": _provenance(s, d)})
    if include_geometry and no_geometry:
        warnings.append(f"{len(no_geometry)} segment(s) have no stored road geometry (geometry is null): the road shape is "
                        "unknown. Do not draw a straight line between their endpoints as if it were the road.")
    if bad_geometry:
        warnings.append(f"{len(bad_geometry)} segment(s) have unreadable geometry and were returned with geometry=null: "
                        f"{bad_geometry[:5]}")

    closures = [{"id": e.id, "description": e.description, "status": e.status, "data_status": e.data_status,
                 "history_label": ("SIMULATED (demonstration scenario)" if e.data_status == "simulated"
                                   else "OBSERVED (historical report)"),
                 "remembered": bool(e.remembered_at),
                 "cause": e.cause, "origin": e.origin, "confidence": e.confidence, "started_at": e.started_at,
                 "ended_at": e.ended_at, "remembered_at": e.remembered_at, "location_text": e.location_text,
                 "source_url": e.source_url, "blocks_segments": list(e.blocks_segments),
                 "blocks_bridges": list(e.blocks_bridges), "near_places": list(e.near_places)}
                for e in sorted(events.values(), key=lambda x: x.id)]

    connections = [
        {
            "from_place_id": c.from_id,
            "to_place_id": c.to_id,
            "segment_id": c.segment_id,
            "direction": c.direction,
            "travel_time_min": c.travel_time_min,
            "status": "blocked" if c.segment_id in blocked else "open",
            "blocked_by": blocked.get(c.segment_id, []),
        }
        for c in snap.connections
    ]

    seasons = [
        {
            "id": s.id,
            "name": s.name,
            "start_month": s.start_month,
            "end_month": s.end_month,
            "segment_ids": list(s.segment_ids),
        }
        for s in sorted(snap.seasons.values(), key=lambda x: x.id)
    ]

    entities = places + hospitals + bridges + segments
    verified = sum(1 for e in entities if e["provenance"]["confidence"] == "verified")
    return {
        "dataset": ({"id": ds.get("id"), "kind": kind, "retrieved_at": ds.get("retrieved_at"),
                     "attribution": ds.get("attribution"), "network_fingerprint": ds.get("network_fingerprint")}
                    if ds else None),
        "corridors": sorted(snap.corridors()),
        "counts": {"places": len(places), "hospitals": len(hospitals), "bridges": len(bridges), "segments": len(segments),
                   "connections": len(connections), "closures": len(closures),
                   "active_closures": len(snap.active_events()), "blocked_segments": len(blocked)},
        "provenance_summary": {"entities": len(entities), "verified": verified, "unverified": len(entities) - verified,
                               "note": "'verified' requires a recorded manual check (last_verified); everything else is "
                                       "unverified, including all OpenStreetMap-derived records."},
        "places": places, "hospitals": hospitals, "bridges": bridges, "segments": segments,
        "connections": connections, "closures": closures, "seasons": seasons,
        "warnings": warnings,
    }


