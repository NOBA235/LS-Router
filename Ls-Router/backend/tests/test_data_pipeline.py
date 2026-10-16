"""Normalization tests on the synthetic fixture. Plain asserts; no database or network."""
import copy
import json
import math
from dataclasses import replace
from datetime import date

import osm_normalize as N
import synthetic_corridor as S
from pipeline_config import PipelineConfig

CFG = PipelineConfig(bbox=S.BBOX, reference_names={"dimapur": "Synthetic Dimapur", "kohima": "Synthetic Kohima"})
TODAY = date(2026, 10, 14)


def build(cfg=CFG, curated=None):
    return S.synthetic_dataset(curated=curated, cfg=cfg, today=TODAY)


def by_id(rows):
    return {r["id"]: r for r in rows}


def codes(issues):
    return {i["code"] for i in issues}


def test_topology_matches_hand_computed_expectation():
    ds = build()
    c = ds["report"]["counts"]
    assert (c["places_total"], c["segments"], c["connections"], c["bridges"], c["hospitals"]) == (8, 9, 17, 1, 1)
    pairs = {(s["from_place_id"], s["to_place_id"]) for s in ds["segments"]}
    assert pairs == {
        ("place-node-7001", "junction-9003"), ("junction-9003", "junction-9005"), ("junction-9003", "place-node-7004"),
        ("junction-9005", "junction-9006"), ("junction-9005", "junction-9012"), ("junction-9005", "place-node-7003"),
        ("junction-9006", "place-node-7002"), ("place-node-7004", "junction-9012"), ("junction-9012", "junction-9006"),
    }
    assert ds["report"]["validation"]["errors"] == []


def test_every_connection_names_an_existing_segment_with_matching_endpoints():
    ds = build()
    segs = by_id(ds["segments"])
    assert ds["connections"]
    for c in ds["connections"]:
        s = segs[c["segment_id"]]
        ends = (s["from_place_id"], s["to_place_id"]) if c["direction"] == "forward" else (s["to_place_id"], s["from_place_id"])
        assert (c["from_place_id"], c["to_place_id"]) == ends


def test_direction_is_respected():
    ds = build()
    oneway = [s for s in ds["segments"] if not s["is_bidirectional"]]
    assert [s["id"] for s in oneway] == ["seg-9012-9006-8007"]
    conns = [c for c in ds["connections"] if c["segment_id"] == "seg-9012-9006-8007"]
    assert len(conns) == 1 and (conns[0]["from_place_id"], conns[0]["to_place_id"]) == ("junction-9012", "junction-9006")
    two_way = [c for c in ds["connections"] if c["segment_id"] == "seg-9001-9003-8001"]
    assert {c["direction"] for c in two_way} == {"forward", "reverse"}


def test_lengths_and_times_come_from_geometry_and_documented_speeds():
    ds = build()
    s1 = by_id(ds["segments"])["seg-9001-9003-8001"]
    expected_km = 2 * N.haversine_km(10.0, 10.0, 10.0, 10.01)
    assert abs(s1["length_km"] - expected_km) < 0.001 and 2.17 < s1["length_km"] < 2.21
    assert s1["travel_time_basis"] == "assumed" and abs(s1["travel_time_min"] - s1["length_km"] / 35 * 60) < 0.01
    s5 = by_id(ds["segments"])["seg-9003-9011-8005"]
    assert s5["travel_time_basis"] == "tagged" and abs(s5["travel_time_min"] - s5["length_km"] / 40 * 60) < 0.01
    for s in ds["segments"]:
        assert math.isfinite(s["travel_time_min"]) and s["travel_time_min"] > 0


def test_bridge_sits_on_the_segment_that_carries_it():
    ds = build()
    assert ds["bridge_on"] == [{"bridge_id": "bridge-way-8003", "segment_id": "seg-9003-9005-8002"}]
    b = ds["bridges"][0]
    # the bridge way only carries its road's name, so that is NOT reported as the bridge's name
    assert b["name"] is None and b["road_name"] == "Synthetic Highway" and b["monsoon_risk"] is None


def test_bridge_name_comes_from_bridge_name_tag():
    roads, feats, meta = S.build()
    next(w for w in roads["elements"] if w.get("id") == 8003)["tags"]["bridge:name"] = "Synthetic Bridge"
    ds = N.normalize(roads, feats, meta, CFG, today=TODAY, dataset_kind="synthetic-test")
    assert ds["bridges"][0]["name"] == "Synthetic Bridge" and ds["bridges"][0]["road_name"] == "Synthetic Highway"


