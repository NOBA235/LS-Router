"""The fingerprint written at seed time (data/osm_normalize.py) and the one recomputed by audit_closure must agree."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import osm_normalize as N
import synthetic_corridor as S
from app.graph import fingerprint as F


def test_segment_and_network_fingerprints_match_between_pipeline_and_backend():
    ds = S.synthetic_dataset()
    assert ds["segments"] and ds["connections"]
    for s in ds["segments"]:
        assert N.segment_fingerprint(s) == F.segment_fingerprint(s) == s["baseline_fp"]
    expect = F.network_fingerprint(ds["segments"], ds["connections"])
    assert N.network_fingerprint(ds["segments"], ds["connections"]) == expect == ds["datasets"][0]["network_fingerprint"]


def test_fingerprint_is_sensitive_to_weights_and_connections_and_tolerates_damage():
    ds = S.synthetic_dataset()
    base = F.network_fingerprint(ds["segments"], ds["connections"])
    segs = [dict(s) for s in ds["segments"]]
    segs[0]["travel_time_min"] += 0.5
    assert F.network_fingerprint(segs, ds["connections"]) != base
    assert F.network_fingerprint(ds["segments"], ds["connections"][1:]) != base
    damaged = ds["connections"] + [{"from_place_id": "x", "to_place_id": "y", "segment_id": None, "travel_time_min": None, "direction": None}]
    assert F.network_fingerprint(ds["segments"], damaged) != base       # no crash on a connection with no segment_id
