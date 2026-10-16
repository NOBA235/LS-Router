"""Needs a running FalkorDB. Seeds the SYNTHETIC dataset into a throwaway graph, never into 'landslide'."""
import os

import pytest

import seed
import synthetic_corridor as S

GRAPH = "landslide_test"


@pytest.mark.integration
def test_seed_twice_creates_nothing_new_and_invariants_hold():
    falkordb = pytest.importorskip("falkordb")
    try:
        client = falkordb.FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"),
                                   port=int(os.environ.get("FALKORDB_PORT", "6379")), socket_connect_timeout=2)
        graph = client.select_graph(GRAPH)
        graph.query("RETURN 1")
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")
    ds = S.synthetic_dataset()
    try:
        graph.query("MATCH (n) DETACH DELETE n")
        first = seed.seed_graph(graph, ds, log=lambda *_: None)
        assert first["nodes_created"] > 0 and first["relationships_created"] > 0
        second = seed.seed_graph(graph, ds, log=lambda *_: None)
        assert second["nodes_created"] == 0 and second["relationships_created"] == 0
        failed = [(n, d) for n, ok, d in seed.verify_graph(graph, ds) if not ok]
        assert failed == []
    finally:
        try:
            graph.delete()
        except Exception:
            pass
