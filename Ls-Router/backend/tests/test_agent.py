"""Tests for Phase 3 LandslideAgent orchestration, ambiguity gating, tool selection, and claim provenance."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import scenarios as SC  # noqa: E402
from app.agent.llm import LLMToolCall, LLMTurn  # noqa: E402
from app.agent.orchestrator import SAFETY_DISCLAIMER, SYSTEM_PROMPT, LandslideAgent  # noqa: E402
from app.graph import queries as Q  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402
from fake_graph import FakeGraph  # noqa: E402


class AgentWorld:
    def __init__(self, ds=None, llm_client=None):
        self.fake = FakeGraph(ds or SC.scenario_a())
        self._n = self._t = 0
        self.agent = LandslideAgent(
            FalkorStore(self.fake),
            clock=self._clock,
            id_factory=self._id,
            llm_client=llm_client,
        )

    def _id(self):
        self._n += 1
        return f"e{self._n}"

    def _clock(self):
        self._t += 1
        return f"2026-10-14T12:00:{self._t:02d}Z"


def assert_every_claim_has_provenance(report: dict) -> None:
    assert report["disclaimer"] == SAFETY_DISCLAIMER
    assert report["system_prompt"] == SYSTEM_PROMPT
    assert report["claims"], "report must include supporting claims"
    called_tools = set(report["provenance"]["tools_called"])
    known_cypher = {
        Q.Q_PLACES, Q.Q_SEGMENTS, Q.Q_CONNECTIONS, Q.Q_HOSPITALS, Q.Q_BRIDGES, Q.Q_EVENTS,
        Q.Q_BLOCKS_SEGMENT, Q.Q_BLOCKS_BRIDGE, Q.Q_FAILED_IN, Q.Q_NEAR_PLACE, Q.Q_SEASONS,
        Q.Q_DATASET, Q.Q_PROCEDURES, Q.W_CREATE_CLOSURE, Q.W_CREATE_CLOSURE_WITH_BRIDGE,
        Q.W_RESOLVE_CLOSURE, Q.W_MARK_REMEMBERED, Q.W_RECORD_FAILURE, Q.W_UPSERT_ALERT,
        Q.W_CLEAR_ALERT_ROUTES, Q.W_LINK_ALERT_ROUTE,
    }
    for claim in report["claims"]:
        assert claim["claim_id"].startswith("claim-")
        assert claim["statement"]
        assert claim["supported_by_tool"] in called_tools
        assert isinstance(claim["supporting_data"], dict) and claim["supporting_data"]
        prov = claim["provenance"]
        assert prov["algorithm"]["name"] and prov["algorithm"]["implementation"]
        assert prov["cypher_executed"], f"claim {claim['claim_id']} has no executed Cypher"
        for q in prov["cypher_executed"]:
            assert q in known_cypher
        for src in prov["sources"]:
            assert src["id"]
            assert src["confidence"] in ("verified", "unverified", None)


def test_ambiguous_event_requires_clarification_and_creates_no_closure():
    w = AgentWorld()
    report = w.agent.run("Landslide near NH29 kilometre 42.")
    assert report["status"] == "clarification_required"
    assert report["closure"] is None
    assert report["affected_settlements"] is None
    assert report["provenance"]["tools_called"] == ["resolve_location"]
    assert [c["segment_id"] for c in report["resolution"]["candidate_segments"]] == ["s3", "s2", "s1"]
    assert not w.fake.events, "no simulated closure may be written when location is ambiguous"
    assert_every_claim_has_provenance(report)


def test_no_match_creates_no_closure():
    w = AgentWorld()
    report = w.agent.run("Landslide near Atlantis")
    assert report["status"] == "no_match"
    assert report["closure"] is None
    assert not w.fake.events
    assert_every_claim_has_provenance(report)


def test_confirmed_closure_on_s2_calculates_detour_route_and_updates_graph():
    w = AgentWorld()
    report = w.agent.run("Landslide near NH29 kilometre 42.", target_segment_id="s2", month=7)
    assert report["status"] == "completed"
    assert report["closure"]["event"]["id"] == "sim-e1"
    assert report["closure"]["event"]["blocks_segments"] == ["s2"]

    # Reachability: s2 has a detour via Bravo (s4 -> B -> s5), so 0 settlements are newly cut off
    aff = report["affected_settlements"]
    assert aff["newly_unreachable"]["count"] == 0
    assert [p["id"] for p in aff["already_unreachable_in_baseline"]] == ["X", "Y"]

    # Alternate route: D -> J1 -> B -> J2 -> K = 60 min vs 40 min baseline (+20 min detour)
    primary_route = report["routes"][0]
    assert primary_route["from_place_id"] == "D" and primary_route["to_place_id"] == "K"
    assert primary_route["status"] == "route_found"
    assert primary_route["route"]["nodes"] == ["D", "J1", "B", "J2", "K"]
    assert primary_route["route"]["total_travel_time_min"] == 60.0
    assert primary_route["baseline_route"]["total_travel_time_min"] == 40.0
    assert primary_route["additional_travel_time_min"] == 20.0
    assert [c["segment_id"] for c in primary_route["explanation_details"]["contributions"]] == ["s1", "s4", "s5", "s3"]

    # Weak links and seasonal risk included
    assert report["weak_links"]["segments"]
    assert report["seasonal_risk"]["month"] == 7
    assert report["draft_alerts"]["alerts"] == []
    assert_every_claim_has_provenance(report)


def test_closure_on_s3_cuts_off_settlements_evaluates_hospitals_and_drafts_alerts():
    w = AgentWorld()
    report = w.agent.run("Landslide between J2 and Kohima")
    assert report["status"] == "completed"
    assert report["resolution"]["selected_target"]["segment_id"] == "s3"
    assert report["closure"]["event"]["id"] == "sim-e1"

    # Newly cut-off settlements: Kohima (town), Charlie & Echo (villages)
    aff = report["affected_settlements"]
    assert aff["newly_unreachable"]["count"] == 3
    assert [v["id"] for v in aff["newly_unreachable"]["villages"]] == ["C", "E"]
    assert [t["id"] for t in aff["newly_unreachable"]["towns"]] == ["K"]
    assert [p["id"] for p in aff["already_unreachable_in_baseline"]] == ["X", "Y"]

    # Hospital reachability from C, E, K
    hosp = report["hospital_reachability"]
    assert hosp["affected_settlements"] == ["C", "E", "K"]
    assert hosp["hospitals_with_no_usable_route_from_any_affected_settlement"] == ["HJ", "HY"]
    assert hosp["hospitals_without_road_attachment"] == ["HN"]

    # Primary corridor route D -> K is now no_route
    assert report["routes"][0]["status"] == "no_route"
    assert report["routes"][0]["route"] is None

    # Synthetic draft alerts created for C, E, K
    alerts = report["draft_alerts"]["alerts"]
    assert [a["target_place_id"] for a in alerts] == ["C", "E", "K"]
    for a in alerts:
        assert a["text"].startswith("SIMULATION \u2014")
        assert a["sent"] is False and a["status"] == "draft"
    assert len(w.fake.alerts) == 3
    assert_every_claim_has_provenance(report)


def test_agent_selects_relevant_tools_for_targeted_queries():
    w = AgentWorld()

    # 1. Reroute query
    r_route = w.agent.run("Find the fastest route from Dimapur to Echo")
    assert r_route["provenance"]["tools_called"] == ["reroute", "explain_path"]
    assert r_route["routes"][0]["route"]["total_travel_time_min"] == 52.0
    assert_every_claim_has_provenance(r_route)

    # 2. Weak links query
    r_weak = w.agent.run("What are the weak links in the corridor?")
    assert r_weak["provenance"]["tools_called"] == ["find_weak_links"]
    assert r_weak["weak_links"]["segments"][0]["segment_id"] == "s3"
    assert_every_claim_has_provenance(r_weak)

    # 3. Seasonal risk query
    r_season = w.agent.run("Show elevated monsoon risk in August")
    assert r_season["provenance"]["tools_called"] == ["seasonal_risk"]
    assert r_season["seasonal_risk"]["month"] == 8
    assert_every_claim_has_provenance(r_season)

    # 4. Apply closure on Bridge One, remember it, then clear and audit
    r_bridge = w.agent.run("Bridge collapse at Bridge One")
    ev_id = r_bridge["closure"]["event"]["id"]
    assert r_bridge["closure"]["event"]["blocks_bridges"] == ["BR1"]

    r_mem = w.agent.run(f"Remember closure {ev_id}")
    assert r_mem["provenance"]["tools_called"] == ["remember_closure"]
    assert r_mem["remembered_closure"]["failure_records_created"] == ["BR1"]
    assert_every_claim_has_provenance(r_mem)

    r_clear = w.agent.run(f"Clear closure {ev_id} and audit")
    assert r_clear["provenance"]["tools_called"] == ["clear_closure", "audit_closure"]
    assert r_clear["closure_resolution"]["restored_segments"] == ["s2"]
    assert r_clear["closure_audit"]["returned_to_baseline"] is True
    assert_every_claim_has_provenance(r_clear)


def test_llm_planner_mode_enforces_ambiguity_gate_and_grounds_report():
    class StubAmbiguousSkippingLLM:
        """Tries to call apply_closure directly without resolving or despite ambiguity."""

        def step(self, system_prompt, messages, tools):
            return LLMTurn(
                tool_calls=[
                    LLMToolCall(id="c1", name="apply_closure", arguments={"segment_id": "s2", "description": "skip check"})
                ],
                provider="stub",
                model="stub-1",
            )

    w1 = AgentWorld(llm_client=StubAmbiguousSkippingLLM())
    rep1 = w1.agent.run("Landslide near NH29 kilometre 42.")
    assert rep1["status"] == "clarification_required", "Guardrail must block LLM from closing an ambiguous location"
    assert not w1.fake.events

    class StubProperLLM:
        """Selects resolve_location then apply_closure on an unambiguous prompt."""

        def __init__(self):
            self.turns = 0

        def step(self, system_prompt, messages, tools):
            self.turns += 1
            if self.turns == 1:
                return LLMTurn(
                    tool_calls=[LLMToolCall(id="c1", name="resolve_location", arguments={"description": "Landslide near Alpha"})],
                    provider="stub",
                    model="stub-1",
                )
            if self.turns == 2:
                return LLMTurn(
                    tool_calls=[LLMToolCall(id="c2", name="apply_closure", arguments={"segment_id": "s6", "description": "Landslide near Alpha"})],
                    provider="stub",
                    model="stub-1",
                )
            return LLMTurn(tool_calls=[], final_text="Completed analysis.", provider="stub", model="stub-1")

    w2 = AgentWorld(llm_client=StubProperLLM())
    rep2 = w2.agent.run("Landslide near Alpha")
    assert rep2["status"] == "completed"
    assert rep2["closure"]["event"]["blocks_segments"] == ["s6"]
    assert [v["id"] for v in rep2["affected_settlements"]["newly_unreachable"]["villages"]] == ["A"]
    assert_every_claim_has_provenance(rep2)

