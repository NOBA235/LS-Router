r"""Load data/cache/normalized.json into FalkorDB. Idempotent: every write is a MERGE keyed on id.

    python data\seed.py --dry-run        # validate + count, no database needed
    python data\seed.py                  # seed graph "landslide" (FALKORDB_* from .env), then verify
    python data\seed.py --verify-only
    python data\seed.py --reset --yes    # DELETE EVERYTHING in the graph first (also removes simulated closures)

Seed-owned nodes are replaced property-by-property (SET n = props), so a rerun after a data refresh leaves no stale
values. Re-running the same dataset creates no new nodes or relationships (the run prints the created counts).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pipeline_config as pc
from osm_normalize import SCHEMA_VERSION, validate_dataset

NODE_LABELS = ["Corridor", "Dataset", "Season", "Place", "Hospital", "Bridge", "RoadSegment", "ClosureEvent"]
INDEX_STATEMENTS = [f"CREATE INDEX FOR (n:{label}) ON (n.id)" for label in NODE_LABELS]
MAX_ROWS, MAX_BYTES = 250, 150_000


def _clean(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if v is None:
            continue  # absent property == unknown; never store a guess
        if isinstance(v, dict):
            raise ValueError(f"{rec.get('id')}: property {k!r} is a dict; only scalars and lists are stored")
        out[k] = v
    return out


def _batches(rows: list):
    batch, size = [], 0
    for row in rows:
        n = len(json.dumps(row, ensure_ascii=False))
        if batch and (len(batch) >= MAX_ROWS or size + n > MAX_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += n
    if batch:
        yield batch


def _node_rows(ds: dict, label: str) -> list:
    source = {"Corridor": [ds["corridor"]], "Dataset": ds["datasets"], "Season": ds["seasons"], "Place": ds["places"],
              "Hospital": ds["hospitals"], "Bridge": ds["bridges"], "RoadSegment": ds["segments"],
              "ClosureEvent": ds["observed_events"]}[label]
    return [{"id": r["id"], "props": _clean(r)} for r in source]


def _rel_specs(ds: dict) -> list:
    """(name, from_label, to_label, rel_type, merge_key_prop_or_None, rows[{a,b,k?,props}])"""
    seg = ds["segments"]
    return [
        ("SEGMENT_OF", "RoadSegment", "Corridor", "SEGMENT_OF", None,
         [{"a": s["id"], "b": ds["corridor"]["id"], "props": {"role": "nh29" if s["on_nh29"] else "connector"}} for s in seg]),
        ("CONNECTED_BY", "Place", "Place", "CONNECTED_BY", "segment_id",
         [{"a": c["from_place_id"], "b": c["to_place_id"], "k": c["segment_id"],
           "props": _clean({"segment_id": c["segment_id"], "travel_time_min": c["travel_time_min"], "direction": c["direction"]})}
          for c in ds["connections"]]),
        ("ON", "Bridge", "RoadSegment", "ON", None, [{"a": r["bridge_id"], "b": r["segment_id"], "props": {}} for r in ds["bridge_on"]]),
        ("LOCATED_AT", "Hospital", "Place", "LOCATED_AT", None,
         [{"a": r["hospital_id"], "b": r["place_id"],
           "props": {"snap_distance_m": r["snap_distance_m"], "method": r["method"], "data_status": r["data_status"]}}
          for r in ds["hospital_located_at"]]),
        ("AFFECTS", "Season", "RoadSegment", "AFFECTS", None,
         [{"a": r["season_id"], "b": r["segment_id"], "props": {"basis": r["basis"]}} for r in ds["season_affects"]]),
        ("NEAR_PLACE", "ClosureEvent", "Place", "NEAR_PLACE", None,
         [{"a": r["event_id"], "b": r["place_id"], "props": {"basis": r["basis"], "data_status": r["data_status"]}}
          for r in ds["event_near_place"]]),
        ("BLOCKS", "ClosureEvent", "RoadSegment", "BLOCKS", None,
         [{"a": r["event_id"], "b": r["target_id"], "props": {}} for r in ds["event_blocks"]]),
        ("FAILED_IN", "Bridge", "ClosureEvent", "FAILED_IN", None,
         [{"a": r["bridge_id"], "b": r["event_id"], "props": _clean({"year": r["year"]})} for r in ds["event_failed_bridges"]]),
    ]


def build_statements(ds: dict) -> list:
    """Pure: list of {name, query, params}. Every write is MERGE-based, so replays are harmless."""
    out = []
    for label in NODE_LABELS:
        rows = _node_rows(ds, label)
        q = f"UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) SET n = row.props"
        out += [{"name": f"nodes:{label}", "query": q, "params": {"rows": b}} for b in _batches(rows)]
    for name, la, lb, rel, key, rows in _rel_specs(ds):
        pattern = f"(a)-[r:{rel}" + (f" {{{key}: row.k}}" if key else "") + "]->(b)"
        q = f"UNWIND $rows AS row MATCH (a:{la} {{id: row.a}}), (b:{lb} {{id: row.b}}) MERGE {pattern} SET r = row.props"
        out += [{"name": f"rels:{name}", "query": q, "params": {"rows": b}} for b in _batches(rows)]
    return out


def expected_counts(ds: dict) -> dict:
    return {"Corridor": 1, "Dataset": len(ds["datasets"]), "Season": len(ds["seasons"]), "Place": len(ds["places"]),
            "Hospital": len(ds["hospitals"]), "Bridge": len(ds["bridges"]), "RoadSegment": len(ds["segments"]),
            "ClosureEvent": len(ds["observed_events"])}


def seed_graph(graph, ds: dict, log=print) -> Counter:
    for stmt in INDEX_STATEMENTS:
        try:
            graph.query(stmt)
        except Exception as exc:  # re-running: index already exists
            if "already" not in str(exc).lower() and "exist" not in str(exc).lower():
                raise
    totals: Counter = Counter()
    for st in build_statements(ds):
        res = graph.query(st["query"], st["params"])
        for attr in ("nodes_created", "relationships_created", "properties_set"):
            totals[attr] += getattr(res, attr, 0) or 0
    log(f"Seed run created {totals['nodes_created']} node(s) and {totals['relationships_created']} relationship(s); "
        "a rerun of the same dataset should report 0 and 0.")
    return totals


def _scalar(graph, query: str):
    rows = graph.query(query).result_set
    return rows[0][0] if rows else None


def verify_graph(graph, ds: dict) -> list:
    """Return [(check, ok, detail)] comparing the live graph with the dataset and the connectivity invariants."""
    checks = []
    for label, want in expected_counts(ds).items():
        q = f"MATCH (n:{label}) RETURN count(n)"
        if label == "ClosureEvent":
            q = "MATCH (n:ClosureEvent {origin: 'curated_report'}) RETURN count(n)"
        got = _scalar(graph, q)
        checks.append((f"count {label}", got == want, f"graph={got} dataset={want}"))
        dup = _scalar(graph, f"MATCH (n:{label}) WITH n.id AS id, count(*) AS c WHERE c > 1 RETURN count(id)")
        checks.append((f"no duplicate ids {label}", dup == 0, f"duplicates={dup}"))
    got = _scalar(graph, "MATCH ()-[c:CONNECTED_BY]->() RETURN count(c)")
    checks.append(("count CONNECTED_BY", got == len(ds["connections"]), f"graph={got} dataset={len(ds['connections'])}"))
    invariants = [
        ("every CONNECTED_BY names an existing RoadSegment",
         "MATCH (s:RoadSegment) WITH collect(s.id) AS ids MATCH ()-[c:CONNECTED_BY]->() WHERE NOT c.segment_id IN ids RETURN count(c)"),
        ("every RoadSegment is SEGMENT_OF the corridor", "MATCH (s:RoadSegment) WHERE NOT (s)-[:SEGMENT_OF]->(:Corridor) RETURN count(s)"),
        ("no Place is isolated", "MATCH (p:Place) WHERE NOT (p)-[:CONNECTED_BY]-() RETURN count(p)"),
        ("every Hospital is LOCATED_AT a Place", "MATCH (h:Hospital) WHERE NOT (h)-[:LOCATED_AT]->(:Place) RETURN count(h)"),
        ("verified implies last_verified", "MATCH (n) WHERE n.confidence = 'verified' AND n.last_verified IS NULL RETURN count(n)"),
        ("last_verified implies verified", "MATCH (n) WHERE n.last_verified IS NOT NULL AND n.confidence <> 'verified' RETURN count(n)"),
        ("seed events are observed, not simulated",
         "MATCH (e:ClosureEvent {origin: 'curated_report'}) WHERE e.data_status <> 'observed' RETURN count(e)"),
    ]
    for name, q in invariants:
        got = _scalar(graph, q)
        checks.append((name, got == 0, f"violations={got}"))
    return checks


def _connect(graph_name: str):
    from falkordb import FalkorDB  # imported late so --dry-run works without the package

    pc.load_env()
    client = FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"), port=int(os.environ.get("FALKORDB_PORT", "6379")),
                      password=os.environ.get("FALKORDB_PASSWORD") or None)
    return client.select_graph(graph_name)


def main(argv=None) -> int:
    pc.load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=pc.NORMALIZED_PATH)
    ap.add_argument("--graph", default=os.environ.get("FALKORDB_GRAPH", pc.PRIMARY_GRAPH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--reset", action="store_true", help="delete ALL nodes in the graph first (needs --yes)")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--allow-synthetic", action="store_true", help="permit a synthetic test dataset (never in the primary graph)")
    a = ap.parse_args(argv)

    if not a.dataset.exists():
        print(f"{a.dataset} not found. Run: python data\\fetch_osm.py normalize", file=sys.stderr)
        return 1
    ds = json.loads(a.dataset.read_text(encoding="utf-8"))
    if ds.get("schema_version") != SCHEMA_VERSION:
        print(f"Unsupported schema_version {ds.get('schema_version')}", file=sys.stderr)
        return 1
    errors = [i for i in validate_dataset(ds) if i["severity"] == "error"]
    if errors:
        print("Refusing to seed: dataset fails validation:", file=sys.stderr)
        for i in errors:
            print(f"  {i['code']}: {i['message']}", file=sys.stderr)
        return 1
    kind = ds["meta"]["kind"]
    if kind != "real" and (a.graph == pc.PRIMARY_GRAPH or not a.allow_synthetic):
        print(f"Dataset kind is {kind!r}. Synthetic data needs --allow-synthetic and a graph other than "
              f"{pc.PRIMARY_GRAPH!r}.", file=sys.stderr)
        return 1
    print(f"Dataset: {kind}, retrieved {ds['meta']['source'].get('retrieved_at')}; target graph: {a.graph}")
    print("Expected node counts:", expected_counts(ds), "| CONNECTED_BY:", len(ds["connections"]))
    if a.dry_run:
        print(f"Dry run: {len(build_statements(ds))} statement(s) would be sent. Nothing written.")
        return 0

    graph = _connect(a.graph)
    if a.reset:
        if not a.yes:
            print("--reset deletes every node in the graph (including simulated closures). Add --yes to confirm.", file=sys.stderr)
            return 1
        graph.query("MATCH (n) DETACH DELETE n")
        print("Graph cleared.")
    if not a.verify_only:
        seed_graph(graph, ds)
    checks = verify_graph(graph, ds)
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
