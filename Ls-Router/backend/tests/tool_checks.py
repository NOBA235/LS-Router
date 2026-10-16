"""Scenario checks for the eleven tools. Each takes `make(dataset, clock=...)` and returns a world with `.tb` (Toolbox) and `.fake`
(FakeGraph, or None against a live FalkorDB). Expected values are derived by hand from the topologies in scenarios.py.
Run against the in-memory graph by test_tools_fake.py and against real FalkorDB by test_tools_falkordb.py."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import scenarios as SC
from app.graph import queries as Q
from app.graph.errors import ToolInputError, ToolStateError

SPEC_KEYS = {"result", "cypher", "params", "explanation", "graph", "warnings", "algorithm"}


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}{a}")


def ids(rows, key="id"):
    return [r[key] for r in rows]


def vals(d):
    return d["result"]


# 1 ---------------------------------------------------------------------------------------------------------
def check_closure_creation(make):
    w = make(SC.scenario_a())
    env = w.tb.apply_closure("s6", "Landslide on the Alpha spur")
    r = vals(env)
    assert r["created"] is True and r["event"]["id"] == "sim-e1"
    assert r["event"]["data_status"] == "simulated" and r["event"]["status"] == "active" and r["event"]["blocks_segments"] == ["s6"]
    assert r["state_change"]["newly_blocked_segments"] == ["s6"] and r["state_change"]["blocked_segments_after"] == ["s6"]
    assert {(c["from"], c["to"]) for c in r["state_change"]["connections_now_disabled"]} == {("J2", "A"), ("A", "J2")}
    assert any("not hand-verified" in x for x in env["warnings"]), "unverified location must be flagged"
    if w.fake:
        assert w.fake.events["sim-e1"]["data_status"] == "simulated" and ("sim-e1", "s6", "seg") in w.fake.blocks
    again = w.tb.apply_closure("s6", "Landslide on the Alpha spur")  # repeated request is safe
    assert vals(again)["created"] is False and vals(again)["event"]["id"] == "sim-e1"
    assert any("already exists" in x for x in again["warnings"])
    sim = [e for e in w.fake.events.values() if e["data_status"] == "simulated"] if w.fake else None
    assert sim is None or len(sim) == 1
    # bridge closure blocks the bridge and, through it, its segment
    b = vals(w.tb.apply_closure("s2", "Bridge out", bridge_id="BR1"))
    assert b["event"]["blocks_bridges"] == ["BR1"] and b["event"]["blocks_segments"] == ["s2"]
    # validation
    raises(ToolInputError, w.tb.apply_closure, "nope", "x")
    raises(ToolInputError, w.tb.apply_closure, "s6", "   ")
    raises(ToolInputError, w.tb.apply_closure, "s6", "x" * 501)
    raises(ToolInputError, w.tb.apply_closure, "s6", "x", bridge_id="BR1")      # BR1 is not ON s6
    raises(ToolInputError, w.tb.apply_closure, "s6", "x", bridge_id="ghost")
    if w.fake:
        w.fake.no_corridor.add("s9")
        raises(ToolInputError, w.tb.apply_closure, "s9", "outside corridor")


# 2 ---------------------------------------------------------------------------------------------------------
def check_multiple_closures_same_segment(make):
    tb = make(SC.scenario_a()).tb
    e1 = vals(tb.apply_closure("s2", "first report"))["event"]["id"]
    second = tb.apply_closure("s2", "second report")
    e2 = vals(second)["event"]["id"]
    assert e1 != e2 and vals(second)["state_change"]["already_blocked_by_other_events"] == {"s2": [e1]}
    r = vals(tb.reroute("D", "K"))
    assert r["blocked_segments"] == {"s2": [e1, e2]} and r["route"]["nodes"] == ["D", "J1", "B", "J2", "K"]
    assert r["route"]["total_travel_time_min"] == 60 and r["baseline_route"]["total_travel_time_min"] == 40
    c1 = vals(tb.clear_closure(e1))
    assert c1["restored_segments"] == [] and c1["still_blocked_by_other_closures"] == {"s2": [e2]}
    assert vals(tb.reroute("D", "K"))["route"]["total_travel_time_min"] == 60, "road must stay closed while e2 is active"
    c2 = vals(tb.clear_closure(e2))
    assert c2["restored_segments"] == ["s2"] and c2["still_blocked_by_other_closures"] == {}
    assert vals(tb.reroute("D", "K"))["route"]["total_travel_time_min"] == 40


# 3 ---------------------------------------------------------------------------------------------------------
def check_closure_resolution(make):
    w = make(SC.scenario_a(events=[{"id": "obs1", "started_at": "2024-08", "segments": ["s3"]}]))
    tb = w.tb
    e = vals(tb.apply_closure("s6", "spur"))["event"]["id"]
    env = tb.clear_closure(e)
    r = vals(env)
    assert r["state_transition"]["from"] == "active" and r["state_transition"]["to"] == "resolved" and r["state_transition"]["ended_at"]
    assert r["event"]["status"] == "resolved" and r["restored_segments"] == ["s6"] and r["blocked_segments_after"] == []
    assert r["history_preserved"]["event_node_kept"] and r["history_preserved"]["blocks_links_kept"] == 1
    if w.fake:
        assert w.fake.events[e]["status"] == "resolved" and (e, "s6", "seg") in w.fake.blocks, "history must be kept"
    again = tb.clear_closure(e)
    assert vals(again)["state_transition"] is None and any("already resolved" in x for x in again["warnings"])
    raises(ToolInputError, tb.clear_closure, "ghost")
    raises(ToolStateError, tb.clear_closure, "obs1")  # an observed record is not a closure


# 4 ---------------------------------------------------------------------------------------------------------
def check_connectivity_changes(make):
    tb = make(SC.scenario_a()).tb
    e6 = vals(tb.apply_closure("s6", "spur"))["event"]["id"]
    r = vals(tb.find_cut_off_places(e6))
    assert ids(r["newly_unreachable"]["villages"]) == ["A"] and r["newly_unreachable"]["count"] == 1
    assert r["newly_unreachable"]["towns"] == [] and r["newly_unreachable"]["junctions"] == []
    assert ids(r["already_unreachable_in_baseline"]) == ["X", "Y"], "pre-existing islands are not 'newly cut off'"
    assert r["reachability"]["baseline_reachable_places"] == 8 and r["reachability"]["reachable_with_active_closures"] == 7
    ev = r["newly_unreachable"]["villages"][0]["evidence"]
    assert ev["baseline_path_nodes"] == ["D", "J1", "J2", "A"] and ev["baseline_path_segments"] == ["s1", "s2", "s6"]
    assert ev["baseline_travel_time_min"] == 35 and ev["blocked_segments_on_path"] == ["s6"]
    assert [(b["segment_id"], b["from"], b["to"]) for b in r["cut_boundary"]] == [("s6", "J2", "A")]
    assert r["hospitals_newly_unreachable_from_dimapur"] == []
    # two closures that only matter together
    tb2 = make(SC.scenario_a()).tb
    a = vals(tb2.apply_closure("s2", "main"))["event"]["id"]
    b = vals(tb2.apply_closure("s4", "detour"))["event"]["id"]
    r2 = vals(tb2.find_cut_off_places(a))
    assert ids(r2["newly_unreachable"]["villages"]) == ["A", "B", "C", "E"] and ids(r2["newly_unreachable"]["towns"]) == ["K"]
    assert ids(r2["newly_unreachable"]["junctions"]) == ["J2"]
    assert sorted(h["id"] for h in r2["hospitals_newly_unreachable_from_dimapur"]) == ["HE", "HK"]
    assert r2["attributable_to_this_event"] == ["A", "B", "C", "E", "J2", "K"] and r2["also_cut_by_other_closures"] == []
    assert vals(tb2.find_cut_off_places(b))["attributable_to_this_event"] == ["A", "B", "C", "E", "J2", "K"]
    # add the spur closure: A is cut whatever happens to s2, so it is not attributable to s2
    tb2.apply_closure("s6", "spur too")
    r3 = vals(tb2.find_cut_off_places(a))
    assert r3["attributable_to_this_event"] == ["B", "C", "E", "J2", "K"] and r3["also_cut_by_other_closures"] == ["A"]
    # Kohima as the secondary reference
    tb4 = make(SC.scenario_a()).tb
    e3 = vals(tb4.apply_closure("s3", "J2-K"))["event"]["id"]
    k = vals(tb4.find_cut_off_places(e3))["from_kohima"]
    assert k["baseline_reachable"] == 8 and k["newly_unreachable"] == ["A", "B", "D", "J1", "J2"]
    # a resolved event blocks nothing and is not blamed
    tb4.clear_closure(e3)
    done = tb4.find_cut_off_places(e3)
    assert vals(done)["newly_unreachable"]["count"] == 0 and any("not active" in x for x in done["warnings"])


# 5 ---------------------------------------------------------------------------------------------------------
def check_directed_reachability(make):
    tb = make(SC.scenario_a()).tb
    fwd = vals(tb.reroute("D", "E"))
    assert fwd["route"]["nodes"] == ["D", "J1", "J2", "K", "C", "E"] and fwd["route"]["total_travel_time_min"] == 52
    back = vals(tb.reroute("E", "D"))   # s8 is one-way C->E, so E has no way out
    assert back["status"] == "no_route" and back["route"] is None
    assert vals(tb.reroute("E", "C"))["status"] == "no_route"
    assert vals(tb.reroute("C", "E"))["route"]["total_travel_time_min"] == 4
    e = vals(tb.apply_closure("s8", "one-way link"))["event"]["id"]
    r = vals(tb.find_cut_off_places(e))
    assert ids(r["newly_unreachable"]["villages"]) == ["E"]


# 6 ---------------------------------------------------------------------------------------------------------
def check_component_correctness(make):
    tb = make(SC.scenario_a()).tb
    e5 = vals(tb.apply_closure("s5", "alternate only"))["event"]["id"]
    c = vals(tb.find_cut_off_places(e5))["component_analysis"]
    assert c["baseline_component_count"] == 2 and c["component_count_with_closures"] == 2 and c["new_components"] == []
    assert "undirected" in c["representation"]
    e6 = vals(tb.apply_closure("s6", "spur"))["event"]["id"]
    c = vals(tb.find_cut_off_places(e6))["component_analysis"]
    assert c["baseline_component_count"] == 2 and c["component_count_with_closures"] == 3
    assert c["new_components"] == [["B", "C", "D", "E", "J1", "J2", "K"], ["A"]], "both halves of the split are new components"
    assert c["split_off_from_origin_component"] == [["A"]]
    # undirected components differ from Dimapur reachability: E is in the big component yet cannot reach Dimapur
    assert vals(tb.reroute("E", "D"))["status"] == "no_route"


# 7 ---------------------------------------------------------------------------------------------------------
def scenario_w():
    places = [SC.P("D", "town", "Dimapur", "dimapur"), SC.P("M"), SC.P("N"), SC.P("K", "town", "Kohima", "kohima")]
    segs = [SC.S("d", "D", "K", 100, 40), SC.S("m1", "D", "M", 10, 2.5), SC.S("m2", "D", "M", 7, 3.0),
            SC.S("mn", "M", "N", 10, 4), SC.S("nk", "N", "K", 10, 5)]
    return SC.make_dataset(places, segs)


def check_weighted_shortest_path(make):
    tb = make(scenario_w()).tb
    r = vals(tb.reroute("D", "K"))
    assert r["route"]["nodes"] == ["D", "M", "N", "K"], "fastest, not fewest hops (the direct road is 100 min)"
    assert ids(r["route"]["segments"], "segment_id") == ["m2", "mn", "nk"], "parallel segments: the faster one wins"
    assert r["route"]["total_travel_time_min"] == 27 and r["route"]["total_length_km"] == 12.0
    assert r["additional_travel_time_min"] == 0 and r["route"]["travel_time_is_estimate"] is True
    ex = vals(tb.explain_path("D", "K"))
    assert ex["total_travel_time_min"] == 27 and [c["travel_time_min"] for c in ex["contributions"]] == [7, 10, 10]
    assert abs(sum(c["share_of_total"] for c in ex["contributions"]) - 1) < 1e-3 and all(ex["checks"].values())
    e = vals(tb.apply_closure("mn", "M-N out"))["event"]["id"]
    r2 = vals(tb.reroute("D", "K", avoid_event_id=e))
    assert r2["route"]["nodes"] == ["D", "K"] and r2["route"]["total_travel_time_min"] == 100
    assert r2["additional_travel_time_min"] == 73 and r2["baseline_route"]["total_travel_time_min"] == 27
    assert vals(tb.reroute("D", "D"))["route"]["total_travel_time_min"] == 0


# 8 ---------------------------------------------------------------------------------------------------------
def check_blocked_segment_avoidance(make):
    tb = make(SC.scenario_a()).tb
    e = vals(tb.apply_closure("s2", "main road"))["event"]["id"]
    r = vals(tb.reroute("D", "K"))
    used = set(ids(r["route"]["segments"], "segment_id"))
    assert r["route"]["nodes"] == ["D", "J1", "B", "J2", "K"] and used == {"s1", "s4", "s5", "s3"} and "s2" not in used
    assert r["route"]["total_travel_time_min"] == 60 and r["baseline_route"]["total_travel_time_min"] == 40
    assert r["additional_travel_time_min"] == 20
    ex = vals(tb.explain_path("D", "K"))
    assert ex["exclusions"]["blocked_segments"] == {"s2": [e]} and ex["checks"]["no_selected_segment_is_blocked"]
    tb.clear_closure(e)
    assert vals(tb.reroute("D", "K"))["route"]["total_travel_time_min"] == 40
    asked = tb.reroute("D", "K", avoid_event_id=e)     # explicit what-if on a resolved closure
    assert vals(asked)["route"]["total_travel_time_min"] == 60 and any("not active" in x for x in asked["warnings"])
    # closing the bridge closes the segment it carries
    tb.apply_closure("s2", "bridge out", bridge_id="BR1")
    assert vals(tb.reroute("D", "K"))["route"]["total_travel_time_min"] == 60


# 9 ---------------------------------------------------------------------------------------------------------
def check_no_route_behavior(make):
    tb = make(SC.scenario_a()).tb
    e = vals(tb.apply_closure("s3", "J2-K cut"))["event"]["id"]
    env = tb.reroute("D", "K")
    r = vals(env)
    assert r["status"] == "no_route" and r["route"] is None and r["additional_travel_time_min"] is None
    assert r["baseline_route"]["total_travel_time_min"] == 40 and "No directed path" in r["reason"]
    assert "No route" in env["explanation"]
    ex = vals(tb.explain_path("D", "K"))
    assert ex["status"] == "no_route" and ex["selected_path"] is None
    island = tb.reroute("D", "Y")
    assert vals(island)["status"] == "no_route" and vals(island)["baseline_route"] is None
    assert any("No baseline route" in x for x in island["warnings"])
    raises(ToolInputError, tb.reroute, "D", "nowhere")
    raises(ToolInputError, tb.reroute, "nowhere", "D")
    raises(ToolInputError, tb.reroute, "D", "K", avoid_event_id="ghost")


# 10 --------------------------------------------------------------------------------------------------------
def check_hospital_reachability(make):
    w = make(SC.scenario_a())
    tb = w.tb
    e6 = vals(tb.apply_closure("s6", "spur"))["event"]["id"]
    env = tb.find_unreachable_hospitals(e6)
    r = vals(env)
    assert r["affected_settlements"] == ["A"]
    by = {h["hospital_id"]: h for h in r["per_hospital"]}
    assert sorted(by) == ["HE", "HJ", "HK", "HY"], "HN has no road attachment and cannot be evaluated"
    for hid in ("HJ", "HK", "HE"):
        assert by[hid]["newly_unreachable_from"] == ["A"] and by[hid]["reachable_from"] == []
    assert by["HY"]["unreachable_in_baseline_too_from"] == ["A"] and by["HY"]["newly_unreachable_from"] == []
    assert r["hospitals_with_no_usable_route_from_any_affected_settlement"] == ["HE", "HJ", "HK", "HY"]
    assert r["hospitals_without_road_attachment"] == ["HN"]
    assert any("no road attachment" in x for x in env["warnings"]) and any("not suitability" in x for x in env["warnings"])
    # K, C, E lose their link to J2: hospitals are judged on the road network, with times
    tb2 = make(SC.scenario_a()).tb
    e3 = vals(tb2.apply_closure("s3", "J2-K"))["event"]["id"]
    r3 = vals(tb2.find_unreachable_hospitals(e3, threshold_min=10))
    assert r3["affected_settlements"] == ["C", "E", "K"]
    by3 = {h["hospital_id"]: h for h in r3["per_hospital"]}
    # E sits behind a ONE-WAY road: it never had a way out, so J1/K hospitals were already unreachable FROM E in the baseline
    assert by3["HJ"]["newly_unreachable_from"] == ["C", "K"] and by3["HJ"]["unreachable_in_baseline_too_from"] == ["E"]
    assert by3["HK"]["reachable_from"] == ["C", "K"] and by3["HK"]["unreachable_in_baseline_too_from"] == ["E"]
    assert by3["HE"]["reachable_from"] == ["C", "E", "K"] and by3["HE"]["over_threshold_from"] == ["K"], "reachable but slow is a separate list"
    assert r3["hospitals_with_no_usable_route_from_any_affected_settlement"] == ["HJ", "HY"]
    rows = {(p["village_id"], h["hospital_id"]): h for p in r3["per_settlement"] for h in p["hospitals"]}
    assert rows[("K", "HE")]["travel_time_min"] == 12 and rows[("K", "HE")]["baseline_travel_time_min"] == 12
    assert rows[("C", "HE")]["travel_time_min"] == 4 and rows[("C", "HK")]["travel_time_min"] == 8 and rows[("K", "HK")]["travel_time_min"] == 0
    assert rows[("C", "HE")]["route_segments"] == ["s8"] and rows[("E", "HK")]["status"] == "unreachable_in_baseline_too"
    if w.fake:
        assert all("NEAREST_HOSPITAL" not in q for q, _ in w.fake.log), "the convenience relationship must never decide reachability"
    raises(ToolInputError, tb2.find_unreachable_hospitals, e3, threshold_min=-1)
    tb3 = make(SC.scenario_a()).tb
    nothing = vals(tb3.find_unreachable_hospitals(vals(tb3.apply_closure("s5", "alt"))["event"]["id"]))
    assert nothing["affected_settlements"] == [] and nothing["per_hospital"][0]["reachable_from"] == []


# 11 --------------------------------------------------------------------------------------------------------
def scenario_path():
    names = ["pA", "pB", "pC", "pD", "pE"]
    places = [SC.P("pA", "town", "Alpha", "dimapur")] + [SC.P(n, "village", n) for n in names[1:]]
    segs = [SC.S("pAB", "pA", "pB", 1), SC.S("pBC", "pB", "pC", 1), SC.S("pCD", "pC", "pD", 1), SC.S("pDE", "pD", "pE", 1)]
    return SC.make_dataset(places, segs, bridges=[("BRB", "Bridge BC", ["pBC"])])


def check_betweenness_ranking(make):
    tb = make(scenario_path()).tb
    env = tb.find_weak_links("corr-test", top_k=4)
    r = vals(env)
    # path A-B-C-D-E, directed both ways, normalized by NetworkX: edge B-C carries 6 ordered pairs each way / 20 -> 0.6 summed
    assert [(s["segment_id"], s["betweenness"]) for s in r["segments"]] == [("pBC", 0.6), ("pCD", 0.6), ("pAB", 0.4), ("pDE", 0.4)]
    assert [s["ordered_pairs_whose_chosen_shortest_path_uses_it"] for s in r["segments"]] == [12, 12, 8, 8]
    assert [(p["place_id"], p["betweenness"]) for p in r["places"][:3]] == [("pC", 0.666667), ("pB", 0.5), ("pD", 0.5)]
    assert {p["place_id"]: p["is_articulation_point_undirected"] for p in r["places"]} == {"pC": True, "pB": True, "pD": True}
    assert r["bridges"][0]["bridge_id"] == "BRB" and r["bridges"][0]["betweenness"] == 0.6 and r["bridges"][0]["score_inherited_from_segments"] == ["pBC"]
    top = r["segments"][0]
    assert top["if_closed"]["places_cut_off_from_dimapur"] == ["pC", "pD", "pE"] and top["if_closed"]["undirected_components_after"] == 2
    assert r["segments"][2]["if_closed"]["places_cut_off_from_dimapur"] == ["pB", "pC", "pD", "pE"]
    assert top["example_paths"] and all(len(p["nodes"]) >= 2 for p in top["example_paths"])
    assert any("NOT a failure probability" in x for x in env["warnings"]) and "directed graph" in r["representation"]
    # weights matter: the heavy D-A road is never on a shortest path, so it scores zero (it would not unweighted)
    sq = SC.make_dataset([SC.P("qA", "town", "A", "dimapur"), SC.P("qB"), SC.P("qC"), SC.P("qD")],
                         [SC.S("qAB", "qA", "qB", 1), SC.S("qBC", "qB", "qC", 1), SC.S("qCD", "qC", "qD", 1), SC.S("qDA", "qD", "qA", 10)])
    s = vals(make(sq).tb.find_weak_links("corr-test", top_k=4))["segments"]
    assert s[-1]["segment_id"] == "qDA" and s[-1]["betweenness"] == 0.0 and all(x["betweenness"] > 0 for x in s[:-1])
    raises(ToolInputError, tb.find_weak_links, "no-such-corridor")
    for bad in (0, -1, True, "3", 201, 2.5):
        raises(ToolInputError, tb.find_weak_links, "corr-test", bad)


# 12 --------------------------------------------------------------------------------------------------------
def scenario_season():
    places = [SC.P("D", "town", "Dimapur", "dimapur"), SC.P("J"), SC.P("K", "town", "Kohima", "kohima")]
    segs = [SC.S("s1", "D", "J", 5, risk="high"), SC.S("s2", "J", "K", 5, risk="medium"), SC.S("s3", "D", "K", 12)]
    events = [{"id": "o1", "started_at": "2024-08", "segments": ["s2"]}, {"id": "o2", "started_at": "2025-07-14", "segments": ["s2"]},
              {"id": "o3", "started_at": "2024-09-03", "segments": ["s3"]}, {"id": "o4", "started_at": "2024-12", "segments": ["s1"]},
              {"id": "o5", "started_at": "2024-07", "near": ["D"]}]
    return SC.make_dataset(places, segs, bridges=[("BA", "Bridge A", ["s1"]), ("BB", "Bridge B", ["s2"])],
                           seasons=[("monsoon", "Monsoon", 6, 9, ["s1", "s2", "s3"]), ("winter", "Winter", 11, 2, ["s3"])],
                           events=events, failed_in=[("BB", "o1", 2024)])


def check_seasonal_risk_ranking(make):
    tb = make(scenario_season(), clock="2026-08-05T12:00:%02dZ").tb
    sim = vals(tb.apply_closure("s1", "demo closure"))["event"]["id"]
    tb.clear_closure(sim)
    env = tb.seasonal_risk(7)
    r = vals(env)
    assert [x["id"] for x in r["ranked"]] == ["s1", "s2", "BB", "s3"], [x["id"] for x in r["ranked"]]
    by = {x["id"]: x for x in r["ranked"]}
    assert ids(by["s2"]["observed_in_season"]) == ["o1", "o2"] and by["s2"]["counts"] == {"observed": 2, "simulated": 0}
    assert by["s1"]["observed_in_season"] == [] and ids(by["s1"]["simulated_in_season"]) == [sim], "simulated history stays separate"
    assert by["s1"]["events_outside_season"] == ["o4"]
    assert ids(by["BB"]["observed_in_season"]) == ["o1"] and ids(by["s3"]["observed_in_season"]) == ["o3"]
    assert [x["rank"] for x in r["ranked"]] == [1, 2, 3, 4] and r["unranked_applicable_items"] == 1 and "BA" not in by
    assert r["observed_events_without_links"] == [{"id": "o5", "started_at": "2024-07", "near_places": ["Dimapur"]}]
    w = " ".join(env["warnings"]).lower()
    assert "probabilit" in w and "simulated" in w and "not linked" in w
    assert vals(tb.seasonal_risk("July"))["ranked"] == r["ranked"] and vals(tb.seasonal_risk("7"))["ranked"] == r["ranked"]
    win = vals(tb.seasonal_risk(1))       # a season that wraps the year end
    assert ids(win["seasons"]) == ["winter"] and win["ranked"] == [] and win["unranked_applicable_items"] == 1
    none = vals(tb.seasonal_risk(5))
    assert none["seasons"] == [] and none["ranked"] == []
    for bad in (0, 13, -1, "x", None, True, 7.5, ""):
        raises(ToolInputError, tb.seasonal_risk, bad)


# 13 --------------------------------------------------------------------------------------------------------
def check_historical_event_idempotency(make):
    w = make(SC.scenario_a(events=[{"id": "obs1", "started_at": "2024-08"}]))
    tb = w.tb
    e = vals(tb.apply_closure("s2", "bridge out", bridge_id="BR1"))["event"]["id"]
    first = vals(tb.remember_closure(e))
    assert first["already_remembered"] is False and first["failure_records_created"] == ["BR1"] and first["history_kind"] == "simulated"
    stamp = first["remembered_at"]
    for _ in range(3):
        rep = tb.remember_closure(e)
        assert vals(rep)["already_remembered"] is True and vals(rep)["failure_records_created"] == [] and vals(rep)["remembered_at"] == stamp
        assert vals(rep)["failure_records_already_present"] == ["BR1"] and vals(rep)["failure_records_total_for_event"] == 1
    if w.fake:
        assert len(w.fake.failed) == 1 and w.fake.failed[("BR1", e)] == 2026
    vals(tb.apply_closure("s2", "bridge out", bridge_id="BR1"))   # a repeat request must not create a second event
    if w.fake:
        assert len([x for x in w.fake.events if x.startswith("sim-")]) == 1
    tb.clear_closure(e)
    assert vals(tb.remember_closure(e))["failure_records_total_for_event"] == 1, "clearing must not erase or inflate history"
    seg_only = vals(tb.apply_closure("s2", "segment only"))["event"]["id"]
    r = tb.remember_closure(seg_only)
    assert vals(r)["failure_records_created"] == [] and vals(r)["bridges_on_blocked_segments_not_recorded"] == ["BR1"]
    assert any("no bridge failure was recorded" in x for x in r["warnings"])
    obs = vals(tb.remember_closure("obs1"))
    assert obs["history_kind"] == "observed" and obs["failure_records_created"] == []
    raises(ToolInputError, tb.remember_closure, "ghost")


# 14 --------------------------------------------------------------------------------------------------------
def check_provenance_and_query_traceability(make):
    w = make(SC.scenario_a(events=[{"id": "obs1", "started_at": "2024-08"}]))
    tb = w.tb
    calls = [("apply_closure", ("s6", "spur")), ("find_cut_off_places", ("sim-e1",)), ("reroute", ("D", "K")),
             ("explain_path", ("D", "K")), ("find_unreachable_hospitals", ("sim-e1",)), ("find_weak_links", ("corr-test", 3)),
             ("seasonal_risk", (8,)), ("draft_alerts", ("sim-e1",)), ("remember_closure", ("sim-e1",)),
             ("audit_closure", ("sim-e1",)), ("clear_closure", ("sim-e1",))]
    algorithmic = {"find_cut_off_places", "reroute", "explain_path", "find_unreachable_hospitals", "find_weak_links", "draft_alerts"}
    for name, args in calls:
        start = len(w.fake.log) if w.fake else None
        env = getattr(tb, name)(*args)
        assert SPEC_KEYS <= set(env), f"{name}: missing {SPEC_KEYS - set(env)}"
        assert env["cypher"] and all(isinstance(q, str) and q.strip() for q in env["cypher"]), name
        if w.fake:
            assert env["cypher"] == [q for q, _ in w.fake.log[start:]], f"{name}: cypher must be exactly what was sent"
            assert env["params"]["queries"] == [p for _, p in w.fake.log[start:]]
        assert len(env["params"]["query_names"]) == len(env["cypher"]) and env["params"]["inputs"]
        assert env["algorithm"]["name"] and env["algorithm"]["implementation"] and isinstance(env["warnings"], list)
        assert set(env["graph"]) == {"nodes", "edges"} and env["explanation"]
        if name in algorithmic:
            assert "NetworkX" in env["algorithm"]["implementation"] and "documented fallback" in env["algorithm"]["implementation"]
        for n in env["graph"]["nodes"]:
            assert n["id"] and "source_url" in n
        for ed in env["graph"]["edges"]:
            assert ed["id"] and "source_url" in ed and "confidence" in ed
    assert any("CREATE (e:ClosureEvent" in q for q in tb.apply_closure("s7", "another")["cypher"]), "write statements are reported too"
    ex = vals(tb.explain_path("D", "K"))
    assert all(c["source_url"] and c["confidence"] and c["data_status"] for c in ex["contributions"])
    assert vals(tb.reroute("D", "K"))["route"]["segments"][0]["source_url"].startswith("https://")
    cypher_blob = " ".join(tb.reroute("D", "K")["cypher"])
    assert "CONNECTED_BY" in cypher_blob and "ClosureEvent" in cypher_blob and "RoadSegment" in cypher_blob


# 15 --------------------------------------------------------------------------------------------------------
def check_baseline_restoration(make):
    w = make(SC.scenario_a())
    tb = w.tb
    e1 = vals(tb.apply_closure("s2", "main"))["event"]["id"]
    active = vals(tb.audit_closure(e1))
    codes = {d["code"] for d in active["discrepancies"]}
    assert active["returned_to_baseline"] is False and {"event_not_resolved", "segment_still_blocked"} <= codes
    tb.clear_closure(e1)
    done = tb.audit_closure(e1)
    r = vals(done)
    assert r["returned_to_baseline"] is True and r["resolved"] is True and r["discrepancies"] == [] and r["blocked_segments_now"] == {}
    assert {c["name"] for c in r["checks"]} >= {"network_matches_baseline_fingerprint", "segments_match_baseline", "connections_match_segments"}
    assert all(c["ok"] for c in r["checks"]) and done["explanation"].startswith("The graph is back")
    # another closure still active: must NOT claim baseline
    a = vals(tb.apply_closure("s2", "again"))["event"]["id"]
    b = vals(tb.apply_closure("s4", "other"))["event"]["id"]
    tb.clear_closure(a)
    r2 = tb.audit_closure(a)
    assert vals(r2)["returned_to_baseline"] is False and vals(r2)["other_active_closures"] == [b]
    assert "other_active_closures" in {d["code"] for d in vals(r2)["discrepancies"]} and "NOT at baseline" in r2["explanation"]
    assert vals(r2)["places_unreachable_from_dimapur_due_to_active_closures"] == []   # alternates exist, but the road is not baseline
    tb.clear_closure(b)
    assert vals(tb.audit_closure(b))["returned_to_baseline"] is True
    # same segment closed twice: clearing one leaves the segment blocked
    c = vals(tb.apply_closure("s3", "x"))["event"]["id"]
    d = vals(tb.apply_closure("s3", "y"))["event"]["id"]
    tb.clear_closure(c)
    r3 = vals(tb.audit_closure(c))
    assert r3["returned_to_baseline"] is False and "segment_still_blocked" in {x["code"] for x in r3["discrepancies"]}
    tb.clear_closure(d)
    if w.fake:   # somebody else changed the network after the closure was cleared
        w.fake.tamper_segment("s1", travel_time_min=11)
        t = vals(tb.audit_closure(d))
        tc = {x["code"] for x in t["discrepancies"]}
        assert t["returned_to_baseline"] is False and {"segment_changed_since_baseline", "network_fingerprint_mismatch", "connection_integrity"} <= tc
        w.fake.tamper_segment("s1", travel_time_min=10)
        assert vals(tb.audit_closure(d))["returned_to_baseline"] is True
        w.fake.drop_connection("s1", "reverse")
        t2 = vals(tb.audit_closure(d))
        assert t2["returned_to_baseline"] is False and "connection_integrity" in {x["code"] for x in t2["discrepancies"]}
    # data seeded before fingerprints existed: cannot be verified, so no claim is made
    old = make(SC.scenario_a(fingerprint=False)).tb
    e = vals(old.apply_closure("s6", "spur"))["event"]["id"]
    old.clear_closure(e)
    env = old.audit_closure(e)
    assert vals(env)["returned_to_baseline"] is None and any("fingerprint" in x for x in env["warnings"])


# 16 --------------------------------------------------------------------------------------------------------
def scenario_invalid(value):
    places = [SC.P("D", "town", "Dimapur", "dimapur"), SC.P("J"), SC.P("L"), SC.P("K", "town", "Kohima", "kohima")]
    segs = [SC.S("dj", "D", "J", 5), SC.S("jk", "J", "K", value), SC.S("dl", "D", "L", 20), SC.S("lk", "L", "K", 30)]
    return SC.make_dataset(places, segs)


def check_missing_and_invalid_travel_time(make):
    live = make(scenario_w()).fake is None
    for value in ([None, 0, -5] if live else [None, float("nan"), 0, -5, "fast", float("inf")]):
        tb = make(scenario_invalid(value)).tb
        env = tb.reroute("D", "K")
        r = vals(env)
        assert r["route"]["nodes"] == ["D", "L", "K"] and r["route"]["total_travel_time_min"] == 50, f"invalid weight {value!r} must not be used (0 would give 5)"
        assert any("missing/invalid travel time" in x for x in env["warnings"]), value
        ex = vals(tb.explain_path("D", "K"))
        assert ex["exclusions"]["excluded_invalid_weight"] == ["jk"] and ex["total_travel_time_min"] == 50
        # connectivity ignores weights; routing must refuse them
        e = vals(tb.apply_closure("lk", "valid road out"))["event"]["id"]
        only = tb.reroute("D", "K")
        assert vals(only)["status"] == "no_route" and "invalid travel time" in vals(only)["reason"] and vals(only)["route"] is None
        cut = vals(tb.find_cut_off_places(e))
        assert cut["newly_unreachable"]["count"] == 0, "K is still connected by a road; only its weight is unusable"
        assert vals(tb.find_weak_links("corr-test", 5))["graph_size"]["directed_edges"] >= 1


# 17 --------------------------------------------------------------------------------------------------------
def check_incomplete_geographic_data(make):
    tb = make(SC.scenario_a()).tb
    e = vals(tb.apply_closure("s6", "spur"))["event"]["id"]
    env = tb.find_cut_off_places(e)
    assert any("HN" in x and "no road attachment" in x for x in env["warnings"])
    assert any("already unreachable" in x for x in env["warnings"])
    # no Dimapur in the data
    nodim = SC.make_dataset([SC.P("D", "town", "Dimapur", None), SC.P("K", "town", "Kohima", "kohima")], [SC.S("s", "D", "K", 5)])
    t2 = make(nodim).tb
    ev = vals(t2.apply_closure("s", "x"))["event"]["id"]
    assert "dimapur" in str(raises(ToolStateError, t2.find_cut_off_places, ev))
    # a connection naming a missing segment is never traversable
    bad = SC.make_dataset([SC.P("D", "town", "Dimapur", "dimapur"), SC.P("Z", "village", "Zulu"), SC.P("K", "town", "Kohima", "kohima")],
                          [SC.S("s", "D", "K", 5)],
                          extra_connections=[{"from_place_id": "D", "to_place_id": "Z", "segment_id": "ghost", "travel_time_min": 1, "direction": "forward"},
                                             {"from_place_id": "Z", "to_place_id": "D", "segment_id": None, "travel_time_min": 1, "direction": "reverse"}])
    t3 = make(bad).tb
    rr = t3.reroute("D", "Z")
    assert vals(rr)["status"] == "no_route" and any("name no existing RoadSegment" in x for x in rr["warnings"])
    ev3 = vals(t3.apply_closure("s", "x"))["event"]["id"]
    assert ids(vals(t3.find_cut_off_places(ev3))["already_unreachable_in_baseline"]) == ["Z"]
    t3.clear_closure(ev3)
    damaged = t3.audit_closure(ev3)       # the audit must cope with exactly this kind of damage, and not claim baseline
    assert vals(damaged)["returned_to_baseline"] is False and "connection_integrity" in {d["code"] for d in vals(damaged)["discrepancies"]}
    # Kohima cannot be reached in the extract
    split = SC.make_dataset([SC.P("D", "town", "Dimapur", "dimapur"), SC.P("J"), SC.P("K", "town", "Kohima", "kohima")], [SC.S("s", "D", "J", 5)])
    t4 = make(split).tb
    ev4 = vals(t4.apply_closure("s", "x"))["event"]["id"]
    assert any("Kohima is not reachable" in x for x in t4.find_cut_off_places(ev4)["warnings"])
    raises(ToolInputError, tb.find_cut_off_places, "ghost")
    raises(ToolInputError, tb.draft_alerts, "ghost")
    raises(ToolInputError, tb.audit_closure, "ghost")


# 18 --------------------------------------------------------------------------------------------------------
def check_duplicate_seed_operations(make):
    import seed
    ds = SC.scenario_a()
    st = seed.build_statements(ds)
    assert st == seed.build_statements(SC.scenario_a()) and all("MERGE" in s["query"] and "CREATE" not in s["query"] for s in st)

    class Rec:
        def __init__(self):
            self.q = []

        def query(self, q, p=None):
            self.q.append((q, p))
            return type("R", (), {"nodes_created": 0, "relationships_created": 0, "properties_set": 0, "result_set": []})()

    a, b = Rec(), Rec()
    seed.seed_graph(a, ds, log=lambda *_: None)
    seed.seed_graph(b, ds, log=lambda *_: None)
    assert a.q == b.q
    # tools see an identical graph whenever the same dataset is loaded
    r1 = vals(make(SC.scenario_a()).tb.reroute("D", "E"))
    r2 = vals(make(SC.scenario_a()).tb.reroute("D", "E"))
    assert r1 == r2


# draft_alerts -----------------------------------------------------------------------------------------------
def check_draft_alerts(make):
    w = make(SC.scenario_a())
    tb = w.tb
    e6 = vals(tb.apply_closure("s6", "spur"))["event"]["id"]
    env = tb.draft_alerts(e6)
    r = vals(env)
    assert r["settlements_considered"] == ["A"] and len(r["alerts"]) == 1
    a = r["alerts"][0]
    assert a["text"].startswith("SIMULATION \u2014 Road closure reported in the model near") and "Alpha" in a["text"] and "Dimapur" in a["text"]
    assert a["text"].endswith("Verify road conditions and official guidance before travelling.") and a["length"] <= 480
    assert "no road route from Alpha to any recorded hospital" in a["text"] and a["status"] == "draft" and a["sent"] is False
    assert a["routed_via_segments"] == [] and a["supporting"]["cut_off_evidence"]["baseline_path_segments"] == ["s1", "s2", "s6"]
    assert "population" not in a["text"].lower() and "people" not in a["text"].lower()
    assert any("Drafts only" in x for x in env["warnings"])
    if w.fake:
        stored = w.fake.alerts["alert-" + e6 + "-A"]
        assert stored["status"] == "draft" and stored["simulation"] is True and w.fake.alert_targets["alert-" + e6 + "-A"] == "A"
    # a closure that leaves some hospitals reachable names one, with its route
    tb2 = make(SC.scenario_a()).tb
    e3 = vals(tb2.apply_closure("s3", "J2-K"))["event"]["id"]
    out = vals(tb2.draft_alerts(e3))
    texts = {x["target_place_id"]: x for x in out["alerts"]}
    assert sorted(texts) == ["C", "E", "K"]
    assert "about 4 min (estimate) to Hosp E" in texts["C"]["text"] and texts["C"]["routed_via_segments"] == ["s8"]
    assert "capabilities are unknown" in texts["C"]["text"] and "assumed speeds" in texts["C"]["text"]
    assert "about 0 min (estimate) to Hosp E" in texts["E"]["text"] and "about 0 min (estimate) to Hosp K" in texts["K"]["text"]
    assert all(x["length"] <= 480 for x in out["alerts"])
    again = vals(tb2.draft_alerts(e3))                   # regenerating must not duplicate
    assert [x["alert_id"] for x in again["alerts"]] == [x["alert_id"] for x in out["alerts"]]
    tb2.clear_closure(e3)
    none = tb2.draft_alerts(e3)
    assert vals(none)["alerts"] == [] and any("not active" in x for x in none["warnings"])


def check_capability_probe(make):
    w = make(SC.scenario_a(), procedures=[("algo.SPpaths", "READ"), ("algo.betweenness", "READ"), ("algo.WCC", "READ"), ("db.labels", "READ")])
    env = w.tb.probe_capabilities()
    r = vals(env)
    assert r["algo_procedures"] == ["algo.SPpaths", "algo.WCC", "algo.betweenness"] and r["native_weighted_paths"] and r["native_betweenness"]
    assert env["cypher"] == [Q.Q_PROCEDURES]


CHECKS = [  # (name, function, fake_only)
    ("closure_creation", check_closure_creation, False),
    ("multiple_closures_same_segment", check_multiple_closures_same_segment, False),
    ("closure_resolution", check_closure_resolution, False),
    ("connectivity_changes", check_connectivity_changes, False),
    ("directed_reachability", check_directed_reachability, False),
    ("component_correctness", check_component_correctness, False),
    ("weighted_shortest_path", check_weighted_shortest_path, False),
    ("blocked_segment_avoidance", check_blocked_segment_avoidance, False),
    ("no_route_behavior", check_no_route_behavior, False),
    ("hospital_reachability", check_hospital_reachability, False),
    ("betweenness_ranking", check_betweenness_ranking, False),
    ("seasonal_risk_ranking", check_seasonal_risk_ranking, False),
    ("historical_event_idempotency", check_historical_event_idempotency, False),
    ("provenance_and_query_traceability", check_provenance_and_query_traceability, False),
    ("baseline_restoration", check_baseline_restoration, False),
    ("missing_and_invalid_travel_time", check_missing_and_invalid_travel_time, False),
    ("incomplete_geographic_data", check_incomplete_geographic_data, False),
    ("duplicate_seed_operations", check_duplicate_seed_operations, False),
    ("draft_alerts", check_draft_alerts, False),
    ("capability_probe", check_capability_probe, True),
]