def test_nh29_is_identified_by_ref_not_by_highway_class():
    ds = build()
    nh = {s["id"] for s in ds["segments"] if s["on_nh29"]}
    assert len(nh) == 4 and all(by_id(ds["segments"])[i]["ref"] == "NH 29" for i in nh)
    assert ds["report"]["nh29"]["segments"] == 4
    comps = ds["report"]["components_without_nh29_dropped"]
    assert len(comps) == 1 and comps[0]["refs"] == ["SH 1"]
    assert not any("8008" in s["source_id"] for s in ds["segments"])


def test_skips_and_dedupes_are_counted_not_hidden():
    sk = build()["report"]["skipped_or_dropped"]
    assert sk["edges_duplicate_skipped"] == 1 and sk["edges_clipped_outside_bbox"] == 1 and sk["edges_missing_nodes"] == 1
    assert sk["ways_access_restricted_skipped"] == 1
    assert sk["places_unnamed_skipped"] == 1 and sk["places_other_tag_skipped"] == 1 and sk["places_duplicates_skipped"] == 1
    assert sk["hospitals_duplicates_skipped"] == 1


def test_unattached_features_are_reported_and_left_out_of_the_graph():
    ds = build()
    rep = ds["report"]
    assert [u["name"] for u in rep["unattached_places"]] == ["Synthetic Faraway"]
    assert len(rep["unattached_hospitals"]) == 1 and rep["unattached_hospitals"][0]["name"] is None
    assert "Synthetic Faraway" not in {p["name"] for p in ds["places"]}
    assert all(p["snap_distance_m"] is not None for p in ds["places"] if p["type"] != "junction")


def test_reference_places_and_hospital_attachment():
    ds = build()
    assert ds["reference_places"] == {"dimapur": "place-node-7001", "kohima": "place-node-7002"}
    h = ds["hospitals"][0]
    assert h["name"] == "Synthetic Hospital" and h["beds"] == 120 and "trauma_level" not in h
    assert ds["hospital_located_at"][0]["place_id"] == "place-node-7002"


def test_unknowns_are_not_invented():
    ds = build()
    pops = {p["name"]: p["population"] for p in ds["places"] if p["type"] != "junction"}
    assert pops["Synthetic Dimapur"] == 123456 and pops["Synthetic Village A"] is None
    for label in ("places", "hospitals", "bridges", "segments"):
        for r in ds[label]:
            assert r["confidence"] == "unverified" and r["last_verified"] is None
            assert r["source_url"].startswith("https://www.openstreetmap.org/")
            assert r["retrieved_at"] == "2000-01-01T00:00:00Z"
    assert all(s["monsoon_risk"] is None for s in ds["segments"])
    assert ds["observed_events"] == []


def test_missing_speeds_leave_travel_time_missing_when_assumptions_are_off():
    ds = build(replace(CFG, use_assumed_speeds=False))
    segs = by_id(ds["segments"])
    assert segs["seg-9001-9003-8001"]["travel_time_min"] is None and segs["seg-9001-9003-8001"]["travel_time_basis"] == "missing"
    assert segs["seg-9003-9011-8005"]["travel_time_min"] is not None
    assert "missing_travel_time" in codes(ds["report"]["validation"]["warnings"])
    assert all(c["travel_time_min"] is None for c in ds["connections"] if c["segment_id"] == "seg-9001-9003-8001")


def test_missing_nh29_gives_an_actionable_error():
    try:
        build(replace(CFG, nh_ref_regex=r"^NH 99$"))
    except N.NormalizationError as exc:
        assert "NH 29" in str(exc) and "--nh-ref-regex" in str(exc)
    else:
        raise AssertionError("expected NormalizationError")


def test_output_is_deterministic():
    a, b = json.dumps(build(), sort_keys=True), json.dumps(build(), sort_keys=True)
    assert a == b


