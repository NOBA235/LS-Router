"""Turn cached Overpass JSON into the Landslide Router graph dataset. Standard library only.

Pure functions: no network, no database. See data/README.md for the modelling rules.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from pipeline_config import PipelineConfig

SCHEMA_VERSION = 1
CONFIDENCE_VALUES = {"verified", "unverified"}
DATA_STATUS_VALUES = {"observed", "derived", "simulated"}
MONSOON_RISK_VALUES = {"low", "medium", "high"}
PLACE_TYPE_MAP = {"city": "town", "town": "town", "village": "village", "hamlet": "village"}
PLACE_RANK = {"city": 0, "town": 1, "village": 2, "hamlet": 3}
OVERLAY_FIELDS = {"name", "population", "beds", "trauma_level", "monsoon_risk", "surface"}


class NormalizationError(RuntimeError):
    pass


# --------------------------------------------------------------------------- helpers
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def fold(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")


def osm_url(osm_type: str, osm_id: int) -> str:
    return f"https://www.openstreetmap.org/{osm_type}/{osm_id}"


_SPEED = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kmh|kph)?\s*$", re.I)


def parse_maxspeed_kmh(value: str | None) -> float | None:
    """Numeric maxspeed in km/h, or None for missing/unusable values ('none', 'signals', ...)."""
    m = _SPEED.match(value or "")
    if not m:
        return None
    kmh = float(m.group(1)) * (1.609344 if (m.group(2) or "").lower() == "mph" else 1.0)
    return kmh if 3 <= kmh <= 140 else None


def oneway_flags(tags: dict) -> tuple[bool, bool]:
    """(travel allowed in way order, travel allowed against way order)."""
    v = (tags.get("oneway") or "").lower()
    if v in ("yes", "true", "1"):
        return True, False
    if v == "-1":
        return False, True
    if v != "no" and tags.get("junction") == "roundabout":
        return True, False
    return True, True


def _pos_int(value) -> int | None:
    try:
        n = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _in_bbox(lat: float, lon: float, bbox) -> bool:
    s, w, n, e = bbox
    return s <= lat <= n and w <= lon <= e


def _approx_m(lat1, lon1, lat2, lon2) -> float:
    dy = (lat2 - lat1) * 110574.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------- baseline fingerprints
# Identical copy of backend/app/graph/fingerprint.py (kept equal by a test). Used by audit_closure.
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


# --------------------------------------------------------------------------- road network
@dataclass
class Edge:
    u: int
    v: int
    way_id: int
    idx: int
    fwd: bool
    rev: bool
    highway: str
    ref: str
    name: str
    surface: str
    speed_tag: float | None
    bridge: str
    length_km: float
    nh29: bool


def _flags_along(e: Edge, frm: int) -> tuple[bool, bool]:
    return (e.fwd, e.rev) if e.u == frm else (e.rev, e.fwd)


def _other(e: Edge, n: int) -> int:
    return e.v if e.u == n else e.u


def _components(edges) -> dict:
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for u, v in edges:
        ru, rv = find(u), find(v)
        if ru != rv:
            parent[ru] = rv
    return {n: find(n) for n in list(parent)}


def _passes_through(edges: dict, adj: dict, n: int) -> bool:
    """A degree-2 vertex can be absorbed into a segment only if both edges agree on road and direction."""
    k1, k2 = adj[n]
    e1, e2 = edges[k1], edges[k2]
    if (e1.ref, e1.highway) != (e2.ref, e2.highway):
        return False
    return _flags_along(e1, _other(e1, n)) == _flags_along(e2, n)


def _walk_chains(edges: dict, adj: dict, keep: set) -> list:
    visited: set = set()
    chains = []

    def walk(start):
        for key0 in sorted(adj[start]):
            if key0 in visited:
                continue
            path, elist, cur, key = [start], [], start, key0
            while True:
                visited.add(key)
                e = edges[key]
                nxt = _other(e, cur)
                elist.append(e)
                path.append(nxt)
                if nxt in keep:
                    break
                a, b = adj[nxt]
                key = b if a == key else a
                cur = nxt
            chains.append((path, elist))

    for start in sorted(keep):
        walk(start)
    while True:  # pure cycles of degree-2 vertices have no keep vertex yet
        rest = sorted(k for k in edges if k not in visited)
        if not rest:
            return chains
        keep.add(rest[0][0])
        walk(rest[0][0])


def _read_roads(roads_raw: dict):
    nodes, ways = {}, []
    for el in roads_raw.get("elements", []):
        if el.get("type") == "node" and "lat" in el and "lon" in el:
            nodes[el["id"]] = (float(el["lat"]), float(el["lon"]))
        elif el.get("type") == "way" and isinstance(el.get("nodes"), list):
            ways.append(el)
    return nodes, ways


# --------------------------------------------------------------------------- features
def _features(features_raw: dict, curated: dict, cfg: PipelineConfig, stats: Counter, issue) -> tuple[list, list]:
    places, hospitals = [], []
    for el in features_raw.get("elements", []):
        t = el.get("tags") or {}
        if "lat" in el and "lon" in el:
            lat, lon = float(el["lat"]), float(el["lon"])
        elif isinstance(el.get("center"), dict):
            lat, lon = float(el["center"]["lat"]), float(el["center"]["lon"])
        else:
            stats["features_without_coordinates"] += 1
            continue
        if el.get("type") == "node" and t.get("place"):
            tag = t["place"]
            if tag not in PLACE_TYPE_MAP:
                stats["places_other_tag_skipped"] += 1
                continue
            if not (t.get("name") or "").strip():
                stats["places_unnamed_skipped"] += 1
                continue
            places.append({"origin": "osm", "osm_type": "node", "osm_id": el["id"], "name": t["name"].strip(),
                           "lat": lat, "lon": lon, "tag": tag, "population": _pos_int(t.get("population"))})
        if t.get("amenity") == "hospital" or t.get("healthcare") == "hospital":
            hospitals.append({"origin": "osm", "osm_type": el.get("type", "node"), "osm_id": el["id"],
                              "name": (t.get("name") or "").strip() or None, "lat": lat, "lon": lon,
                              "beds": _pos_int(t.get("beds"))})
    add = curated.get("additions") or {}
    for item in add.get("places", []):
        ok = _curated_ok(item, ("name", "lat", "lon", "source_url", "compiled_on", "type"), "addition place", issue)
        if ok and item["type"] in ("village", "town"):
            places.append({"origin": "curated", "osm_type": "curated", "osm_id": slug(item["name"]), "name": item["name"],
                           "lat": float(item["lat"]), "lon": float(item["lon"]), "tag": item["type"],
                           "population": _pos_int(item.get("population")), "extra": item})
    for item in add.get("hospitals", []):
        if _curated_ok(item, ("name", "lat", "lon", "source_url", "compiled_on"), "addition hospital", issue):
            hospitals.append({"origin": "curated", "osm_type": "curated", "osm_id": slug(item["name"]), "name": item["name"],
                              "lat": float(item["lat"]), "lon": float(item["lon"]), "beds": _pos_int(item.get("beds")),
                              "extra": item})
    return _dedupe_places(places, cfg, stats), _dedupe_hospitals(hospitals, cfg, stats)


def _curated_ok(item: dict, required: tuple, what: str, issue) -> bool:
    missing = [k for k in required if item.get(k) in (None, "")]
    if missing:
        issue("error", "curated_incomplete", f"{what} {item.get('name')!r} lacks {missing}")
        return False
    return True


def _dedupe_places(places: list, cfg: PipelineConfig, stats: Counter) -> list:
    places = sorted(places, key=lambda p: (PLACE_RANK.get(p["tag"], 9) if p["origin"] == "osm" else -1,
                                            fold(p["name"]), str(p["osm_id"])))
    kept: list = []
    for p in places:
        dup = any(fold(q["name"]) == fold(p["name"]) and _approx_m(p["lat"], p["lon"], q["lat"], q["lon"]) <= cfg.dedupe_place_m
                  for q in kept)
        if dup:
            stats["places_duplicates_skipped"] += 1
        else:
            kept.append(p)
    return kept


def _dedupe_hospitals(hospitals: list, cfg: PipelineConfig, stats: Counter) -> list:
    order = {"node": 0, "way": 1, "relation": 2, "curated": -1}
    hospitals = sorted(hospitals, key=lambda h: (order.get(h["osm_type"], 3), str(h["osm_id"])))
    kept: list = []
    for h in hospitals:
        limit = cfg.dedupe_hospital_m if h["name"] else 50.0
        dup = any(((fold(q["name"]) == fold(h["name"])) if h["name"] and q["name"] else True)
                  and _approx_m(h["lat"], h["lon"], q["lat"], q["lon"]) <= limit for q in kept)
        if dup:
            stats["hospitals_duplicates_skipped"] += 1
        else:
            kept.append(h)
    return kept


# --------------------------------------------------------------------------- main entry
def normalize(roads_raw: dict, features_raw: dict, source_meta: dict, cfg: PipelineConfig | None = None,
              curated: dict | None = None, today: date | None = None, dataset_kind: str = "real") -> dict:
    cfg = cfg or PipelineConfig()
    curated = curated or {}
    today = today or date.today()
    stats: Counter = Counter()
    issues: list = []

    def issue(severity, code, message, **kw):
        issues.append({"severity": severity, "code": code, "message": message, **kw})

    retrieved_at = source_meta.get("retrieved_at")
    nh_re = re.compile(cfg.nh_ref_regex, re.I)

    # 1. read + select ways -------------------------------------------------------
    nodes, all_ways = _read_roads(roads_raw)
    ref_counts: Counter = Counter()
    chosen = []
    for w in sorted(all_ways, key=lambda x: x["id"]):
        t = w.get("tags") or {}
        hw = t.get("highway")
        if not hw:
            continue
        ref_counts[(t.get("ref") or "").strip() or "(no ref)"] += 1
        if hw not in cfg.highway_classes:
            stats["ways_other_highway_class"] += 1
        elif t.get("area") == "yes":
            stats["ways_area_skipped"] += 1
        elif t.get("access") in ("no", "private"):
            stats["ways_access_restricted_skipped"] += 1
        else:
            chosen.append(w)
    highway_counts = Counter((w.get("tags") or {}).get("highway") for w in chosen)

    # 2. edges (dedupe, clip to bbox) --------------------------------------------
    edges: dict = {}
    for w in chosen:
        t = w.get("tags") or {}
        fwd, rev = oneway_flags(t)
        if (t.get("oneway") or "").lower() in ("reversible", "alternating"):
            stats["ways_oneway_reversible_treated_bidirectional"] += 1
        ref = (t.get("ref") or "").strip()
        bridge = (t.get("bridge") or "").strip()
        bridge = "" if bridge in ("", "no") else bridge
        ids = w["nodes"]
        for i, (a, b) in enumerate(zip(ids, ids[1:])):
            if a == b:
                continue
            if a not in nodes or b not in nodes:
                stats["edges_missing_nodes"] += 1
                continue
            (la, lo), (lb, lob) = nodes[a], nodes[b]
            if not (_in_bbox(la, lo, cfg.bbox) and _in_bbox(lb, lob, cfg.bbox)):
                stats["edges_clipped_outside_bbox"] += 1
                continue
            key = (a, b) if a < b else (b, a)
            if key in edges:
                stats["edges_duplicate_skipped"] += 1
                continue
            edges[key] = Edge(a, b, w["id"], i, fwd, rev, t["highway"], ref, (t.get("name") or "").strip(),
                              (t.get("surface") or "").strip(), parse_maxspeed_kmh(t.get("maxspeed")), bridge,
                              haversine_km(la, lo, lb, lob), bool(ref and nh_re.search(ref)))

    nh_edges = [e for e in edges.values() if e.nh29]
    if not nh_edges:
        top = ", ".join(f"{r} ({n})" for r, n in ref_counts.most_common(8)) or "none"
        raise NormalizationError(f"No way matches the NH29 ref pattern {cfg.nh_ref_regex!r}. Refs found: {top}. "
                                 "Adjust --nh-ref-regex after checking the fetch summary.")

    # 3. keep only components that touch NH29 ------------------------------------
    comp = _components(edges)
    keep_roots = {comp[e.u] for e in nh_edges}
    dropped: dict = defaultdict(list)
    for key, e in list(edges.items()):
        if comp[e.u] not in keep_roots:
            dropped[comp[e.u]].append(e)
            del edges[key]
    dropped_components = [{"edges": len(es), "way_ids": sorted({e.way_id for e in es})[:5],
                           "refs": sorted({e.ref for e in es if e.ref})[:3]}
                          for es in sorted(dropped.values(), key=lambda x: -len(x))]

    # 4. features and attachment to road vertices --------------------------------
    place_feats, hosp_feats = _features(features_raw, curated, cfg, stats, issue)
    vertex_ids = sorted({n for k in edges for n in k})

    def nearest(lat, lon, k=25):
        return heapq.nsmallest(k, ((_approx_m(lat, lon, *nodes[n]), n) for n in vertex_ids))

    claimed: dict = {}
    unattached_places = []
    for p in sorted(place_feats, key=lambda p: (PLACE_RANK.get(p["tag"], 9) if p["origin"] == "osm" else (0 if p["tag"] == "town" else 2),
                                                -(p["population"] or 0), fold(p["name"]), str(p["osm_id"]))):
        limit = cfg.snap_threshold_m["town" if PLACE_TYPE_MAP.get(p["tag"], p["tag"]) == "town" else "village"]
        cands = nearest(p["lat"], p["lon"])
        pick = next(((d, n) for d, n in cands if d <= limit and n not in claimed), None)
        if pick:
            claimed[pick[1]] = (p, round(haversine_km(p["lat"], p["lon"], *nodes[pick[1]]) * 1000, 1))
        else:
            unattached_places.append({"name": p["name"], "type": PLACE_TYPE_MAP.get(p["tag"], p["tag"]),
                                      "id": f"{p['osm_type']}/{p['osm_id']}",
                                      "nearest_road_vertex_m": round(cands[0][0], 1) if cands else None,
                                      "reason": "no free road vertex within threshold"})
    hosp_attach = []
    unattached_hospitals = []
    for h in hosp_feats:
        cands = nearest(h["lat"], h["lon"], 1)
        if cands and cands[0][0] <= cfg.snap_threshold_m["hospital"]:
            hosp_attach.append((h, cands[0][1], round(haversine_km(h["lat"], h["lon"], *nodes[cands[0][1]]) * 1000, 1)))
        else:
            unattached_hospitals.append({"name": h["name"], "id": f"{h['osm_type']}/{h['osm_id']}",
                                         "nearest_road_vertex_m": round(cands[0][0], 1) if cands else None,
                                         "reason": "no road vertex within threshold"})

    # 5. vertices to keep, chains -> segments -----------------------------------
    adj: dict = defaultdict(list)
    for key in sorted(edges):
        adj[key[0]].append(key)
        adj[key[1]].append(key)
    keep = {n for n, ks in adj.items() if len(ks) != 2}
    keep |= set(claimed) | {n for _, n, _ in hosp_attach}
    keep |= {n for n, ks in adj.items() if len(ks) == 2 and not _passes_through(edges, adj, n)}
    chains = _walk_chains(edges, adj, keep)

    seg_records, edge_to_seg = [], {}
    pending = []  # (record, path)
    for path, elist in chains:
        flags = [_flags_along(e, path[i]) for i, e in enumerate(elist)]
        along, against = flags[0]
        if path[0] == path[-1]:
            stats["segments_self_loop_dropped"] += 1
            continue
        if not along and not against:
            stats["segments_unusable_dropped"] += 1
            continue
        if not along and against:
            path, elist, along, against = path[::-1], elist[::-1], True, False
        elif along and against and path[0] > path[-1]:
            path, elist = path[::-1], elist[::-1]
        length_km, minutes, bases, by_name, surfaces = 0.0, 0.0, set(), Counter(), set()
        for e in elist:
            length_km += e.length_km
            by_name[e.name] += e.length_km if e.name else 0
            if e.surface:
                surfaces.add(e.surface)
            if e.speed_tag:
                speed, basis = e.speed_tag, "tagged"
            elif cfg.use_assumed_speeds:
                speed = cfg.default_speeds_kmh.get(e.highway.replace("_link", ""), cfg.default_speeds_kmh["default"])
                basis = "assumed"
            else:
                speed, basis = None, "missing"
            bases.add(basis)
            minutes += (e.length_km / speed * 60) if speed else 0.0
        basis = "missing" if "missing" in bases else (bases.pop() if len(bases) == 1 else "mixed")
        way_ids = list(dict.fromkeys(e.way_id for e in elist))
        names = [n for n, _ in by_name.most_common() if n]
        sid = f"seg-{path[0]}-{path[-1]}-{elist[0].way_id}"
        rec = {"id": sid, "name": names[0] if names else None, "ref": elist[0].ref or None, "highway": elist[0].highway,
               "length_km": round(length_km, 3), "travel_time_min": None if basis == "missing" else round(minutes, 2),
               "travel_time_basis": basis, "surface": None if not surfaces else (next(iter(surfaces)) if len(surfaces) == 1 else "mixed"),
               "monsoon_risk": None, "confidence": "unverified", "source_url": osm_url("way", way_ids[0]),
               "source_id": ";".join(f"way/{w}" for w in way_ids), "retrieved_at": retrieved_at, "last_verified": None,
               "data_status": "derived", "is_bidirectional": bool(along and against), "on_nh29": elist[0].nh29,
               "way_ids": [f"way/{w}" for w in way_ids], "from_node": path[0], "to_node": path[-1],
               "geometry": json.dumps([[round(nodes[n][1], 5), round(nodes[n][0], 5)] for n in path], separators=(",", ":"))}
        seg_records.append(rec)
        for e in elist:
            edge_to_seg[(min(e.u, e.v), max(e.u, e.v))] = sid
    seg_records.sort(key=lambda s: s["id"])

    # 6. places at used vertices --------------------------------------------------
    used = {n for s in seg_records for n in (s["from_node"], s["to_node"])}
    places, place_by_node = [], {}
    for n in sorted(used):
        lat, lon = nodes[n]
        if n in claimed:
            p, dist = claimed[n]
            curated_p = p["origin"] == "curated"
            rec = {"id": f"place-{p['osm_type']}-{p['osm_id']}", "name": p["name"], "type": PLACE_TYPE_MAP.get(p["tag"], p["tag"]),
                   "lat": p["lat"], "lon": p["lon"], "population": p["population"], "confidence": "unverified",
                   "source_url": p["extra"]["source_url"] if curated_p else osm_url("node", p["osm_id"]),
                   "source_id": None if curated_p else f"node/{p['osm_id']}",
                   "retrieved_at": p["extra"]["compiled_on"] if curated_p else retrieved_at, "last_verified": None,
                   "data_status": "observed", "osm_place_tag": None if curated_p else p["tag"],
                   "network_node_id": f"node/{n}", "snap_distance_m": dist, "reference_key": None,
                   "origin": p["origin"]}
        else:
            rec = {"id": f"junction-{n}", "name": None, "type": "junction", "lat": lat, "lon": lon, "population": None,
                   "confidence": "unverified", "source_url": osm_url("node", n), "source_id": f"node/{n}",
                   "retrieved_at": retrieved_at, "last_verified": None, "data_status": "derived", "osm_place_tag": None,
                   "network_node_id": f"node/{n}", "snap_distance_m": None, "reference_key": None, "origin": "derived"}
        places.append(rec)
        place_by_node[n] = rec["id"]
    for n, (p, _) in claimed.items():
        if n not in used:
            unattached_places.append({"name": p["name"], "type": PLACE_TYPE_MAP.get(p["tag"], p["tag"]),
                                      "id": f"{p['osm_type']}/{p['osm_id']}", "nearest_road_vertex_m": None,
                                      "reason": "its road segments were dropped"})
    for s in seg_records:
        s["from_place_id"], s["to_place_id"] = place_by_node[s["from_node"]], place_by_node[s["to_node"]]
        del s["from_node"], s["to_node"]

    connections = []
    for s in seg_records:
        connections.append({"from_place_id": s["from_place_id"], "to_place_id": s["to_place_id"], "segment_id": s["id"],
                            "travel_time_min": s["travel_time_min"], "direction": "forward"})
        if s["is_bidirectional"]:
            connections.append({"from_place_id": s["to_place_id"], "to_place_id": s["from_place_id"], "segment_id": s["id"],
                                "travel_time_min": s["travel_time_min"], "direction": "reverse"})
    connections.sort(key=lambda c: (c["segment_id"], c["direction"]))
    for s in seg_records:
        s["baseline_fp"] = segment_fingerprint(s)

    # 7. hospitals ----------------------------------------------------------------
    hospitals, located_at = [], []
    for h, n, dist in hosp_attach:
        if n not in used:
            unattached_hospitals.append({"name": h["name"], "id": f"{h['osm_type']}/{h['osm_id']}",
                                         "nearest_road_vertex_m": dist, "reason": "its road segments were dropped"})
            continue
        curated_h = h["origin"] == "curated"
        hid = f"hospital-{h['osm_type']}-{h['osm_id']}"
        hospitals.append({"id": hid, "name": h["name"], "lat": h["lat"], "lon": h["lon"], "beds": h["beds"],
                          "confidence": "unverified",
                          "source_url": h["extra"]["source_url"] if curated_h else osm_url(h["osm_type"], h["osm_id"]),
                          "source_id": None if curated_h else f"{h['osm_type']}/{h['osm_id']}",
                          "retrieved_at": h["extra"]["compiled_on"] if curated_h else retrieved_at, "last_verified": None,
                          "data_status": "observed", "origin": h["origin"]})
        located_at.append({"hospital_id": hid, "place_id": place_by_node[n], "snap_distance_m": dist,
                           "method": "nearest_road_vertex", "data_status": "derived"})
    hospitals.sort(key=lambda h: h["id"])
    located_at.sort(key=lambda x: x["hospital_id"])

    # 8. bridges (a bridge sits ON every segment that carries one of its edges) ---
    by_way: dict = defaultdict(list)
    for key, e in edges.items():
        if e.bridge and key in edge_to_seg:
            by_way[e.way_id].append(e)
    bridges, bridge_on = [], []
    way_tags = {w["id"]: (w.get("tags") or {}) for w in chosen}
    # A bridge way usually carries its road's name. It is the BRIDGE's name only if bridge:name says so, or if no
    # non-bridge part of the same road uses that name.
    road_names = {(e.ref, e.name) for e in edges.values() if e.name and not e.bridge}
    for wid, es in sorted(by_way.items()):
        es.sort(key=lambda e: e.idx)
        seq = [es[0].u] + [e.v for e in es]
        mid = nodes[seq[len(seq) // 2]]
        t = way_tags[wid]
        tag_name = (t.get("name") or "").strip()
        bname = (t.get("bridge:name") or "").strip() or (tag_name if tag_name and (es[0].ref, tag_name) not in road_names else "")
        bid = f"bridge-way-{wid}"
        bridges.append({"id": bid, "name": bname or None, "road_name": tag_name if tag_name and tag_name != bname else None,
                        "lat": mid[0], "lon": mid[1],
                        "monsoon_risk": None, "confidence": "unverified", "source_url": osm_url("way", wid),
                        "source_id": f"way/{wid}", "retrieved_at": retrieved_at, "last_verified": None,
                        "data_status": "observed", "bridge_tag": es[0].bridge,
                        "length_m": round(sum(e.length_km for e in es) * 1000, 1), "origin": "osm"})
        for sid in sorted({edge_to_seg[(min(e.u, e.v), max(e.u, e.v))] for e in es}):
            bridge_on.append({"bridge_id": bid, "segment_id": sid})

    # 9. corridor, dataset, season, observed events ---------------------------------
    corridor = {"id": cfg.corridor_id, "name": cfg.corridor_name, "bbox_south": cfg.bbox[0], "bbox_west": cfg.bbox[1],
                "bbox_north": cfg.bbox[2], "bbox_east": cfg.bbox[3], "nh_ref_regex": cfg.nh_ref_regex}
    src = {k: source_meta.get(k) for k in ("name", "license", "license_url", "attribution", "retrieved_at",
                                           "osm_base_timestamp", "endpoint", "query_sha256")}
    dataset = {"id": "dataset-osm-corridor", "kind": dataset_kind, **{k: v for k, v in src.items() if v is not None},
               "bbox": ",".join(str(x) for x in cfg.bbox), "generator": "data/osm_normalize.py",
               "schema_version": SCHEMA_VERSION, "network_fingerprint": network_fingerprint(seg_records, connections)}
    season = {"id": "monsoon", "name": "Monsoon", "start_month": cfg.monsoon_start_month, "end_month": cfg.monsoon_end_month,
              "data_status": "derived", "confidence": "unverified",
              "basis": "modeling assumption (no source attached); confirm the months before relying on them"}
    season_affects = [{"season_id": "monsoon", "segment_id": s["id"], "basis": "assumed_all_segments_exposed"} for s in seg_records]

    ds = {"schema_version": SCHEMA_VERSION,
          "meta": {"kind": dataset_kind, "source": src, "bbox": list(cfg.bbox), "config": cfg.to_dict()},
          "corridor": corridor, "datasets": [dataset], "seasons": [season], "places": places, "hospitals": hospitals,
          "bridges": bridges, "segments": [{k: v for k, v in s.items()} for s in seg_records], "connections": connections,
          "bridge_on": bridge_on, "hospital_located_at": located_at, "season_affects": season_affects,
          "observed_events": [], "event_near_place": [], "event_blocks": [], "event_failed_bridges": [],
          "reference_places": {}}

    _apply_reference_places(ds, cfg, issue)
    _apply_verification(ds, curated.get("verification") or {}, today, stats, issue)
    unresolved_events = _apply_risk_events(ds, curated.get("risk_events") or {}, issue)
    ref_check = _hospital_reference_check(ds, curated.get("hospital_reference") or {})

    ds["report"] = _report(ds, stats, issues, ref_counts, highway_counts, dropped_components, unattached_places,
                           unattached_hospitals, unresolved_events, ref_check, cfg)
    issues.extend(validate_dataset(ds, today))
    ds["report"]["validation"] = {"errors": [i for i in issues if i["severity"] == "error"],
                                  "warnings": [i for i in issues if i["severity"] == "warning"]}
    return ds


# --------------------------------------------------------------------------- post steps
def _apply_reference_places(ds: dict, cfg: PipelineConfig, issue) -> None:
    for key, wanted in cfg.reference_names.items():
        cands = sorted((p for p in ds["places"] if p["type"] == "town" and fold(p["name"]) == fold(wanted)),
                       key=lambda p: (p.get("osm_place_tag") != "city", p["id"]))
        if not cands:
            ds["reference_places"][key] = None
            continue
        if len(cands) > 1:
            issue("warning", "ambiguous_reference_place", f"{len(cands)} towns named {wanted!r}; using {cands[0]['id']}")
        cands[0]["reference_key"] = key
        ds["reference_places"][key] = cands[0]["id"]


def _apply_verification(ds: dict, overlay: dict, today: date, stats: Counter, issue) -> None:
    recs = {r["id"]: r for k in ("places", "hospitals", "bridges", "segments") for r in ds[k]}
    for entry in overlay.get("entries", []):
        rid = entry.get("id")
        rec = recs.get(rid)
        if rec is None:
            issue("warning", "verification_unresolved", f"verification entry for unknown id {rid!r}")
            stats["verification_unresolved"] += 1
            continue
        conf = entry.get("confidence", "unverified")
        lv = entry.get("last_verified")
        if conf not in CONFIDENCE_VALUES:
            issue("error", "verification_bad_confidence", f"{rid}: confidence {conf!r}")
            continue
        if conf == "verified":
            try:
                ok = bool(entry.get("verification_source_url")) and date.fromisoformat(lv) <= today
            except (TypeError, ValueError):
                ok = False
            if not ok:
                issue("error", "verification_incomplete",
                      f"{rid}: 'verified' needs verification_source_url and a real last_verified date not in the future")
                continue
        elif lv:
            issue("error", "verification_unverified_with_date", f"{rid}: last_verified set on an unverified entry")
            continue
        bad = set(entry.get("set", {})) - OVERLAY_FIELDS
        if bad:
            issue("error", "verification_bad_field", f"{rid}: cannot set {sorted(bad)}")
            continue
        rec.update(entry.get("set", {}))
        rec["confidence"], rec["last_verified"] = conf, lv if conf == "verified" else None
        rec["verification_source_url"] = entry.get("verification_source_url")
        rec["verification_notes"] = entry.get("notes")
        stats["verification_applied"] += 1


def _apply_risk_events(ds: dict, cfg: dict, issue) -> list:
    places_by_name: dict = defaultdict(list)
    for p in ds["places"]:
        if p["name"]:
            places_by_name[fold(p["name"])].append(p["id"])
    seg_ids, bridge_ids = {s["id"] for s in ds["segments"]}, {b["id"] for b in ds["bridges"]}
    unresolved = []
    for ev in cfg.get("events", []):
        sources = ev.get("sources") or []
        if not (ev.get("id") and ev.get("description") and sources and sources[0].get("url")):
            issue("error", "event_incomplete", f"risk event {ev.get('id')!r} needs id, description and a source url")
            continue
        ds["observed_events"].append({
            "id": ev["id"], "description": ev["description"], "lat": ev.get("lat"), "lon": ev.get("lon"),
            "started_at": ev.get("started_at"), "ended_at": ev.get("ended_at"), "cause": ev.get("cause"),
            "status": ev.get("status", "historical"), "data_status": "observed", "confidence": "unverified",
            "last_verified": None, "date_precision": ev.get("date_precision"), "date_note": ev.get("date_note"),
            "location_text": ev.get("location_text"), "source_url": sources[0]["url"],
            "source_urls": [s["url"] for s in sources], "origin": "curated_report"})
        for name in ev.get("place_names", []):
            hits = places_by_name.get(fold(name), [])
            if len(hits) == 1:
                ds["event_near_place"].append({"event_id": ev["id"], "place_id": hits[0], "basis": "named_in_source",
                                               "data_status": "derived"})
            else:
                unresolved.append({"event_id": ev["id"], "place_name": name, "matches": len(hits)})
        for sid in ev.get("segment_ids", []):
            if sid in seg_ids:
                ds["event_blocks"].append({"event_id": ev["id"], "target_id": sid, "target_label": "RoadSegment"})
            else:
                issue("error", "event_unknown_segment", f"{ev['id']}: segment {sid!r} not in dataset")
        for bid in ev.get("bridge_ids", []):
            if bid in bridge_ids:
                ds["event_failed_bridges"].append({"event_id": ev["id"], "bridge_id": bid,
                                                   "year": int(str(ev.get("started_at", ""))[:4] or 0) or None})
            else:
                issue("error", "event_unknown_bridge", f"{ev['id']}: bridge {bid!r} not in dataset")
    ds["observed_events"].sort(key=lambda e: e["id"])
    return unresolved


def _hospital_reference_check(ds: dict, ref: dict) -> dict:
    names = [fold(h["name"]) for h in ds["hospitals"] if h["name"]]
    matched, missing = [], []
    for item in ref.get("hospitals", []):
        key = fold(item["name"])
        hit = any(key in n or n in key for n in names)
        (matched if hit else missing).append(item["name"])
    return {"source": ref.get("source_url"), "matched": matched, "not_found_in_graph": missing}


def _report(ds, stats, issues, ref_counts, highway_counts, dropped, unatt_p, unatt_h, unresolved, ref_check, cfg) -> dict:
    segs = ds["segments"]
    adjacency: dict = defaultdict(set)
    for c in ds["connections"]:
        adjacency[c["from_place_id"]].add(c["to_place_id"])
        adjacency[c["to_place_id"]].add(c["from_place_id"])
    comp_sizes, seen = [], set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack, size = [start], 0
        seen.add(start)
        while stack:
            cur = stack.pop()
            size += 1
            for nb in adjacency[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        comp_sizes.append(size)
    counts = {"corridor": 1, "dataset": 1, "season": 1,
              "places_total": len(ds["places"]),
              "places_village": sum(p["type"] == "village" for p in ds["places"]),
              "places_town": sum(p["type"] == "town" for p in ds["places"]),
              "places_junction": sum(p["type"] == "junction" for p in ds["places"]),
              "hospitals": len(ds["hospitals"]), "bridges": len(ds["bridges"]), "segments": len(segs),
              "connections": len(ds["connections"]), "observed_events": len(ds["observed_events"])}
    counts["graph_nodes_total"] = (3 + counts["places_total"] + counts["hospitals"] + counts["bridges"] + counts["segments"]
                                   + counts["observed_events"])
    basis = Counter(s["travel_time_basis"] for s in segs)
    return {
        "counts": counts,
        "note_on_scope_target": "Brief expects roughly 150-200 nodes where sources allow; this is reported, never padded.",
        "ways_seen_by_ref_top": ref_counts.most_common(12), "selected_ways_by_highway_class": dict(highway_counts),
        "nh29": {"segments": sum(s["on_nh29"] for s in segs),
                 "length_km": round(sum(s["length_km"] for s in segs if s["on_nh29"]), 2)},
        "directionality": {"bidirectional_segments": sum(s["is_bidirectional"] for s in segs),
                           "one_way_segments": sum(not s["is_bidirectional"] for s in segs)},
        "travel_time_basis": dict(basis), "assumed_speeds_kmh": cfg.default_speeds_kmh if cfg.use_assumed_speeds else None,
        "skipped_or_dropped": dict(sorted(stats.items())), "components_without_nh29_dropped": dropped,
        "network_components_by_place_count": sorted(comp_sizes, reverse=True),
        "unattached_places": unatt_p, "unattached_hospitals": unatt_h,
        "missing_data": {"places_population_unknown": sum(p["population"] is None for p in ds["places"] if p["type"] != "junction"),
                         "hospitals_beds_unknown": sum(h["beds"] is None for h in ds["hospitals"]),
                         "hospitals_trauma_level_unknown": len(ds["hospitals"]),
                         "hospitals_unnamed": sum(not h["name"] for h in ds["hospitals"]),
                         "bridges_unnamed": sum(not b["name"] for b in ds["bridges"]),
                         "segments_missing_travel_time": sum(s["travel_time_min"] is None for s in segs),
                         "segments_without_monsoon_risk_label": len(segs)},
        "hospital_reference_check": ref_check, "events_places_unresolved": unresolved,
        "reference_places": ds["reference_places"],
        "assumptions": [
            "Settlements and hospitals are attached to the nearest road vertex within a distance threshold; access roads "
            "below that distance are not modelled. Distances are stored per record (snap_distance_m).",
            "Segments are stretches of road between junctions/attached features; a bridge sits ON every segment that carries it, "
            "so closing a bridge is modelled as closing those segments.",
            "Roads are bidirectional unless OSM tags them one-way; reversible/alternating values are treated as bidirectional.",
            "Travel time uses maxspeed tags where present, otherwise placeholder planning speeds per road class (assumed, not measured).",
            "No monsoon_risk labels, failure histories, bed counts or trauma levels are inferred. Observed events are attached "
            "to places only when a source names the place.",
            "NEAREST_HOSPITAL is not populated in Phase 1; it needs the routing of Phase 2 and is never authoritative.",
        ],
    }


# --------------------------------------------------------------------------- validation
def validate_dataset(ds: dict, today: date | None = None) -> list:
    """Structural and provenance checks. Returns issues; callers must refuse a dataset with errors."""
    today = today or date.today()
    out: list = []

    def err(code, msg, **kw):
        out.append({"severity": "error", "code": code, "message": msg, **kw})

    def warn(code, msg, **kw):
        out.append({"severity": "warning", "code": code, "message": msg, **kw})

    bbox = ds.get("meta", {}).get("bbox")
    ids: dict = {}
    for label in ("places", "hospitals", "bridges", "segments", "observed_events"):
        seen = Counter(r["id"] for r in ds.get(label, []))
        for rid, n in seen.items():
            if n > 1:
                err("duplicate_id", f"{label}: id {rid} appears {n} times")
        ids[label] = set(seen)

    for label in ("places", "hospitals", "bridges", "segments", "observed_events"):
        for r in ds.get(label, []):
            if r.get("confidence") not in CONFIDENCE_VALUES:
                err("bad_confidence", f"{r['id']}: confidence {r.get('confidence')!r}")
            if r.get("data_status") not in DATA_STATUS_VALUES:
                err("bad_data_status", f"{r['id']}: data_status {r.get('data_status')!r}")
            if label != "observed_events" and not r.get("source_url"):
                err("missing_source_url", f"{r['id']}: no source_url")
            if label not in ("observed_events",) and not r.get("retrieved_at"):
                err("missing_retrieved_at", f"{r['id']}: no retrieved_at")
            lv = r.get("last_verified")
            if lv and r.get("confidence") != "verified":
                err("last_verified_without_verification", f"{r['id']}: last_verified set but confidence is not 'verified'")
            if r.get("confidence") == "verified":
                try:
                    if not lv or date.fromisoformat(lv) > today:
                        raise ValueError
                except (TypeError, ValueError):
                    err("verified_without_valid_date", f"{r['id']}: 'verified' needs a real last_verified date")
            if r.get("monsoon_risk") not in (None, *MONSOON_RISK_VALUES):
                err("bad_monsoon_risk", f"{r['id']}: monsoon_risk {r['monsoon_risk']!r}")
            if label in ("places", "hospitals", "bridges"):
                lat, lon = r.get("lat"), r.get("lon")
                if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180):
                    err("bad_coordinates", f"{r['id']}: lat/lon {lat!r}/{lon!r}")
                elif bbox and not _in_bbox(lat, lon, bbox):
                    err("outside_bbox", f"{r['id']}: outside the corridor bounding box")
    for ev in ds.get("observed_events", []):
        if ev.get("data_status") != "observed":
            err("event_not_observed", f"{ev['id']}: seed events must be data_status 'observed' (simulated ones are created at runtime)")

    segs = {s["id"]: s for s in ds.get("segments", [])}
    dirs: dict = defaultdict(set)
    for c in ds.get("connections", []):
        s = segs.get(c.get("segment_id"))
        if s is None:
            err("connection_without_segment", f"connection {c.get('from_place_id')}->{c.get('to_place_id')} has no valid segment_id")
            continue
        if c["from_place_id"] not in ids["places"] or c["to_place_id"] not in ids["places"]:
            err("connection_unknown_place", f"connection on {s['id']} references an unknown place")
        ends = (s["from_place_id"], s["to_place_id"]) if c["direction"] == "forward" else (s["to_place_id"], s["from_place_id"])
        if (c["from_place_id"], c["to_place_id"]) != ends:
            err("connection_direction_mismatch", f"connection on {s['id']} does not match its segment endpoints/direction")
        if c["direction"] == "reverse" and not s["is_bidirectional"]:
            err("reverse_on_oneway", f"{s['id']} is one-way but has a reverse connection")
        if c.get("travel_time_min") != s.get("travel_time_min"):
            err("connection_time_mismatch", f"connection on {s['id']} disagrees with the segment travel time")
        dirs[s["id"]].add(c["direction"])
    for sid, s in segs.items():
        want = {"forward", "reverse"} if s["is_bidirectional"] else {"forward"}
        if dirs.get(sid) != want:
            err("segment_connections_incomplete", f"{sid}: expected connections {sorted(want)}, found {sorted(dirs.get(sid, []))}")
        tt = s.get("travel_time_min")
        if tt is not None and not (isinstance(tt, (int, float)) and math.isfinite(tt) and tt > 0):
            err("bad_travel_time", f"{sid}: travel_time_min {tt!r}")
        if not (isinstance(s.get("length_km"), (int, float)) and s["length_km"] > 0):
            err("bad_length", f"{sid}: length_km {s.get('length_km')!r}")
    missing_tt = [sid for sid, s in segs.items() if s.get("travel_time_min") is None]
    if missing_tt:
        warn("missing_travel_time", f"{len(missing_tt)} segment(s) have no travel time; routing must reject them")

    for b in ds.get("bridge_on", []):
        if b["bridge_id"] not in ids["bridges"] or b["segment_id"] not in segs:
            err("bridge_on_dangling", f"ON relationship {b} references a missing node")
    for b in ids["bridges"]:
        if not any(x["bridge_id"] == b for x in ds.get("bridge_on", [])):
            warn("bridge_without_segment", f"{b} is not ON any segment")
    for h in ds.get("hospital_located_at", []):
        if h["hospital_id"] not in ids["hospitals"] or h["place_id"] not in ids["places"]:
            err("located_at_dangling", f"LOCATED_AT {h} references a missing node")
    for h in ids["hospitals"]:
        if not any(x["hospital_id"] == h for x in ds.get("hospital_located_at", [])):
            err("hospital_unattached", f"{h} has no road attachment")
    for key, pid in (ds.get("reference_places") or {}).items():
        if pid is None:
            err("reference_place_missing", f"reference place {key!r} is not in the network (check the unattached list)")

    if not any(e.get("severity") == "error" for e in out):
        ref = [p for p in (ds.get("reference_places") or {}).values() if p]
        if len(ref) == 2:
            adj: dict = defaultdict(set)
            for c in ds["connections"]:
                adj[c["from_place_id"]].add(c["to_place_id"])
                adj[c["to_place_id"]].add(c["from_place_id"])
            seen, stack = {ref[0]}, [ref[0]]
            while stack:
                for nb in adj[stack.pop()]:
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            if ref[1] not in seen:
                warn("reference_places_disconnected", "Dimapur and Kohima are not connected in the extracted network "
                                                      "(undirected); the NH29 chain may be broken in this extract")
    return out
