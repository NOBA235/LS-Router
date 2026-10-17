"""End-to-end HTTP tests for the Phase 5 FastAPI backend (`backend/app/main.py`).

Exercises all 8 required Phase 5 endpoints plus `/health`, OpenAPI schema generation,
CORS configuration, Pydantic validation, not-found (404), state-conflict (409), and
database-unavailable (503) responses against the deterministic `scenario_a` corridor graph.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_store
from app.graph.errors import StoreError
from app.graph.store import FalkorStore
from app.graph.tools import Toolbox
from app.main import SAFETY_DISCLAIMER, app, get_toolbox
from tests.fake_graph import FakeGraph
from tests.scenarios import scenario_a


@pytest.fixture
def fake():
    ds = scenario_a(
        events=[
            {
                "id": "obs-2023",
                "description": "Historical 2023 monsoon landslide near J1",
                "started_at": "2023-07-15",
                "segments": ["s2"],
                "near": ["J1"],
            }
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


# ---------------------------------------------------------------------------
# 1. OpenAPI & CORS
# ---------------------------------------------------------------------------
def test_openapi_schema_and_docs_expose_all_phase5_routes(client: TestClient):
    docs_res = client.get("/docs")
    assert docs_res.status_code == 200
    assert "Landslide Router API" in docs_res.text

    schema_res = client.get("/openapi.json")
    assert schema_res.status_code == 200
    schema = schema_res.json()
    paths = set(schema["paths"].keys())
    expected_paths = {
        "/health",
        "/graph",
        "/closure",
        "/cutoff/{event_id}",
        "/reroute",
        "/weak-links",
        "/seasonal-risk",
        "/alerts/{event_id}",
        "/resolve/{event_id}",
    }
    assert expected_paths.issubset(paths)


def test_cors_preflight_allows_local_vite_origin(client: TestClient):
    res = client.options(
        "/closure",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---------------------------------------------------------------------------
# 2. GET /graph
# ---------------------------------------------------------------------------
def test_get_graph_returns_entities_provenance_geometry_and_cypher(client: TestClient, fake):
    # Attach detail metadata to verify provenance and geometry parsing
    fake.places["D"].update(
        {
            "confidence": "verified",
            "source_id": "node/101",
            "retrieved_at": "2026-10-01T00:00:00Z",
            "last_verified": "2026-10-02",
            "population": 122000,
        }
    )
    fake.segments["s1"].update(
        {
            "source_id": "way/555",
            "retrieved_at": "2026-10-01T00:00:00Z",
            "last_verified": None,
            "surface": "asphalt",
            "geometry": "[[93.72, 25.90], [93.75, 25.85]]",
        }
    )

    res = client.get("/graph")
    assert res.status_code == 200
    body = res.json()

    assert body["disclaimer"] == SAFETY_DISCLAIMER
    assert body["corridors"] == ["corr-test"]
    assert body["counts"]["places"] == len(fake.places)
    assert body["counts"]["segments"] == len(fake.segments)
    assert body["counts"]["hospitals"] == len(fake.hospitals)
    assert body["counts"]["bridges"] == len(fake.bridges)
    assert body["counts"]["active_closures"] == 0
    assert body["counts"]["blocked_segments"] == 0
    assert len(body["cypher"]) > 0

    place_d = next(p for p in body["places"] if p["id"] == "D")
    assert place_d["population"] == 122000
    assert place_d["provenance"] == {
        "source_url": "https://example.test/place/D",
        "source_id": "node/101",
        "retrieved_at": "2026-10-01T00:00:00Z",
        "last_verified": "2026-10-02",
        "confidence": "verified",
        "data_status": "observed",
    }

    seg_s1 = next(s for s in body["segments"] if s["id"] == "s1")
    assert seg_s1["status"] == "open"
    assert seg_s1["surface"] == "asphalt"
    assert seg_s1["geometry"] == [[93.72, 25.9], [93.75, 25.85]]
    assert seg_s1["provenance"]["source_id"] == "way/555"

    # Segment without stored geometry stays None (never fabricated as a straight line)
    seg_s7 = next(s for s in body["segments"] if s["id"] == "s7")
    assert seg_s7["geometry"] is None

    # Excluding geometry sets all segment geometries to None
    res_no_geom = client.get("/graph", params={"include_geometry": "false"})
    assert res_no_geom.status_code == 200
    assert all(s["geometry"] is None for s in res_no_geom.json()["segments"])


# ---------------------------------------------------------------------------
# 3. POST /closure (Ambiguous, No-Match, Unambiguous, Confirmed, Idempotent)
# ---------------------------------------------------------------------------
def test_post_closure_ambiguous_does_not_mutate_graph(client: TestClient):
    res = client.post(
        "/closure",
        json={"description": "Landslide at NH29 near kilometre 42."},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "clarification_required"
    assert body["requires_clarification"] is True
    assert body["created"] is False
    assert body["event_id"] is None
    assert body["closure"] is None
    assert len(body["resolution"]["candidate_segments"]) > 0
    assert body["resolution"]["clarification_prompt"]
    assert len(body["cypher"]) > 0

    # Verify the graph was NOT mutated
    g = client.get("/graph").json()
    assert g["counts"]["active_closures"] == 0
    assert g["counts"]["blocked_segments"] == 0


def test_post_closure_coordinate_only_requires_clarification(client: TestClient):
    res = client.post(
        "/closure",
        json={
            "description": "Mudslide reported near coordinates",
            "lat": 10.0,
            "lon": 10.0,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "clarification_required"
    assert body["requires_clarification"] is True
    assert body["created"] is False
    assert len(body["resolution"]["candidate_segments"]) > 0


def test_post_closure_no_match_does_not_mutate_graph(client: TestClient):
    res = client.post(
        "/closure",
        json={"description": "Landslide near Wakanda"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "no_match"
    assert body["requires_clarification"] is False
    assert body["created"] is False
    assert body["event_id"] is None
    assert body["closure"] is None


def test_post_closure_unambiguous_and_idempotent_and_redacts_pii(client: TestClient):
    res = client.post(
        "/closure",
        json={
            "description": "Landslide between Kohima and Charlie reported by caller +91 9876543210",
            "remember_in_history": True,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "applied"
    assert body["requires_clarification"] is False
    assert body["created"] is True
    assert body["event_id"] == "sim-e1"
    assert body["event"]["status"] == "active"
    assert body["event"]["data_status"] == "simulated"
    assert "+91 9876543210" not in body["event"]["description"]
    assert "[REDACTED_PHONE]" in body["event"]["description"]
    assert body["target_segment"]["id"] == "s7"
    assert body["state_change"]["newly_blocked_segments"] == ["s7"]
    assert body["remembered"] is not None
    assert body["remembered"]["remembered_at"] is not None
    assert len(body["cypher"]) > 0
    assert body["disclaimer"] == SAFETY_DISCLAIMER

    # Idempotent repeat call does not create a duplicate event
    res2 = client.post(
        "/closure",
        json={"description": "Landslide between Kohima and Charlie reported by caller +91 9876543210"},
    )
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["status"] == "applied"
    assert body2["created"] is False
    assert body2["event_id"] == "sim-e1"

    # Graph view now shows segment s7 and its connections as blocked
    g = client.get("/graph").json()
    assert g["counts"]["active_closures"] == 1
    assert g["counts"]["blocked_segments"] == 1
    seg_s7 = next(s for s in g["segments"] if s["id"] == "s7")
    assert seg_s7["status"] == "blocked"
    assert seg_s7["blocked_by"] == ["sim-e1"]


def test_post_closure_with_explicit_target_confirmation(client: TestClient):
    res = client.post(
        "/closure",
        json={
            "description": "Bridge washout at NH29 kilometre 42",
            "target_segment_id": "s2",
            "target_bridge_id": "BR1",
            "cause": "washout",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "applied"
    assert body["created"] is True
    assert body["event"]["cause"] == "washout"
    assert body["event"]["blocks_segments"] == ["s2"]
    assert body["event"]["blocks_bridges"] == ["BR1"]


# ---------------------------------------------------------------------------
# 4. GET /cutoff/{event_id}
# ---------------------------------------------------------------------------
def test_get_cutoff_returns_cut_off_settlements_and_hospital_impact(client: TestClient):
    c_res = client.post(
        "/closure",
        json={"description": "Landslide between J2 and Kohima", "target_segment_id": "s3"},
    ).json()
    event_id = c_res["event_id"]

    res = client.get(f"/cutoff/{event_id}", params={"threshold_min": 5.0})
    assert res.status_code == 200
    body = res.json()

    assert body["event_id"] == event_id
    assert body["newly_unreachable"]["count"] == 3
    village_ids = [v["id"] for v in body["newly_unreachable"]["villages"]]
    town_ids = [t["id"] for t in body["newly_unreachable"]["towns"]]
    assert village_ids == ["C", "E"]
    assert town_ids == ["K"]
    assert body["attributable_to_this_event"] == ["C", "E", "K"]
    assert len(body["cut_boundary"]) == 1
    assert body["cut_boundary"][0]["segment_id"] == "s3"

    hosp = body["hospital_reachability"]
    assert hosp["affected_settlements"] == ["C", "E", "K"]
    assert hosp["threshold_min"] == 5.0
    assert "HJ" in hosp["hospitals_with_no_usable_route_from_any_affected_settlement"]
    assert len(body["cypher"]) > 0
    assert body["disclaimer"] == SAFETY_DISCLAIMER


# ---------------------------------------------------------------------------
# 5. GET /reroute
# ---------------------------------------------------------------------------
def test_get_reroute_computes_detour_and_path_explanation(client: TestClient):
    # Block primary NH29 segment s2 (20 min) so D -> K detours via Bravo (s4=15 + s5=25 = 40 min, +20 min)
    c_res = client.post(
        "/closure",
        json={"description": "Landslide on NH29", "target_segment_id": "s2"},
    ).json()
    event_id = c_res["event_id"]

    # Test with `from` and `to` query parameters
    res = client.get("/reroute", params={"from": "D", "to": "K", "avoid_event_id": event_id})
    assert res.status_code == 200
    body = res.json()

    assert body["status"] == "route_found"
    assert body["from_place_id"] == "D"
    assert body["to_place_id"] == "K"
    assert body["route"]["total_travel_time_min"] == 60.0
    assert body["baseline_route"]["total_travel_time_min"] == 40.0
    assert body["additional_travel_time_min"] == 20.0
    seg_ids = [s["segment_id"] for s in body["route"]["segments"]]
    assert seg_ids == ["s1", "s4", "s5", "s3"]

    # Verify explain_path breakdown is included
    expl = body["path_explanation"]
    assert expl["status"] == "route_found"
    assert expl["checks"]["segment_times_sum_to_total"] is True
    assert expl["checks"]["no_selected_segment_is_blocked"] is True
    assert len(expl["contributions"]) == 4

    # Also test with `from_place_id` and `to_place_id` parameter aliases
    res2 = client.get("/reroute", params={"from_place_id": "D", "to_place_id": "K"})
    assert res2.status_code == 200
    assert res2.json()["route"]["total_travel_time_min"] == 60.0


def test_get_reroute_reports_no_route_when_cut_off(client: TestClient):
    client.post("/closure", json={"description": "Landslide on segment s3", "target_segment_id": "s3"})
    res = client.get("/reroute", params={"from": "D", "to": "K"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "no_route"
    assert body["route"] is None
    assert body["path_explanation"]["status"] == "no_route"


# ---------------------------------------------------------------------------
# 6. GET /weak-links
# ---------------------------------------------------------------------------
def test_get_weak_links_returns_centrality_ranking(client: TestClient):
    # Default corridor auto-resolution
    res = client.get("/weak-links", params={"top_k": 3})
    assert res.status_code == 200
    body = res.json()
    assert body["corridor_id"] == "corr-test"
    assert body["top_k"] == 3
    assert len(body["segments"]) == 3
    assert body["segments"][0]["rank"] == 1
    assert "betweenness" in body["segments"][0]
    assert "if_closed" in body["segments"][0]
    assert len(body["bridges"]) >= 1
    assert len(body["places"]) >= 1
    assert len(body["cypher"]) > 0
    assert body["disclaimer"] == SAFETY_DISCLAIMER


# ---------------------------------------------------------------------------
# 7. GET /seasonal-risk
# ---------------------------------------------------------------------------
def test_get_seasonal_risk_returns_documented_ranking(client: TestClient):
    res = client.get("/seasonal-risk", params={"month": "july"})
    assert res.status_code == 200
    body = res.json()
    assert body["month"] == 7
    assert len(body["seasons"]) == 1
    assert body["seasons"][0]["id"] == "monsoon"
    assert body["ranking_method"]
    assert len(body["ranked"]) > 0
    top = body["ranked"][0]
    assert top["id"] == "s2"
    assert top["monsoon_risk"] == "high"
    assert len(top["observed_in_season"]) == 1
    assert "simulated_in_season" in top
    assert body["disclaimer"] == SAFETY_DISCLAIMER


# ---------------------------------------------------------------------------
# 8. POST /alerts/{event_id}
# ---------------------------------------------------------------------------
def test_post_alerts_creates_synthetic_sms_drafts_only(client: TestClient):
    c_res = client.post(
        "/closure",
        json={"description": "Landslide between Kohima and Charlie", "target_segment_id": "s7"},
    ).json()
    event_id = c_res["event_id"]

    res = client.post(f"/alerts/{event_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["event_id"] == event_id
    assert body["settlements_considered"] == ["C", "E"]
    assert len(body["alerts"]) == 2
    for alert in body["alerts"]:
        assert alert["status"] == "draft"
        assert alert["sent"] is False
        assert alert["channel"] == "sms-draft"
        assert alert["text"].startswith("SIMULATION")
        assert alert["supporting"]["closure_event_id"] == event_id
    assert body["disclaimer"] == SAFETY_DISCLAIMER


# ---------------------------------------------------------------------------
# 9. POST /resolve/{event_id}
# ---------------------------------------------------------------------------
def test_post_resolve_clears_closure_and_audits_baseline_return(client: TestClient):
    c_res = client.post(
        "/closure",
        json={"description": "Landslide between Kohima and Charlie", "target_segment_id": "s7"},
    ).json()
    event_id = c_res["event_id"]

    res = client.post(f"/resolve/{event_id}", json={"remember_in_history": True})
    assert res.status_code == 200
    body = res.json()
    assert body["event_id"] == event_id
    assert body["resolved"] is True
    assert body["returned_to_baseline"] is True
    assert body["restored_segments"] == ["s7"]
    assert body["history_preserved"]["event_node_kept"] is True
    assert body["remembered"]["remembered_at"] is not None
    assert body["audit"]["discrepancies"] == []
    assert len(body["cypher"]) > 0
    assert body["disclaimer"] == SAFETY_DISCLAIMER


# ---------------------------------------------------------------------------
# 10. Validation (422), Not Found (404), Conflict (409), Store Failure (503)
# ---------------------------------------------------------------------------
def test_validation_and_domain_error_status_codes(client: TestClient):
    # 422: lat without lon
    r1 = client.post("/closure", json={"description": "Landslide", "lat": 25.8})
    assert r1.status_code == 422
    assert r1.json()["error"]["code"] == "validation_error"

    # 422: missing `from` / `to` on /reroute
    r2 = client.get("/reroute", params={"from": "D"})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "invalid_input"

    # 422: invalid month on /seasonal-risk
    r3 = client.get("/seasonal-risk", params={"month": "13"})
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "invalid_input"

    # 404: unknown event_id on /cutoff, /alerts, /resolve
    for method, path in [
        ("GET", "/cutoff/sim-does-not-exist"),
        ("POST", "/alerts/sim-does-not-exist"),
        ("POST", "/resolve/sim-does-not-exist"),
    ]:
        resp = client.request(method, path)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    # 404: unknown place on /reroute, unknown corridor on /weak-links, unknown target_segment_id on /closure
    assert client.get("/reroute", params={"from": "UNKNOWN", "to": "K"}).status_code == 404
    assert client.get("/weak-links", params={"corridor_id": "unknown-corridor"}).status_code == 404
    assert (
        client.post(
            "/closure",
            json={"description": "Landslide", "target_segment_id": "seg-does-not-exist"},
        ).status_code
        == 404
    )

    # 409: attempting to resolve an observed historical event (obs-2023)
    r_conflict = client.post("/resolve/obs-2023")
    assert r_conflict.status_code == 409
    assert r_conflict.json()["error"]["code"] == "invalid_state"


def test_store_error_returns_503_service_unavailable():
    def _broken_store():
        raise StoreError("FalkorDB connection refused on localhost:6379")

    app.dependency_overrides[get_store] = _broken_store
    try:
        with TestClient(app) as c:
            res = c.get("/graph")
            assert res.status_code == 503
            body = res.json()
            assert body["error"]["code"] == "store_error"
            assert "FalkorDB connection refused" in body["error"]["message"]
    finally:
        app.dependency_overrides.clear()