def test_validation_catches_broken_datasets():
    base = build()
    cases = {
        "connection_without_segment": lambda d: d["connections"][0].update(segment_id="nope"),
        "bad_travel_time": lambda d: d["segments"][0].update(travel_time_min=float("nan")),
        "verified_without_valid_date": lambda d: d["places"][0].update(confidence="verified"),
        "last_verified_without_verification": lambda d: d["places"][0].update(last_verified="2026-01-01"),
        "outside_bbox": lambda d: d["places"][0].update(lat=50.0),
        "duplicate_id": lambda d: d["places"].append(copy.deepcopy(d["places"][0])),
        "bad_data_status": lambda d: d["places"][0].update(data_status="made-up"),
        "reverse_on_oneway": lambda d: d["connections"].append(
            {"from_place_id": "junction-9006", "to_place_id": "junction-9012", "segment_id": "seg-9012-9006-8007",
             "travel_time_min": by_id(d["segments"])["seg-9012-9006-8007"]["travel_time_min"], "direction": "reverse"}),
        "segment_connections_incomplete": lambda d: d["connections"].pop(),
        "event_not_observed": lambda d: d["observed_events"].append({"id": "sim-1", "data_status": "simulated", "confidence": "unverified"}),
        "reference_place_missing": lambda d: d["reference_places"].update(dimapur=None),
    }
    for code, mutate in cases.items():
        ds = copy.deepcopy(base)
        mutate(ds)
        assert code in codes(N.validate_dataset(ds, TODAY)), code


def test_verification_overlay_requires_a_date_and_a_source():
    ok = {"verification": {"entries": [{"id": "hospital-node-7101", "confidence": "verified", "last_verified": "2026-10-01",
                                         "verification_source_url": "https://example.org/x", "set": {"beds": 150}}]}}
    h = build(curated=ok)["hospitals"][0]
    assert h["confidence"] == "verified" and h["last_verified"] == "2026-10-01" and h["beds"] == 150
    bad = [{"id": "hospital-node-7101", "confidence": "verified", "set": {}},
           {"id": "hospital-node-7101", "confidence": "verified", "last_verified": "2999-01-01", "verification_source_url": "https://e.org"},
           {"id": "hospital-node-7101", "confidence": "unverified", "last_verified": "2026-10-01"},
           {"id": "hospital-node-7101", "confidence": "verified", "last_verified": "2026-10-01", "verification_source_url": "https://e.org",
            "set": {"trauma_level": 1, "lat": 5}}]
    for entry in bad:
        ds = build(curated={"verification": {"entries": [entry]}})
        assert ds["hospitals"][0]["confidence"] == "unverified" and ds["hospitals"][0]["last_verified"] is None
        assert ds["report"]["validation"]["errors"]  # rejected with an error, not silently applied
    unknown = build(curated={"verification": {"entries": [{"id": "nope", "confidence": "unverified"}]}})
    assert "verification_unresolved" in codes(unknown["report"]["validation"]["warnings"])


def test_observed_events_stay_observed_and_link_only_to_named_places():
    ev = {"id": "obs-test", "description": "d", "cause": "landslide", "started_at": "2000-01", "place_names": ["Synthetic Village A", "Nowhere"],
          "sources": [{"url": "https://example.org/a"}, {"url": "https://example.org/b"}]}
    ds = build(curated={"risk_events": {"events": [ev]}})
    e = ds["observed_events"][0]
    assert e["data_status"] == "observed" and e["confidence"] == "unverified" and e["lat"] is None
    assert e["source_urls"] == ["https://example.org/a", "https://example.org/b"]
    assert [(r["event_id"], r["place_id"]) for r in ds["event_near_place"]] == [("obs-test", "place-node-7003")]
    assert ds["report"]["events_places_unresolved"] == [{"event_id": "obs-test", "place_name": "Nowhere", "matches": 0}]
    assert ds["event_blocks"] == [] and ds["event_failed_bridges"] == []
    bad = dict(ev, segment_ids=["seg-does-not-exist"])
    assert "event_unknown_segment" in codes(build(curated={"risk_events": {"events": [bad]}})["report"]["validation"]["errors"])


def test_helpers():
    assert N.parse_maxspeed_kmh("40") == 40 and abs(N.parse_maxspeed_kmh("25 mph") - 40.2336) < 1e-3
    assert N.parse_maxspeed_kmh("none") is None and N.parse_maxspeed_kmh("signals") is None and N.parse_maxspeed_kmh("0") is None
    assert N.oneway_flags({}) == (True, True) and N.oneway_flags({"oneway": "yes"}) == (True, False)
    assert N.oneway_flags({"oneway": "-1"}) == (False, True) and N.oneway_flags({"junction": "roundabout"}) == (True, False)
    assert N.fold("Chümoukedima") == "chumoukedima" == N.fold("  CHUMOUKEDIMA ")
