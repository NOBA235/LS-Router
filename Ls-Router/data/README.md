# Data pipeline (Phase 1)

```
Overpass API --fetch--> data/cache/raw/*.json --normalize--> data/cache/normalized.json + report.md --seed--> FalkorDB
   (network, once)        (cached, committed)                  (offline, validated)                       (idempotent)
```

Source: OpenStreetMap, (c) OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright). Keep the attribution when you show or redistribute this data. `fetch_meta.json` records the endpoint, retrieval time, OSM data timestamp and query hash. Standard library only; `osmnx`/`overpy` were not needed (raw Overpass JSON is all the pipeline uses, and osmnx would pull in a large geo stack).

Nothing here runs at application startup. Commit `data/cache/` once you are happy with the extract so a clean checkout can seed without Overpass.

## Modelling rules

- **Segments.** A `RoadSegment` is a stretch of road between two network vertices. Vertices are road junctions, dead ends, places where road identity or one-way direction changes, and the road points that settlements and hospitals attach to. Every `CONNECTED_BY` edge carries the `segment_id` of the segment it travels on; validation fails the dataset otherwise. A two-way segment has two `CONNECTED_BY` edges (`forward`, `reverse`), a one-way segment one.
- **NH29.** A way counts as NH29 only if its `ref` matches `nh_ref_regex` (default `NH 29`). Other roads in the corridor box are kept as `role: connector` if they join the NH29 network; components that never touch NH29 are dropped and listed in the report. `fetch` prints the refs it saw, because the old numbering (NH 39) or a neighbouring road (NH 129, NH 2) may be what OSM uses; override with `--nh-ref-regex`.
- **Settlements and hospitals** attach to the nearest road vertex within a threshold (town 2.5 km, village 1 km, hospital 0.8 km; `pipeline_config.py`). A settlement *becomes* the `Place` at its vertex (its own coordinates are kept; `snap_distance_m` records the move). A hospital is linked with `(Hospital)-[:LOCATED_AT]->(Place)`, an added relationship, because the brief's `CONNECTED_BY` is Place-to-Place. Access roads shorter than the threshold are not modelled. Anything farther is listed as *unattached* in the report and is **not** put in the graph.
- **Bridges** (`bridge=*` ways) sit `ON` every segment carrying one of their edges; closing a bridge later means closing those segments. A bridge's `name` is `bridge:name`, or the way's `name` only when no non-bridge part of that road uses it (otherwise it is stored as `road_name`).
- **Travel time** = sum over edges of length / speed, using a numeric `maxspeed` tag when present, otherwise placeholder planning speeds per road class (`DEFAULT_SPEEDS_KMH`: assumptions, not measurements). Each segment records `travel_time_basis` (`tagged`, `assumed`, `mixed`, `missing`). `--no-assumed-speeds` leaves untagged times missing; routing must reject those.
- **Direction.** Two-way unless OSM says `oneway` (`yes`, `-1`, roundabouts). `reversible`/`alternating` are treated as two-way and counted.
- **Provenance** on every geographic node: `source_url`, `source_id`, `retrieved_at`, `last_verified` (unset unless a person verified it), `confidence` (`unverified` by default), `data_status` (`observed` for tagged OSM features, `derived` for computed segments and junctions).
- **Unknowns stay unknown**: a missing property means unknown. Nothing infers population, beds, trauma level, `monsoon_risk` or failure history.
- **Seasons**: one `Season` node (monsoon, months 6 to 9) marked `derived` with the note that it is a modelling assumption; it `AFFECTS` all segments with `basis: assumed_all_segments_exposed`. It carries no risk information by itself.
- **Not in Phase 1**: `NEAREST_HOSPITAL` (needs routing, never authoritative), `Alert`, simulated closures, `FAILED_IN`/`BLOCKS` from real evidence unless you add reviewed ids.

## Curated inputs (`data/curated/`)

| File | Used by pipeline | Purpose |
|---|---|---|
| `risk_events.json` | yes | 7 publicly reported landslide/washout/bridge events (Aug 2024 to Sep 2025) with source URLs. Seeded as `ClosureEvent` with `data_status: observed`, `confidence: unverified`, no coordinates. Linked `NEAR_PLACE` only when a source names a place that exists in the graph. Add `segment_ids` / `bridge_ids` after you have reviewed the match to create `BLOCKS` / `FAILED_IN`. **Compiled from web search; open each source and confirm before trusting.** |
| `hospital_reference.json` | yes (report only) | Names from the Nagaland CMHIS empanelled list, used to flag hospitals missing from the OSM extract. Not seeded. |
| `verification.json` | if present | Your hand verification. `verified` needs `verification_source_url` and a real `last_verified` date (not in the future). Only name, population, beds, trauma_level, monsoon_risk (low/medium/high), surface can be set. See `verification.example.json`. |
| `additions.json` | if present | Hospitals or settlements OSM lacks (name, lat, lon, source_url, compiled_on). See `additions.example.json`. |

The example files are not loaded.

## Checking the result

1. `python data\fetch_osm.py normalize` prints the report and exits non-zero on validation errors. `seed.py` re-validates and refuses a bad dataset.
2. After seeding, `seed.py` compares counts with the dataset and runs invariants: every `CONNECTED_BY` names an existing segment, every segment is `SEGMENT_OF` the corridor, no isolated places, every hospital has a road attachment, `verified` implies a date, seed events are observed.
3. Look at the graph in FalkorDB Browser with `queries.cypher`.

## Limitations

- OSM coverage in this area is unknown to me until you fetch it. The report, not this document, tells you how many places, hospitals and bridges exist and whether Dimapur and Kohima are connected (a warning appears if not).
- Attachment uses the nearest road vertex, not the nearest point on the road, and ignores access roads under the thresholds.
- Placeholder speeds make travel times rough estimates; replace them with sourced values where you can.
- Synthetic test data (`backend/tests/synthetic_corridor.py`, coordinates near 10N 10E) is refused by `seed.py` in the primary graph `landslide`.
