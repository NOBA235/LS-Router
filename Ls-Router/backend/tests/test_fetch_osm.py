"""Fetch/cache logic with an injected fake HTTP layer. No network."""
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import fetch_osm as F
import osm_normalize as N
import synthetic_corridor as S
from pipeline_config import PipelineConfig

CFG = PipelineConfig(bbox=S.BBOX, reference_names={"dimapur": "Synthetic Dimapur", "kohima": "Synthetic Kohima"})
OK = json.dumps({"osm3s": {"timestamp_osm_base": "2000-01-01T00:00:00Z"}, "elements": [{"type": "node", "id": 1}]}).encode()
EPS = ["https://a.example/api", "https://b.example/api"]


def scripted(*responses):
    """fake post: pops (status, body) or raises the given TransientError; records calls."""
    calls = []

    def post(url, body, headers, timeout):
        calls.append(url)
        r = responses[len(calls) - 1] if len(calls) <= len(responses) else responses[-1]
        if isinstance(r, Exception):
            raise r
        return r

    post.calls = calls
    return post


def test_queries_embed_bbox_and_hash_changes_with_it():
    q = F.build_queries(CFG)
    assert "(9.9,9.9,10.2,10.2)" in q["roads"] and "out body center" in q["features"] and "hospital" in q["features"]
    assert F.queries_hash(q) != F.queries_hash(F.build_queries(replace(CFG, bbox=(1, 2, 3, 4))))


def test_success_on_first_try():
    data, url = F.fetch_overpass("q", EPS, post=scripted((200, OK)), sleep=lambda s: None)
    assert url == EPS[0] and len(data["elements"]) == 1


def test_rate_limit_is_retried_with_backoff():
    sleeps = []
    post = scripted((429, b""), (504, b""), (200, OK))
    data, url = F.fetch_overpass("q", EPS, post=post, sleep=sleeps.append)
    assert len(post.calls) == 3 and sleeps == [5, 10] and url == EPS[0]


def test_partial_result_is_not_accepted_and_next_endpoint_is_tried():
    partial = json.dumps({"remark": "runtime error: Query timed out", "elements": [{"type": "node", "id": 1}]}).encode()
    post = scripted((200, partial))
    try:
        F.fetch_overpass("q", EPS, post=post, sleep=lambda s: None, attempts=1)
    except F.FetchError as exc:
        assert "incomplete result" in str(exc) and EPS[0] in str(exc) and EPS[1] in str(exc)
    else:
        raise AssertionError("partial result must not be accepted")
    post2 = scripted((200, partial), (200, OK))
    data, url = F.fetch_overpass("q", EPS, post=post2, sleep=lambda s: None, attempts=1)
    assert url == EPS[1]


def test_bad_query_fails_fast_without_retries():
    post = scripted((400, b"syntax error"))
    try:
        F.fetch_overpass("q", EPS, post=post, sleep=lambda s: None)
    except F.FetchError as exc:
        assert "HTTP 400" in str(exc)
    assert len(post.calls) == 1


def test_network_errors_and_garbage_are_transient():
    post = scripted(F.TransientError("network error: timed out"), (200, b"<html>"), (200, OK))
    data, _ = F.fetch_overpass("q", EPS, post=post, sleep=lambda s: None)
    assert len(post.calls) == 3


def test_cache_is_written_only_when_both_requests_succeed_and_then_reused():
    raw = Path(tempfile.mkdtemp())
    roads_ok = OK
    # roads succeeds, features always fails -> nothing may be written
    state = {"n": 0}

    def flaky(url, body, headers, timeout):
        state["n"] += 1
        return (200, roads_ok) if b"out+skel" in body or b"node%28w.r%29" in body else (503, b"")

    try:
        F.fetch_all(CFG, raw_dir=raw, post=flaky, sleep=lambda s: None)
    except F.FetchError:
        pass
    assert not list(raw.glob("*.json"))
    good = scripted((200, OK))
    meta = F.fetch_all(CFG, raw_dir=raw, post=good, sleep=lambda s: None)
    assert (raw / "roads.json").exists() and (raw / "features.json").exists() and (raw / "fetch_meta.json").exists()
    assert meta["query_sha256"] == F.queries_hash(F.build_queries(CFG)) and meta["license"] == "ODbL 1.0"
    before = len(good.calls)
    F.fetch_all(CFG, raw_dir=raw, post=good, sleep=lambda s: None)
    assert len(good.calls) == before  # cache hit: no network


def test_normalize_runs_from_the_cache_alone():
    raw, out, cur = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp()) / "normalized.json", Path(tempfile.mkdtemp())
    roads, feats, meta = S.build()
    (raw / "roads.json").write_text(json.dumps(roads), encoding="utf-8")
    (raw / "features.json").write_text(json.dumps(feats), encoding="utf-8")
    (raw / "fetch_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    ds = F.run_normalize(CFG, raw_dir=raw, out_path=out, curated_dir=cur)
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["report"]["counts"] == ds["report"]["counts"] and (out.parent / "report.md").exists()
    assert "Not attached to the road network" in (out.parent / "report.md").read_text(encoding="utf-8")
