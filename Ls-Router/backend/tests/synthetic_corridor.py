r"""SYNTHETIC TEST FIXTURE. Not real geography, not for the sourced graph.

Overpass-shaped JSON around lat 10, lon 10 (open ocean) with a topology whose normalized result is known by hand:

    D(n1) --S1-- J1(n3) --S2[bridge]-- J2(n5) --S3-- n6 --S4-- K(n7)        NH 29 (trunk)
                   \                    / |           ^
                    S5 - B(a1) - S6 - a2 --S7           S9 (one-way a2 -> n6)
                                        S8 (spur) -- A(s2)

Expected: 8 places, 9 segments, 17 connections (8 two-way + 1 one-way segment), 1 bridge ON S2.
"""
from __future__ import annotations

BBOX = (9.90, 9.90, 10.20, 10.20)
D, N2, J1, N4, J2, N6, K = 9001, 9002, 9003, 9004, 9005, 9006, 9007
A1, A2, O1, B1, S1_, S2_ = 9011, 9012, 9013, 9014, 9021, 9022
X1, X2, X3, OUT1 = 9031, 9032, 9033, 9041

COORDS = {
    D: (10.00, 10.00), N2: (10.00, 10.01), J1: (10.00, 10.02), N4: (10.00, 10.03), B1: (10.00, 10.035), J2: (10.00, 10.04),
    N6: (10.00, 10.05), K: (10.00, 10.06), A1: (10.01, 10.02), A2: (10.01, 10.04), O1: (10.005, 10.045),
    S1_: (9.995, 10.04), S2_: (9.99, 10.04), X1: (10.15, 10.15), X2: (10.15, 10.16), X3: (10.15, 10.17), OUT1: (10.00, 10.50),
}

WAYS = [
    (8001, [D, N2, J1], {"highway": "trunk", "ref": "NH 29", "name": "Synthetic Highway", "surface": "asphalt"}),
    (8002, [J1, N4], {"highway": "trunk", "ref": "NH 29", "name": "Synthetic Highway", "surface": "asphalt"}),
    (8003, [N4, B1, J2], {"highway": "trunk", "ref": "NH 29", "name": "Synthetic Highway", "bridge": "yes", "surface": "asphalt"}),
    (8004, [J2, N6, K], {"highway": "trunk", "ref": "NH 29", "name": "Synthetic Highway", "surface": "asphalt"}),
    (8005, [J1, A1, A2, J2], {"highway": "secondary", "maxspeed": "40", "name": "Synthetic Alt Road"}),
    (8006, [J2, S1_, S2_], {"highway": "tertiary", "name": "Synthetic Spur"}),
    (8007, [A2, O1, N6], {"highway": "secondary", "oneway": "yes", "name": "Synthetic One-way"}),
    (8008, [X1, X2, X3], {"highway": "tertiary", "ref": "SH 1"}),               # separate component, no NH29
    (8009, [D, N2], {"highway": "trunk", "ref": "NH 29"}),                       # duplicate edge
    (8010, [N6, O1], {"highway": "tertiary", "access": "private"}),             # skipped: private
    (8011, [K, OUT1], {"highway": "trunk", "ref": "NH 29"}),                     # clipped: leaves bbox
    (8012, [D, 9999], {"highway": "trunk", "ref": "NH 29"}),                     # references a node that is not in the data
]


def build():
    """Return (roads_raw, features_raw, source_meta)."""
    elements = [{"type": "node", "id": n, "lat": lat, "lon": lon} for n, (lat, lon) in COORDS.items()]
    elements += [{"type": "way", "id": wid, "nodes": list(nodes), "tags": dict(tags)} for wid, nodes, tags in WAYS]  # copies: tests may mutate
    roads = {"version": 0.6, "osm3s": {"timestamp_osm_base": "2000-01-01T00:00:00Z"}, "elements": elements}

    def place(i, name, tag, lat, lon, **extra):
        tags = {"place": tag, **({"name": name} if name else {}), **extra}
        return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}

    feats = [
        place(7001, "Synthetic Dimapur", "city", 10.0002, 10.0001, population="123456"),
        place(7002, "Synthetic Kohima", "town", 10.0002, 10.0599),
        place(7003, "Synthetic Village A", "village", 9.9905, 10.0402),       # on the spur
        place(7004, "Synthetic Village B", "hamlet", 10.0102, 10.0201),       # on the alternative road
        place(7005, None, "village", 10.0, 10.015),                           # unnamed -> skipped
        place(7006, "Synthetic Suburb", "suburb", 10.0, 10.016),              # not a settlement type -> skipped
        place(7007, "Synthetic Faraway", "village", 10.18, 10.18),            # in bbox, > threshold from any road
        place(7008, "Synthetic Village A", "village", 9.9906, 10.0403),       # duplicate of A -> skipped
        {"type": "node", "id": 7101, "lat": 10.0003, "lon": 10.0598,
         "tags": {"amenity": "hospital", "name": "Synthetic Hospital", "beds": "120"}},
        {"type": "way", "id": 7102, "center": {"lat": 10.0004, "lon": 10.0597},
         "tags": {"amenity": "hospital", "name": "Synthetic Hospital"}},     # duplicate of 7101 -> skipped
        {"type": "node", "id": 7103, "lat": 10.19, "lon": 10.19, "tags": {"healthcare": "hospital"}},  # unnamed, far -> unattached
    ]
    features = {"version": 0.6, "elements": feats}
    meta = {"name": "SYNTHETIC TEST FIXTURE", "license": "none", "retrieved_at": "2000-01-01T00:00:00Z",
            "osm_base_timestamp": "2000-01-01T00:00:00Z", "endpoint": "synthetic", "query_sha256": "synthetic"}
    return roads, features, meta


def synthetic_dataset(curated=None, cfg=None, today=None):
    """Normalize the synthetic fixture (needs data/ on sys.path, which pytest.ini sets)."""
    from datetime import date

    import osm_normalize
    from pipeline_config import PipelineConfig

    cfg = cfg or PipelineConfig(bbox=BBOX, reference_names={"dimapur": "Synthetic Dimapur", "kohima": "Synthetic Kohima"})
    roads, feats, meta = build()
    return osm_normalize.normalize(roads, feats, meta, cfg, curated=curated, today=today or date(2026, 10, 14),
                                   dataset_kind="synthetic-test")
