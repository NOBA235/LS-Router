"""Phase 8 tests: timeline, replay from stored events, alert-plan export, and route comparison."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_store
from app.graph.store import FalkorStore
from app.graph.tools import Toolbox
from app.main import app, get_toolbox
from tests.fake_graph import FakeGraph
from tests.scenarios import scenario_a


@pytest.fixture
def fake():
    ds = scenario_a(
        events=[
            {
                "id": "obs-2022",
                "description": "Historical 2022 rockfall near J1",
                "started_at": "2022-08-10",
                "segments": ["s2"],
                "near": ["J1"],
            },
            {
                "id": "obs-2023",
                "description": "Historical 2023 monsoon landslide near J2 cutting off Kohima",
                "started_at": "2023-07-15",
                "segments": ["s3"],
                "near": ["J2"],
            },
        ]
    )
    ds["segments"][1]["monsoon_risk"] = "high"  # s2
    return FakeGraph(ds)


@pytest.fixture
def store(fake):
    return FalkorStore(fake)


@pytest.fixture
def toolbox(store):
    n = 0
    t = 0

    def _id() -> str:
        nonlocal n
        n += 1
        return f"e{n}"

    def _clock() -> str:
        nonlocal t
        t += 1
        return f"2026-10-14T12:00:{t:02d}Z"

    return Toolbox(store, clock=_clock, id_factory=_id)


@pytest.fixture
def client(store, toolbox):
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_toolbox] = lambda: toolbox
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_history_timeline_from_real_stored_events(client: TestClient) -> None:
    """GET /history returns a chronologically ordered timeline with segment and bridge details."""
    res = client.get("/history")
    assert res.status_code == 200
    body = res.json()
    assert "observed_events" in body
    assert "simulated_events" in body
    assert "timeline" in body
    timeline = body["timeline"]
    assert len(timeline) >= 2
    for idx, entry in enumerate(timeline, start=1):
        assert entry["index"] == idx
        assert "id" in entry
        assert "segment_details" in entry
        assert "bridge_details" in entry
        assert "failed_bridges" in entry
        assert isinstance(entry["segment_details"], list)
    dates = [e["started_at"] for e in timeline if e.get("started_at")]
    assert dates == sorted(dates)


def test_timeline_replay_observed_and_resolved_events_without_mutating_active_state(
    client: TestClient,
) -> None:
    """Replaying an observed or resolved event evaluates its impact without altering active closures."""
    # 1. Replay a stored historical (observed) event that is currently inactive
    replay_res = client.get("/replay/obs-2023")
    assert replay_res.status_code == 200
    data = replay_res.json()
    assert data["replay_mode"] is True
    assert data["event"]["id"] == "obs-2023"
    assert data["event"]["data_status"] == "observed"
    assert "s3" in data["event"]["blocks_segments"]
    assert "reroute" in data
    assert "baseline_comparison" in data["reroute"]["path_explanation"]
    assert data["alerts"] is not None
    assert len(data["alerts"]["alerts"]) >= 1
    for alert in data["alerts"]["alerts"]:
        assert alert["status"] == "draft"
        assert alert["sent"] is False

    # Verify active closures on the live graph were NOT mutated by replay
    graph_res = client.get("/graph")
    assert graph_res.status_code == 200
    active_closures = [c for c in graph_res.json()["closures"] if c["status"] == "active"]
    assert active_closures == []

    # 2. Create a simulated closure on s2, resolve it, and then replay the resolved event
    sim = client.post(
        "/closure",
        json={
            "description": "Landslide near NH29 kilometre 42",
            "target_segment_id": "s2",
            "cause": "landslide",
        },
    )
    assert sim.status_code == 200
    ev_id = sim.json()["event_id"]

    resolve_res = client.post(f"/resolve/{ev_id}", json={"remember_in_history": True})
    assert resolve_res.status_code == 200

    replay_sim = client.post(f"/replay/{ev_id}")
    assert replay_sim.status_code == 200
    r_data = replay_sim.json()
    assert r_data["replay_mode"] is True
    assert r_data["event"]["id"] == ev_id
    assert r_data["event"]["status"] == "resolved"
    assert "s2" in r_data["event"]["blocks_segments"]
    comp = r_data["reroute"]["path_explanation"]["baseline_comparison"]
    assert comp["same_as_baseline"] is False
    assert "s2" in comp["segments_avoided_from_baseline"]


def test_alert_plan_export_json_and_text_draft_only(client: TestClient) -> None:
    """GET /alerts/{event_id}/export supports JSON and plain-text formats with draft-only guardrails."""
    json_res = client.get(
        "/alerts/obs-2023/export",
        params={"format": "json", "replay": "true"},
    )
    assert json_res.status_code == 200
    payload = json_res.json()
    assert payload["event_id"] == "obs-2023"
    assert payload["export_format"] == "json"
    assert payload["all_drafts"] is True
    assert payload["any_sent"] is False
    assert len(payload["alerts"]) >= 1
    assert "SYNTHETIC COMMUNITY ALERT PLAN (DRAFT ONLY — NOT SENT)" in payload["text_plan"]
    for alert in payload["alerts"]:
        assert alert["status"] == "draft"
        assert alert["sent"] is False

    txt_res = client.get(
        "/alerts/obs-2023/export",
        params={"format": "text", "replay": "true"},
    )
    assert txt_res.status_code == 200
    assert "text/plain" in txt_res.headers.get("content-type", "")
    assert "draft-alerts-obs-2023.txt" in txt_res.headers.get("content-disposition", "")
    text_body = txt_res.text
    assert "SYNTHETIC COMMUNITY ALERT PLAN (DRAFT ONLY — NOT SENT)" in text_body
    assert "Status: draft | Sent: False" in text_body


def test_alternative_route_baseline_comparison(client: TestClient) -> None:
    """explain_path includes baseline_comparison showing avoided baseline segments and added detour segments."""
    # Baseline route when nothing is blocked
    base_res = client.get(
        "/reroute",
        params={"from": "D", "to": "K"},
    )
    assert base_res.status_code == 200
    base_comp = base_res.json()["path_explanation"]["baseline_comparison"]
    assert base_comp["same_as_baseline"] is True
    assert base_comp["detour_travel_time_delta_min"] == 0.0
    assert base_comp["detour_length_delta_km"] == 0.0
    assert base_comp["segments_avoided_from_baseline"] == []
    assert base_comp["segments_added_in_detour"] == []

    # Block trunk segment s2 and verify side-by-side comparison metrics
    sim_res = client.post(
        "/closure",
        json={
            "description": "Landslide on s2",
            "target_segment_id": "s2",
            "cause": "landslide",
        },
    )
    assert sim_res.status_code == 200
    ev_id = sim_res.json()["event_id"]

    detour_res = client.get(
        "/reroute",
        params={"from": "D", "to": "K", "avoid_event_id": ev_id},
    )
    assert detour_res.status_code == 200
    detour_comp = detour_res.json()["path_explanation"]["baseline_comparison"]
    assert detour_comp["same_as_baseline"] is False
    assert detour_comp["baseline_path"] is not None
    assert detour_comp["detour_travel_time_delta_min"] == 20.0
    assert "s2" in detour_comp["segments_avoided_from_baseline"]
    assert detour_comp["segments_added_in_detour"] == ["s4", "s5"]
