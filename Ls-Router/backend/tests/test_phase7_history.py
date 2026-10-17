"""Phase 7 tests: Historical Memory and Seasonal Risk.

Verifies all seven Phase 7 acceptance criteria and the non-predictive language rule:
1. Past events persist across application restarts.
2. Historical failure counts cannot be inflated by duplicate requests.
3. Simulated history is visibly labeled.
4. Seasonal rankings use actual stored history and risk attributes.
5. Clearing a closure does not erase its historical record.
6. Repeated failure events influence the ranking according to a documented scoring rule.
7. The agent explains the evidence behind its warning (using 'elevated modeled risk' /
   'historical pattern warrants review' and never claiming a road or bridge will fail).
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.cli import build_synthetic_fixture_dataset
from app.agent.orchestrator import LandslideAgent
from app.config import get_settings
from app.db import reset_offline_graph_cache
from app.graph.in_memory import InMemoryGraph
from app.graph.store import FalkorStore
from app.graph.tools import Toolbox
from app.main import app


def _make_clock_and_ids(prefix: str = "2026-07-15T12:00:"):
    n = 0
    t = 0

    def _id() -> str:
        nonlocal n
        n += 1
        return f"p7-{n}"

    def _clock() -> str:
        nonlocal t
        t += 1
        return f"{prefix}{t:02d}Z"

    return _clock, _id


def _build_fixture_with_observed_history() -> dict:
    """Synthetic fixture enriched with one observed monsoon event on s2 / BR1 (mirrors curated observed history)."""
    ds = build_synthetic_fixture_dataset()
    ds["observed_events"] = [
        {
            "id": "obs-2024-piphema",
            "description": "Observed July 2024 mudslide near Bridge One on NH29",
            "lat": 10.0,
            "lon": 10.035,
            "started_at": "2024-07-08T00:00:00Z",
            "ended_at": "2024-07-12T00:00:00Z",
            "cause": "mudslide",
            "status": "historical",
            "data_status": "observed",
            "confidence": "verified",
            "last_verified": "2024-07-15",
            "source_url": "https://example.test/news/obs-2024-piphema",
        }
    ]
    ds["event_blocks"] = [{"event_id": "obs-2024-piphema", "target_id": "s2", "target_label": "RoadSegment"}]
    ds["event_failed_bridges"] = [{"bridge_id": "BR1", "event_id": "obs-2024-piphema", "year": 2024}]
    for s in ds["segments"]:
        if s["id"] == "s2":
            s["monsoon_risk"] = "high"
    for b in ds["bridges"]:
        if b["id"] == "BR1":
            b["monsoon_risk"] = "high"
    return ds


# ---------------------------------------------------------------------------
# 1. Past events persist across application restarts
# ---------------------------------------------------------------------------
def test_past_events_persist_across_application_restarts(tmp_path: Path, monkeypatch):
    history_file = tmp_path / "persisted_history.json"
    monkeypatch.setenv("LANDSLIDE_USE_FIXTURE", "true")
    monkeypatch.setenv("LANDSLIDE_HISTORY_FILE", str(history_file))
    get_settings.cache_clear()
    reset_offline_graph_cache()

    try:
        # Session 1: Create a monsoon closure on s2 with bridge BR1, remember it, and resolve it
        with TestClient(app) as client1:
            create_res = client1.post(
                "/closure",
                json={
                    "description": "July monsoon washout at Bridge 1 on NH29",
                    "target_segment_id": "s2",
                    "target_bridge_id": "BR1",
                    "cause": "flood",
                    "started_at": "2026-07-18T09:30:00Z",
                    "remember_in_history": True,
                },
            )
            assert create_res.status_code == 200
            created_body = create_res.json()
            assert created_body["status"] == "applied"
            event_id = created_body["event_id"]
            assert created_body["remembered"]["failure_records_created"] == ["BR1"]

            resolve_res = client1.post(f"/resolve/{event_id}")
            assert resolve_res.status_code == 200
            assert resolve_res.json()["returned_to_baseline"] is True

        assert history_file.exists(), "History state file must be written on graph mutations"

        # Simulate full application restart by clearing caches and creating a new TestClient
        get_settings.cache_clear()
        reset_offline_graph_cache()

        with TestClient(app) as client2:
            hist = client2.get("/history").json()
            sim_ids = [e["id"] for e in hist["simulated_events"]]
            assert event_id in sim_ids
            persisted_ev = next(e for e in hist["simulated_events"] if e["id"] == event_id)
            assert persisted_ev["status"] == "resolved"
            assert persisted_ev["remembered"] is True
            assert persisted_ev["remembered_at"] is not None
            assert persisted_ev["blocks_segments"] == ["s2"]
            assert persisted_ev["blocks_bridges"] == ["BR1"]

            bf = [f for f in hist["bridge_failures"] if f["event_id"] == event_id]
            assert len(bf) == 1
            assert bf[0]["bridge_id"] == "BR1"
            assert bf[0]["year"] == 2026
            assert "SIMULATED" in bf[0]["history_label"]

            # Seasonal risk after restart still includes the persisted event
            sr = client2.get("/seasonal-risk", params={"month": "july"}).json()
            br1_rank = next(r for r in sr["ranked"] if r["kind"] == "bridge" and r["id"] == "BR1")
            assert any(e["id"] == event_id for e in br1_rank["simulated_in_season"])
    finally:
        get_settings.cache_clear()
        reset_offline_graph_cache()


# ---------------------------------------------------------------------------
# 2. Historical failure counts cannot be inflated by duplicate requests
# ---------------------------------------------------------------------------
def test_historical_failure_counts_cannot_be_inflated_by_duplicate_requests():
    ds = _build_fixture_with_observed_history()
    graph = InMemoryGraph(ds)
    store = FalkorStore(graph)
    clock, id_factory = _make_clock_and_ids()
    tb = Toolbox(store, clock=clock, id_factory=id_factory)

    # Apply closure with explicit timestamp and bridge
    c1 = tb.apply_closure(
        segment_id="s2",
        description="Monsoon pier scour on Bridge 1",
        cause="flood",
        bridge_id="BR1",
        started_at="2026-07-20T06:00:00Z",
    )["result"]
    assert c1["created"] is True
    eid = c1["event"]["id"]

    # Duplicate active apply_closure request
    c2 = tb.apply_closure(
        segment_id="s2",
        description="Monsoon pier scour on Bridge 1",
        cause="flood",
        bridge_id="BR1",
        started_at="2026-07-20T06:00:00Z",
    )["result"]
    assert c2["created"] is False
    assert c2["event"]["id"] == eid

    # Repeated remember_closure calls must not inflate FAILED_IN records
    r1 = tb.remember_closure(eid)["result"]
    assert r1["already_remembered"] is False
    assert r1["failure_records_created"] == ["BR1"]
    assert r1["failure_records_total_for_event"] == 1
    first_remembered_at = r1["remembered_at"]

    for _ in range(5):
        dup = tb.remember_closure(eid)["result"]
        assert dup["already_remembered"] is True
        assert dup["failure_records_created"] == []
        assert dup["failure_records_already_present"] == ["BR1"]
        assert dup["failure_records_total_for_event"] == 1
        assert dup["remembered_at"] == first_remembered_at

    # Resolve the closure, then replay the exact same apply_closure with the same started_at timestamp
    tb.clear_closure(eid)
    replay = tb.apply_closure(
        segment_id="s2",
        description="Monsoon pier scour on Bridge 1",
        cause="flood",
        bridge_id="BR1",
        started_at="2026-07-20T06:00:00Z",
    )["result"]
    assert replay["created"] is False
    assert replay["event"]["id"] == eid

    # Check seasonal_risk counts for BR1: exactly 1 simulated event and 1 simulated bridge failure
    sr = tb.seasonal_risk("july")["result"]
    br1 = next(r for r in sr["ranked"] if r["kind"] == "bridge" and r["id"] == "BR1")
    assert br1["counts"]["simulated"] == 1
    assert br1["scoring_breakdown"]["bridge_failures_in_season"] == 2  # 1 observed (obs-2024-piphema) + 1 simulated


# ---------------------------------------------------------------------------
# 3. Simulated history is visibly labeled
# ---------------------------------------------------------------------------
def test_simulated_history_is_visibly_labeled():
    ds = _build_fixture_with_observed_history()
    graph = InMemoryGraph(ds)
    store = FalkorStore(graph)
    clock, id_factory = _make_clock_and_ids()
    tb = Toolbox(store, clock=clock, id_factory=id_factory)

    c = tb.apply_closure(
        segment_id="s2",
        description="Simulated July landslide at Bridge 1",
        cause="landslide",
        bridge_id="BR1",
        started_at="2026-07-10T10:00:00Z",
    )["result"]
    eid = c["event"]["id"]
    tb.remember_closure(eid)

    sr = tb.seasonal_risk(7)["result"]
    s2 = next(r for r in sr["ranked"] if r["kind"] == "segment" and r["id"] == "s2")

    assert len(s2["observed_in_season"]) == 1
    assert s2["observed_in_season"][0]["data_status"] == "observed"
    assert "OBSERVED" in s2["observed_in_season"][0]["history_label"]

    assert len(s2["simulated_in_season"]) == 1
    assert s2["simulated_in_season"][0]["id"] == eid
    assert s2["simulated_in_season"][0]["data_status"] == "simulated"
    assert "SIMULATED" in s2["simulated_in_season"][0]["history_label"]
    assert "[SIMULATED" in s2["evidence_summary"]


# ---------------------------------------------------------------------------
# 4. Seasonal rankings use actual stored history and risk attributes
# 5. Clearing a closure does not erase its historical record
# 6. Repeated failure events influence the ranking according to a documented scoring rule
# ---------------------------------------------------------------------------
def test_clearing_closure_preserves_history_and_repeated_failures_change_ranking():
    ds = build_synthetic_fixture_dataset()
    graph = InMemoryGraph(ds)
    store = FalkorStore(graph)
    clock, id_factory = _make_clock_and_ids()
    tb = Toolbox(store, clock=clock, id_factory=id_factory)

    # In baseline fixture: s4 and s5 both have monsoon_risk=None and 0 events, so neither is ranked
    base_sr = tb.seasonal_risk(7)["result"]
    base_ids = [r["id"] for r in base_sr["ranked"]]
    assert "s4" not in base_ids and "s5" not in base_ids

    # Apply 1 simulated monsoon closure on s4 and clear it -> s4 must still appear in seasonal_risk
    e_s4 = tb.apply_closure(
        segment_id="s4",
        description="Simulated slip on bypass s4",
        cause="landslide",
        started_at="2026-07-11T08:00:00Z",
    )["result"]["event"]["id"]
    clear_s4 = tb.clear_closure(e_s4)["result"]
    assert clear_s4["history_preserved"]["event_node_kept"] is True
    assert clear_s4["history_preserved"]["blocks_links_kept"] == 1

    sr_after_s4 = tb.seasonal_risk(7)["result"]
    s4_item = next(r for r in sr_after_s4["ranked"] if r["id"] == "s4")
    assert s4_item["counts"] == {"observed": 0, "simulated": 1}
    assert s4_item["simulated_in_season"][0]["status"] == "resolved"

    # Now apply and clear TWO distinct monsoon closures on s5 (repeated failure events)
    e_s5_1 = tb.apply_closure(
        segment_id="s5",
        description="First monsoon mudslide on s5",
        cause="landslide",
        started_at="2026-07-12T08:00:00Z",
    )["result"]["event"]["id"]
    tb.remember_closure(e_s5_1)
    tb.clear_closure(e_s5_1)

    e_s5_2 = tb.apply_closure(
        segment_id="s5",
        description="Second monsoon mudslide on s5",
        cause="landslide",
        started_at="2026-08-03T14:00:00Z",
    )["result"]["event"]["id"]
    tb.remember_closure(e_s5_2)
    tb.clear_closure(e_s5_2)

    # Verify audit_closure confirms network is back at baseline while history is kept
    audit = tb.audit_closure(e_s5_2)["result"]
    assert audit["returned_to_baseline"] is True

    # Verify repeated failure events on s5 rank s5 above s4 according to the documented scoring rule
    sr_after_s5 = tb.seasonal_risk(7)["result"]
    ranked_ids = [r["id"] for r in sr_after_s5["ranked"]]
    assert "s5" in ranked_ids and "s4" in ranked_ids
    idx_s5 = ranked_ids.index("s5")
    idx_s4 = ranked_ids.index("s4")
    assert idx_s5 < idx_s4, f"s5 (2 failures) must rank ahead of s4 (1 failure), got {ranked_ids}"

    s5_item = sr_after_s5["ranked"][idx_s5]
    s4_item_after = sr_after_s5["ranked"][idx_s4]
    assert s5_item["counts"]["simulated"] == 2
    assert s5_item["scoring_breakdown"]["remembered_in_season"] == 2
    assert s5_item["scoring_breakdown"]["composite_score"] > s4_item_after["scoring_breakdown"]["composite_score"]


# ---------------------------------------------------------------------------
# 7. The agent explains the evidence behind its warning (and obeys language rule)
# ---------------------------------------------------------------------------
def test_agent_explains_evidence_behind_seasonal_warning_without_future_failure_prediction():
    ds = _build_fixture_with_observed_history()
    graph = InMemoryGraph(ds)
    store = FalkorStore(graph)
    clock, id_factory = _make_clock_and_ids()
    agent = LandslideAgent(store, clock=clock, id_factory=id_factory)

    # First create & remember a simulated monsoon closure via agent workflow
    rep_closure = agent.run(
        "Landslide between Kohima and Charlie in July",
        started_at="2026-07-22T10:00:00Z",
        remember_in_history=True,
    )
    assert rep_closure["status"] == "completed"
    assert rep_closure["remembered_closure"] is not None
    assert rep_closure["seasonal_risk"] is not None

    # Now ask the agent for the July seasonal monsoon risk ranking
    rep_risk = agent.run("Show seasonal monsoon risk ranking for July")
    assert rep_risk["status"] == "completed"
    sr = rep_risk["seasonal_risk"]
    assert sr["month"] == 7
    assert len(sr["ranked"]) >= 2

    # Check that seasonal_risk_evidence claims exist and explain the evidence
    ev_claims = [c for c in rep_risk["claims"] if c["category"] == "seasonal_risk_evidence"]
    assert len(ev_claims) == len(sr["ranked"])
    top_claim = ev_claims[0]
    stmt_lower = top_claim["statement"].lower()
    assert "monsoon_risk=" in stmt_lower
    assert "observed in-season event" in stmt_lower
    assert "composite score=" in stmt_lower
    assert ("elevated modeled risk" in stmt_lower) or ("historical pattern warrants review" in stmt_lower)

    # Verify strict language rule: never claim a road or bridge "will fail"
    md_lower = rep_risk["markdown_report"].lower()
    assert ("elevated modeled risk" in md_lower) or ("historical pattern warrants review" in md_lower)
    for forbidden in ("will fail", "guaranteed to fail", "certain to fail", "will collapse"):
        assert forbidden not in md_lower
        for item in sr["ranked"]:
            assert forbidden not in item["advisory"].lower()
            assert forbidden not in item["evidence_summary"].lower()
