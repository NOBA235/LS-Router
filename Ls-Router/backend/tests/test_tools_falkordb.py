"""The same scenario checks as test_tools_fake.py, against a REAL FalkorDB (graph 'landslide_test_tools', wiped each time).
Skipped when FalkorDB is not reachable. This is what proves that FalkorDB accepts the Cypher in app/graph/queries.py."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import seed  # noqa: E402
import tool_checks  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402
from app.graph.tools import Toolbox  # noqa: E402

GRAPH = "landslide_test_tools"


def _graph():
    falkordb = pytest.importorskip("falkordb")
    try:
        client = falkordb.FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"), port=int(os.environ.get("FALKORDB_PORT", "6379")),
                                   socket_connect_timeout=2)
        graph = client.select_graph(GRAPH)
        graph.query("RETURN 1")
        return graph
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable: {exc}")


class LiveWorld:
    fake = None

    def __init__(self, ds, clock="2026-10-14T12:00:%02dZ", procedures=()):
        self.graph = _graph()
        self.graph.query("MATCH (n) DETACH DELETE n")
        seed.seed_graph(self.graph, ds, log=lambda *_: None)
        self._n = self._t = 0
        self._fmt = clock
        self.tb = Toolbox(FalkorStore(self.graph), clock=self._clock, id_factory=self._id)

    def _id(self):
        self._n += 1
        return f"e{self._n}"

    def _clock(self):
        self._t += 1
        return self._fmt % self._t


def make(ds, clock="2026-10-14T12:00:%02dZ", procedures=()):
    return LiveWorld(ds, clock, procedures)


def _generate():
    for name, fn, fake_only in tool_checks.CHECKS:
        if fake_only:
            continue

        def test(fn=fn):
            fn(make)
        test.__name__ = test.__qualname__ = f"test_live_{name}"
        globals()[test.__name__] = pytest.mark.integration(test)


_generate()
