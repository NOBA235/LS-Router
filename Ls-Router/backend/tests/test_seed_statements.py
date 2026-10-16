"""Seed script tests that need no database: statement building, batching, guards, replay stability."""
import json
import re
import tempfile
from pathlib import Path

import seed
import synthetic_corridor as S
from pipeline_config import PRIMARY_GRAPH


def ds():
    return S.synthetic_dataset()


def walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    else:
        yield obj


def rows(statements, name):
    return [r for s in statements if s["name"] == name for r in s["params"]["rows"]]


def test_every_write_is_a_merge_and_nothing_deletes_or_creates_blindly():
    for s in seed.build_statements(ds()):
        assert "MERGE" in s["query"] and "CREATE" not in s["query"] and "DELETE" not in s["query"]


def test_statements_are_replay_stable():
    assert seed.build_statements(ds()) == seed.build_statements(ds())


def test_no_null_values_are_sent():
    assert all(v is not None for s in seed.build_statements(ds()) for v in walk(s["params"]))


def test_row_counts_match_the_dataset():
    d, st = ds(), seed.build_statements(ds())
    assert len(rows(st, "nodes:Place")) == len(d["places"]) == 8
    assert len(rows(st, "nodes:RoadSegment")) == 9 and len(rows(st, "rels:CONNECTED_BY")) == 17
    assert len(rows(st, "rels:SEGMENT_OF")) == 9 and len(rows(st, "rels:ON")) == 1 and len(rows(st, "rels:LOCATED_AT")) == 1
    assert len(rows(st, "rels:AFFECTS")) == 9
    assert seed.expected_counts(d)["Place"] == 8


def test_connected_by_is_keyed_on_segment_id_and_points_at_real_nodes():
    st = seed.build_statements(ds())
    q = next(s["query"] for s in st if s["name"] == "rels:CONNECTED_BY")
    assert "{segment_id: row.k}" in q
    seg_ids = {r["id"] for r in rows(st, "nodes:RoadSegment")}
    place_ids = {r["id"] for r in rows(st, "nodes:Place")}
    for r in rows(st, "rels:CONNECTED_BY"):
        assert r["k"] == r["props"]["segment_id"] and r["k"] in seg_ids
        assert r["a"] in place_ids and r["b"] in place_ids
    for r in rows(st, "rels:ON"):
        assert r["a"] in {x["id"] for x in rows(st, "nodes:Bridge")} and r["b"] in seg_ids


def test_batches_respect_row_and_size_limits():
    small = [{"id": i} for i in range(600)]
    assert [len(b) for b in seed._batches(small)] == [250, 250, 100]
    big = [{"id": i, "g": "x" * 100_000} for i in range(3)]
    assert [len(b) for b in seed._batches(big)] == [1, 1, 1]


def test_clean_drops_unknowns_and_rejects_nested_values():
    assert seed._clean({"id": "a", "x": None, "y": 0, "z": False}) == {"id": "a", "y": 0, "z": False}
    try:
        seed._clean({"id": "a", "bad": {"k": 1}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


class FakeGraph:
    def __init__(self, index_error=None):
        self.queries, self.index_error = [], index_error

    def query(self, q, params=None):
        self.queries.append((q, params))
        if q.startswith("CREATE INDEX") and self.index_error:
            raise Exception(self.index_error)
        return type("R", (), {"nodes_created": 0, "relationships_created": 0, "properties_set": 0, "result_set": []})()


def test_seed_tolerates_existing_indexes_but_not_other_errors():
    g = FakeGraph(index_error="Attribute 'id' is already indexed")
    seed.seed_graph(g, ds(), log=lambda *_: None)
    assert [q for q, _ in g.queries[:8]] == seed.INDEX_STATEMENTS
    try:
        seed.seed_graph(FakeGraph(index_error="syntax error"), ds(), log=lambda *_: None)
    except Exception as exc:
        assert "syntax" in str(exc)
    else:
        raise AssertionError("expected the error to propagate")


def test_replay_sends_identical_statements():
    a, b = FakeGraph(), FakeGraph()
    seed.seed_graph(a, ds(), log=lambda *_: None)
    seed.seed_graph(b, ds(), log=lambda *_: None)
    assert a.queries == b.queries


class CountStub:
    """Answers verify_graph's queries from the dataset, to test the check logic (not Cypher semantics)."""

    def __init__(self, d, wrong=None):
        self.d, self.wrong = d, wrong

    def query(self, q, params=None):
        want = seed.expected_counts(self.d)
        m = re.match(r"MATCH \(n:(\w+)(?: \{origin[^}]*\})?\) RETURN count\(n\)", q)
        if m:
            v = want[m.group(1)] + (1 if self.wrong == m.group(1) else 0)
        elif "CONNECTED_BY]->() RETURN count(c)" in q and "WHERE" not in q:
            v = len(self.d["connections"])
        else:
            v = 0
        return type("R", (), {"result_set": [[v]]})()


def test_verify_graph_passes_when_graph_matches_and_flags_a_mismatch():
    d = ds()
    assert all(ok for _, ok, _ in seed.verify_graph(CountStub(d), d))
    bad = seed.verify_graph(CountStub(d, wrong="Place"), d)
    assert [n for n, ok, _ in bad if not ok] == ["count Place"]


def write(d):
    p = Path(tempfile.mkdtemp()) / "normalized.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def test_synthetic_data_is_refused_in_the_primary_graph_and_without_the_flag():
    p = write(ds())
    assert seed.main(["--dataset", str(p), "--graph", PRIMARY_GRAPH, "--dry-run", "--allow-synthetic"]) == 1
    assert seed.main(["--dataset", str(p), "--graph", "landslide_test", "--dry-run"]) == 1
    assert seed.main(["--dataset", str(p), "--graph", "landslide_test", "--dry-run", "--allow-synthetic"]) == 0


def test_invalid_dataset_is_refused():
    d = ds()
    d["connections"][0]["segment_id"] = "missing"
    assert seed.main(["--dataset", str(write(d)), "--graph", "landslide_test", "--dry-run", "--allow-synthetic"]) == 1
    assert seed.main(["--dataset", str(Path(tempfile.mkdtemp()) / "nope.json"), "--dry-run"]) == 1
