"""Small hand-checkable datasets in the Phase 1 schema (the shape seed.py loads and FakeGraph serves).
Every expected value in tool_checks.py is derived from these topologies by hand, not from the code under test."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.graph.fingerprint import network_fingerprint, segment_fingerprint  # noqa: E402

PROV = {"confidence": "unverified", "retrieved_at": "2000-01-01T00:00:00Z", "last_verified": None}


def P(pid, type="junction", name=None, ref=None):
    return {"id": pid, "type": type, "name": name, "reference_key": ref}


def S(sid, a, b, minutes, km=1.0, two_way=True, risk=None, nh29=False, minutes_conn=None):
    return {"id": sid, "a": a, "b": b, "minutes": minutes, "km": km, "two_way": two_way, "risk": risk, "nh29": nh29,
            "minutes_conn": minutes_conn}


def make_dataset(places, segments, hospitals=(), bridges=(), seasons=(), events=(), failed_in=(), fingerprint=True,
                 corridor="corr-test", extra_connections=(), kind="synthetic-test"):
    pl = [{**PROV, "id": p["id"], "name": p["name"], "type": p["type"], "lat": 10.0, "lon": 10.0, "population": None,
           "source_url": f"https://example.test/place/{p['id']}", "source_id": f"place/{p['id']}", "data_status": "observed",
           "reference_key": p["reference_key"], "origin": "synthetic"} for p in places]
    segs, conns = [], []
    for s in segments:
        rec = {**PROV, "id": s["id"], "name": s.get("name"), "ref": "NH 29" if s["nh29"] else None, "highway": "trunk",
               "length_km": s["km"], "travel_time_min": s["minutes"], "travel_time_basis": "assumed", "surface": None,
               "monsoon_risk": s["risk"], "source_url": f"https://example.test/seg/{s['id']}", "source_id": f"seg/{s['id']}",
               "data_status": "derived", "is_bidirectional": s["two_way"], "on_nh29": s["nh29"], "way_ids": [],
               "from_place_id": s["a"], "to_place_id": s["b"], "geometry": "[]"}
        t = s["minutes"] if s["minutes_conn"] is None else s["minutes_conn"]
        conns.append({"from_place_id": s["a"], "to_place_id": s["b"], "segment_id": s["id"], "travel_time_min": t, "direction": "forward"})
        if s["two_way"]:
            conns.append({"from_place_id": s["b"], "to_place_id": s["a"], "segment_id": s["id"], "travel_time_min": t, "direction": "reverse"})
        rec["baseline_fp"] = segment_fingerprint(rec)
        segs.append(rec)
    conns += list(extra_connections)
    hs = [{**PROV, "id": h[0], "name": h[1], "lat": 10.0, "lon": 10.0, "beds": h[3] if len(h) > 3 else None,
           "source_url": f"https://example.test/hosp/{h[0]}", "source_id": f"hosp/{h[0]}", "data_status": "observed", "origin": "synthetic"}
          for h in hospitals]
    located = [{"hospital_id": h[0], "place_id": h[2], "snap_distance_m": 10.0, "method": "nearest_road_vertex", "data_status": "derived"}
               for h in hospitals if h[2]]
    brs = [{**PROV, "id": b[0], "name": b[1], "road_name": None, "lat": 10.0, "lon": 10.0, "monsoon_risk": b[3] if len(b) > 3 else None,
            "source_url": f"https://example.test/bridge/{b[0]}", "source_id": f"bridge/{b[0]}", "data_status": "observed",
            "bridge_tag": "yes", "length_m": 20.0, "origin": "synthetic"} for b in bridges]
    bridge_on = [{"bridge_id": b[0], "segment_id": sid} for b in bridges for sid in b[2]]
    season_nodes = [{"id": s[0], "name": s[1], "start_month": s[2], "end_month": s[3], "data_status": "derived", "confidence": "unverified",
                     "basis": "test"} for s in seasons]
    season_affects = [{"season_id": s[0], "segment_id": sid, "basis": "assumed_all_segments_exposed"} for s in seasons for sid in s[4]]
    evs, blocks, near, failed = [], [], [], []
    for e in events:
        evs.append({"id": e["id"], "description": e.get("description", "observed test event"), "lat": None, "lon": None,
                    "started_at": e.get("started_at"), "ended_at": None, "cause": e.get("cause", "landslide"), "status": "historical",
                    "data_status": "observed", "confidence": "unverified", "last_verified": None, "date_precision": "month",
                    "location_text": None, "source_url": "https://example.test/news", "source_urls": ["https://example.test/news"],
                    "origin": "curated_report"})
        blocks += [{"event_id": e["id"], "target_id": sid, "target_label": "RoadSegment"} for sid in e.get("segments", [])]
        near += [{"event_id": e["id"], "place_id": pid, "basis": "named_in_source", "data_status": "derived"} for pid in e.get("near", [])]
    failed = [{"bridge_id": b, "event_id": e, "year": y} for b, e, y in failed_in]
    ds = {"schema_version": 1, "meta": {"kind": kind, "source": {"retrieved_at": PROV["retrieved_at"]}, "bbox": None},
          "corridor": {"id": corridor, "name": "Test corridor"},
          "datasets": [{"id": "dataset-test", "kind": kind, "name": "synthetic", "retrieved_at": PROV["retrieved_at"], "bbox": "", "generator": "tests",
                        "schema_version": 1, **({"network_fingerprint": network_fingerprint(segs, conns)} if fingerprint else {})}],
          "seasons": season_nodes, "places": pl, "hospitals": hs, "bridges": brs, "segments": segs, "connections": conns,
          "bridge_on": bridge_on, "hospital_located_at": located, "season_affects": season_affects, "observed_events": evs,
          "event_near_place": near, "event_blocks": blocks, "event_failed_bridges": failed,
          "reference_places": {p["reference_key"]: p["id"] for p in places if p["reference_key"]}}
    if not fingerprint:
        for s in ds["segments"]:
            del s["baseline_fp"]
    return ds


# ----------------------------------------------------------------------------------------------------------
def scenario_a(**kw):
    """The main scenario. Minutes in brackets; all two-way unless stated.

        D(Dimapur) -s1[10]- J1 -s2[20, bridge BR1]- J2 -s3[10]- K(Kohima) -s7[8]- C --s8[4, ONE-WAY C->E]--> E
                             \\                       /  \\
                              s4[15]- B -s5[25]------      s6[5]- A (spur)        X -s9[3]- Y   (island, never reachable)
    Hospitals: HJ@J1, HK@K, HE@E, HY@Y (island), HN has no road attachment.
    Baseline times from D: J1 10, B 25, J2 30, A 35, K 40, C 48, E 52.
    """
    places = [P("D", "town", "Dimapur", "dimapur"), P("J1"), P("J2"), P("K", "town", "Kohima", "kohima"),
              P("A", "village", "Alpha"), P("B", "village", "Bravo"), P("C", "village", "Charlie"), P("E", "village", "Echo"),
              P("X", "village", "Xray"), P("Y", "village", "Yankee")]
    segments = [S("s1", "D", "J1", 10, 5, nh29=True), S("s2", "J1", "J2", 20, 10, nh29=True), S("s3", "J2", "K", 10, 5, nh29=True),
                S("s4", "J1", "B", 15, 8), S("s5", "B", "J2", 25, 12), S("s6", "J2", "A", 5, 2), S("s7", "K", "C", 8, 4),
                S("s8", "C", "E", 4, 2, two_way=False), S("s9", "X", "Y", 3, 1)]
    hospitals = [("HJ", "Hosp J", "J1", 100), ("HK", "Hosp K", "K"), ("HE", "Hosp E", "E"), ("HY", "Hosp Y", "Y"), ("HN", "Hosp N", None)]
    bridges = [("BR1", "Bridge One", ["s2"])]
    seasons = [("monsoon", "Monsoon", 6, 9, [s["id"] for s in segments])]
    return make_dataset(places, segments, hospitals, bridges, seasons, **kw)
