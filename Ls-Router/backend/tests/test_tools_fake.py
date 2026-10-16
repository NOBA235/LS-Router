"""Runs every scenario check against the in-memory FakeGraph (no database needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import tool_checks  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402
from app.graph.tools import Toolbox  # noqa: E402
from fake_graph import FakeGraph  # noqa: E402


class World:
    def __init__(self, ds, clock="2026-10-14T12:00:%02dZ", procedures=()):
        self.fake = FakeGraph(ds, procedures)
        self._n = self._t = 0
        self._clock_fmt = clock
        self.tb = Toolbox(FalkorStore(self.fake), clock=self._clock, id_factory=self._id)

    def _id(self):
        self._n += 1
        return f"e{self._n}"

    def _clock(self):
        self._t += 1
        return self._clock_fmt % self._t


def make(ds, clock="2026-10-14T12:00:%02dZ", procedures=()):
    return World(ds, clock, procedures)


def _generate():
    for name, fn, _fake_only in tool_checks.CHECKS:
        def test(fn=fn):
            fn(make)
        test.__name__ = test.__qualname__ = f"test_{name}"
        test.__doc__ = fn.__doc__
        globals()[test.__name__] = test


_generate()
