import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import check_falkordb
from app.main import app, falkordb_status

OK = {"connected": True, "graph": "landslide", "query_ok": True, "latency_ms": 1.0, "error": None}
DOWN = {"connected": False, "graph": "landslide", "query_ok": False, "latency_ms": 1.0, "error": "ConnectionError: refused"}


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_ok_when_database_answers(client):
    app.dependency_overrides[falkordb_status] = lambda: OK
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["falkordb"]["query_ok"] is True


def test_health_503_when_database_down(client):
    app.dependency_overrides[falkordb_status] = lambda: DOWN
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["falkordb"]["error"]


def test_check_falkordb_reports_error_for_closed_port():
    status = check_falkordb(Settings(falkordb_host="127.0.0.1", falkordb_port=1, falkordb_timeout_s=0.5))
    assert status["connected"] is False
    assert status["query_ok"] is False
    assert status["error"]


@pytest.mark.integration
def test_live_falkordb_query():
    status = check_falkordb(get_settings())
    if not status["connected"]:
        pytest.skip(f"FalkorDB not reachable: {status['error']}")
    assert status["query_ok"] is True
