"""Phase 9 tests: reproducible 12-step demo runner, clean reset procedure, and deliverables."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_store
from app.demo import reset_demo_state, run_twelve_step_demo
from app.graph.store import FalkorStore
from app.graph.tools import Toolbox
from app.main import app, get_toolbox
from tests.fake_graph import FakeGraph
from tests.scenarios import scenario_a

REPO_ROOT = Path(__file__).resolve().parents[2]
NORMALIZED_CACHE = REPO_ROOT / "data" / "cache" / "normalized.json"


def _make_scenario_store() -> FalkorStore:
    return FalkorStore(FakeGraph(scenario_a()))


def test_reset_demo_state_clears_simulated_closures_and_audits_baseline() -> None:
    store = _make_scenario_store()
    tb = Toolbox(store)
    applied = tb.apply_closure(segment_id="s2", description="Test closure")["result"]
    assert applied["event"]["status"] == "active"

    res = reset_demo_state(store)
    assert res["status"] == "reset_complete"
    assert res["resolved_count"] == 1
    assert applied["event"]["id"] in res["resolved_event_ids"]
    assert res["remaining_active_closures"] == []
    assert res["back_to_baseline"] is True


def test_run_twelve_step_demo_on_synthetic_fixture() -> None:
    store = _make_scenario_store()
    report = run_twelve_step_demo(store, include_weak_links=True)

    assert report["status"] == "completed"
    assert report["step_count"] == 12
    assert len(report["steps"]) == 12
    assert report["under_three_minutes"] is True
    assert report["total_elapsed_seconds"] < 180.0
    assert report["back_to_baseline"] is True
    assert report["mcp_handoff_count"] >= 14

    step_nums = [s["step"] for s in report["steps"]]
    assert step_nums == list(range(1, 13))

    # Step 8 alerts should all be drafts with sent=False
    s8_data = report["steps"][7]["data"]
    assert s8_data["alert_count"] >= 1
    assert s8_data["all_drafts"] is True
    for alert in s8_data["alerts"]:
        assert alert["status"] == "draft"
        assert alert["sent"] is False


def test_api_demo_run_and_reset_endpoints() -> None:
    store = _make_scenario_store()
    tb = Toolbox(store)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_toolbox] = lambda: tb
    try:
        with TestClient(app) as client:
            run_resp = client.post("/demo/run", json={"include_weak_links": True})
            assert run_resp.status_code == 200
            payload = run_resp.json()
            assert payload["status"] == "completed"
            assert payload["step_count"] == 12
            assert payload["under_three_minutes"] is True
            assert payload["back_to_baseline"] is True

            reset_resp = client.post("/demo/reset")
            assert reset_resp.status_code == 200
            reset_payload = reset_resp.json()
            assert reset_payload["status"] == "reset_complete"
            assert reset_payload["back_to_baseline"] is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.skipif(not NORMALIZED_CACHE.exists(), reason="Real OSM cache normalized.json not present")
def test_run_twelve_step_demo_on_real_nh29_graph() -> None:
    ds = json.loads(NORMALIZED_CACHE.read_text(encoding="utf-8"))
    store = FalkorStore(FakeGraph(ds))
    report = run_twelve_step_demo(store, include_weak_links=False)

    assert report["status"] == "completed"
    assert report["step_count"] == 12
    assert report["under_three_minutes"] is True
    assert report["back_to_baseline"] is True

    # Step 1: Verify real OSM NH29 graph inventory
    s1 = report["steps"][0]["data"]
    assert s1["counts"]["places"] == 814
    assert s1["counts"]["segments"] == 1045
    assert s1["counts"]["bridges"] == 126
    assert s1["counts"]["hospitals"] == 24
    assert s1["counts"]["stored_events"] == 7
    assert s1["baseline_route"]["total_travel_time_min"] == pytest.approx(125.2, abs=0.1)
    assert s1["baseline_route"]["total_length_km"] == pytest.approx(70.101, abs=0.1)

    # Step 2 & 3: Ambiguous resolution followed by confirmed target resolution
    s2 = report["steps"][1]["data"]
    assert s2["status"] == "ambiguous"
    assert s2["requires_clarification"] is True
    assert len(s2["candidate_segments"]) == 5

    s3 = report["steps"][2]["data"]
    assert s3["detour_target"]["segment_id"] == "seg-569069510-569069527-1353575333"
    assert s3["cutoff_target"]["segment_id"] == "seg-2469699767-6286548534-239157848"

    # Step 5: Exact settlement reachability changes (3 settlements: Kezo Town, Kikruma, Chakhabama)
    s5 = report["steps"][4]["data"]
    assert s5["newly_unreachable_settlement_count"] == 3
    assert set(s5["newly_unreachable_settlements"]) == {"Kezo Town", "Kikruma", "Chakhabama"}

    # Step 6 & 7: Exact detour routing across NH29
    s7 = report["steps"][6]["data"]
    assert s7["baseline_total_travel_time_min"] == pytest.approx(125.2, abs=0.1)
    assert s7["detour_total_travel_time_min"] == pytest.approx(215.23, abs=0.1)
    assert s7["detour_travel_time_delta_min"] == pytest.approx(90.03, abs=0.1)
    assert s7["detour_length_delta_km"] == pytest.approx(30.41, abs=0.1)

    # Step 8: 3 synthetic SMS alert drafts
    s8 = report["steps"][7]["data"]
    assert s8["alert_count"] == 3
    assert s8["all_drafts"] is True
    for draft in s8["alerts"]:
        assert draft["status"] == "draft"
        assert draft["sent"] is False


def test_phase9_deliverable_files_exist_and_nonempty() -> None:
    expected_files = [
        REPO_ROOT / "docs" / "PHASE9.md",
        REPO_ROOT / "docs" / "JUDGES_GUIDE.md",
        REPO_ROOT / "docs" / "BLOG_POST_DRAFT.md",
        REPO_ROOT / "docs" / "PITCH_DECK_OUTLINE.md",
        REPO_ROOT / "scripts" / "run_demo.py",
        REPO_ROOT / "scripts" / "reset_demo.py",
        REPO_ROOT / "backend" / "app" / "demo.py",
    ]
    for path in expected_files:
        assert path.exists(), f"Missing Phase 9 deliverable: {path}"
        content = path.read_text(encoding="utf-8")
        assert len(content.strip()) > 200, f"Deliverable {path} is unexpectedly short"
