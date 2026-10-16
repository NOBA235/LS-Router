"""Tests for Phase 3 natural-language event parsing and corridor location resolution."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import scenarios as SC  # noqa: E402
import synthetic_corridor as SY  # noqa: E402
from app.agent.resolver import LocationResolver, parse_event  # noqa: E402
from app.graph import queries as Q  # noqa: E402
from app.graph.errors import ToolInputError  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402
from fake_graph import FakeGraph  # noqa: E402


def make_resolver(ds=None) -> tuple[LocationResolver, FakeGraph]:
    fg = FakeGraph(ds or SC.scenario_a())
    return LocationResolver(FalkorStore(fg)), fg


def test_parse_event_extracts_cause_road_km_coords_month_and_intent():
    p1 = parse_event("Landslide near NH29 kilometre 42.")
    assert p1.cause == "landslide"
    assert p1.intent == "closure_impact"
    assert p1.road_refs == ["NH 29"]
    assert p1.km_marker == 42.0
    assert p1.bridge_mentioned is False

    p2 = parse_event("Bridge washout on NH-29 at chainage km 163+980 in August")
    assert p2.cause == "flood"
    assert p2.road_refs == ["NH 29"]
    assert p2.km_marker == 163.98
    assert p2.bridge_mentioned is True
    assert p2.month == 8

    p3 = parse_event("Rockfall at 25.7812, 93.9145 between Dimapur and Kohima")
    assert p3.cause == "landslide"
    assert p3.coordinates == {"lat": 25.7812, "lon": 93.9145}
    assert p3.between_pair == ["Dimapur", "Kohima"]

    p4 = parse_event("Clear closure sim-e1 and audit")
    assert p4.intent == "clear_closure"
    assert p4.explicit_event_ids == ["sim-e1"]

    p5 = parse_event("What are the weak links in the corridor?")
    assert p5.intent == "weak_links"

    p6 = parse_event("Show seasonal monsoon risk for July")
    assert p6.intent == "seasonal_risk"
    assert p6.month == 7

    p7 = parse_event("Fastest route from Dimapur to Echo")
    assert p7.intent == "reroute"
    assert p7.between_pair == ["Dimapur", "Echo"]


def test_km_marker_resolution_surfaces_ambiguous_candidates_and_warns():
    resolver, fg = make_resolver()
    env = resolver.resolve_location("Landslide near NH29 kilometre 42.")
    res = env["result"]
    assert env["tool"] == "resolve_location"
    assert res["status"] == "ambiguous"
    assert res["requires_clarification"] is True
    assert res["selected_target"] is None
    assert [c["segment_id"] for c in res["candidate_segments"]] == ["s3", "s2", "s1"]
    assert res["candidate_segments"][0]["cumulative_km_from_dimapur"] == [15.0, 20.0]
    assert res["candidate_segments"][1]["cumulative_km_from_dimapur"] == [5.0, 15.0]
    assert res["candidate_segments"][2]["cumulative_km_from_dimapur"] == [0.0, 5.0]
    assert any("verified highway milestone" in w for w in env["warnings"])
    assert any("exceeds the total modelled corridor length" in w for w in env["warnings"])
    assert env["cypher"] == [q for q, _ in fg.log]
    assert set(env["cypher"]) == {q for _, q in Q.SNAPSHOT_QUERIES}

    # A km marker inside the corridor (km 12 lies inside s2: 5..15 km) ranks s2 #1, but still requires confirmation
    env12 = resolver.resolve_location("Landslide near NH29 kilometre 12")
    r12 = env12["result"]
    assert r12["status"] == "ambiguous" and r12["requires_clarification"] is True
    assert r12["candidate_segments"][0]["segment_id"] == "s2"
    assert "contains km 12" in r12["candidate_segments"][0]["match_reasons"][0]


def test_coordinate_only_search_never_silently_snaps():
    resolver, _ = make_resolver()
    env = resolver.resolve_location("Landslide reported at coordinates", lat=10.0, lon=10.035)
    res = env["result"]
    assert res["status"] == "ambiguous"
    assert res["requires_clarification"] is True
    assert res["selected_target"] is None
    assert res["candidate_segments"]
    assert "distance_m" in res["candidate_segments"][0]
    assert any("proximity alone is not sufficient" in w.lower() for w in env["warnings"])

    with pytest.raises(ToolInputError):
        resolver.resolve_location("Bad coords", lat=95.0, lon=10.0)


def test_ambiguous_vs_unambiguous_place_and_endpoint_matches():
    resolver, _ = make_resolver()

    # Bravo has 2 incident segments (s4 and s5) -> ambiguous
    b_env = resolver.resolve_location("Landslide near Bravo")
    assert b_env["result"]["status"] == "ambiguous"
    assert [c["segment_id"] for c in b_env["result"]["candidate_segments"]] == ["s4", "s5"]

    # J2 has 4 incident segments (s2, s3, s5, s6) -> ambiguous
    j2_env = resolver.resolve_location("Landslide at J2")
    assert j2_env["result"]["status"] == "ambiguous"
    assert [c["segment_id"] for c in j2_env["result"]["candidate_segments"]] == ["s2", "s3", "s5", "s6"]

    # Alpha is at the end of spur s6 (only 1 incident segment) -> resolved unambiguously
    a_env = resolver.resolve_location("Landslide near Alpha")
    assert a_env["result"]["status"] == "resolved"
    assert a_env["result"]["selected_target"]["segment_id"] == "s6"

    # Direct endpoint pair J1 and J2 on NH29 -> resolved unambiguously to s2
    pair_env = resolver.resolve_location("Landslide on NH29 between J1 and J2")
    assert pair_env["result"]["status"] == "resolved"
    assert pair_env["result"]["selected_target"]["segment_id"] == "s2"

    # Multi-segment stretch Dimapur to Kohima -> ambiguous (s1, s2, s3)
    dk_env = resolver.resolve_location("Landslide between Dimapur and Kohima")
    assert dk_env["result"]["status"] == "ambiguous"
    assert [c["segment_id"] for c in dk_env["result"]["candidate_segments"]] == ["s1", "s2", "s3"]

    # Bridge collapse between Dimapur and Kohima -> only s2 on that path carries bridge BR1 -> resolved
    br_dk_env = resolver.resolve_location("Bridge collapse between Dimapur and Kohima")
    assert br_dk_env["result"]["status"] == "resolved"
    assert br_dk_env["result"]["selected_target"]["segment_id"] == "s2"
    assert br_dk_env["result"]["selected_target"]["bridge_id"] == "BR1"


def test_named_bridge_and_explicit_target_confirmation():
    resolver, _ = make_resolver()

    # Named bridge "Bridge One"
    b_env = resolver.resolve_location("Washout at Bridge One")
    assert b_env["result"]["status"] == "resolved"
    assert b_env["result"]["selected_target"]["segment_id"] == "s2"
    assert b_env["result"]["selected_target"]["bridge_id"] == "BR1"

    # Explicit confirmation of an ambiguous query
    c_env = resolver.resolve_location("Landslide near NH29 kilometre 42.", target_segment_id="s2", target_bridge_id="BR1")
    assert c_env["result"]["status"] == "resolved"
    assert c_env["result"]["selected_target"]["segment_id"] == "s2"
    assert c_env["result"]["selected_target"]["bridge_id"] == "BR1"

    # Invalid explicit targets raise ToolInputError
    with pytest.raises(ToolInputError):
        resolver.resolve_location("Test", target_segment_id="nonexistent")
    with pytest.raises(ToolInputError):
        resolver.resolve_location("Test", target_segment_id="s6", target_bridge_id="BR1")


def test_normalized_synthetic_osm_corridor_and_no_match():
    ds = SY.synthetic_dataset()
    resolver, _ = make_resolver(ds)

    # Unique road name "Synthetic Spur" resolves to the single spur segment
    spur_env = resolver.resolve_location("Landslide on Synthetic Spur")
    assert spur_env["result"]["status"] == "resolved"
    assert spur_env["result"]["selected_target"]["segment_name"] == "Synthetic Spur"

    # Shared road name "Synthetic Highway" spans multiple segments -> ambiguous
    hw_env = resolver.resolve_location("Landslide on Synthetic Highway")
    assert hw_env["result"]["status"] == "ambiguous"
    assert len(hw_env["result"]["candidate_segments"]) == 4

    # Unknown location returns no_match
    none_env = resolver.resolve_location("Landslide near Atlantis")
    assert none_env["result"]["status"] == "no_match"
    assert none_env["result"]["candidate_segments"] == []
