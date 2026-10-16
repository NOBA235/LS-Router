"""Baseline fingerprints, written at seed time (data/osm_normalize.py has an identical copy) and recomputed by
audit_closure to detect any change to the road network. backend/tests/test_fingerprint_crosscheck.py keeps them equal."""
from __future__ import annotations

import hashlib


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return f"{float(v):.3f}"
    return str(v)


def segment_fingerprint(seg: dict) -> str:
    parts = [seg["id"], seg["from_place_id"], seg["to_place_id"], seg["is_bidirectional"], seg["length_km"], seg["travel_time_min"]]
    return hashlib.sha1("|".join(_fmt(p) for p in parts).encode()).hexdigest()[:16]


def network_fingerprint(segments: list, connections: list) -> str:
    h = hashlib.sha256()
    for s in sorted(segments, key=lambda s: s["id"]):
        h.update(f"{s['id']}:{segment_fingerprint(s)};".encode())
    for c in sorted(connections, key=lambda c: tuple(_fmt(c[k]) for k in ("segment_id", "direction", "from_place_id", "to_place_id"))):
        h.update(f"{c['segment_id']}|{c['direction']}|{c['from_place_id']}|{c['to_place_id']}|{_fmt(c['travel_time_min'])};".encode())
    return h.hexdigest()[:32]
