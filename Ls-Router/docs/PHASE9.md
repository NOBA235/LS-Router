# Phase 9 — Demo Assets (Reproducible 3-Minute Demo, Reset Procedure, Architecture, Verified Claims & Judge's Guide)

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

Phase 9 packages **Landslide Router** for hackathon evaluation (`Graph Hacks: Context for AI Agents | Track 1: Agents That Act on Connected Data | FalkorDB`) with a reproducible 12-step demonstration that runs in under 25 seconds (well below the 3-minute limit), a clean setup and reset procedure, a Judge's Guide, a technical architecture diagram, a blog post draft, a pitch deck outline, a verified claims checklist, and a transparent limitations statement.

---

## 1. Reproducible Three-Minute Demo (12 Steps)

You can execute the complete 12-step demonstration from the Windows Command Prompt or over HTTP:

### Option A — Windows Command Prompt (against live FalkorDB Cloud / Docker)
```bat
backend\.venv\Scripts\activate.bat
python scripts\run_demo.py
```

### Option B — Windows Command Prompt (Zero-Docker / Offline against the cached 2,019-node OpenStreetMap NH29 dataset)
```bat
backend\.venv\Scripts\activate.bat
python scripts\run_demo.py --dataset data\cache\normalized.json
```

### Option C — HTTP Endpoint
```bat
curl -X POST http://127.0.0.1:8000/demo/run
```

### Actual 12-Step Output on the 2,019-Node Real OpenStreetMap NH29 Graph (`20.96s` total)

| Step | Action | Tool / Endpoint | Actual Result from the 2,019-Node NH29 Graph |
|---|---|---|---|
| **1** | Show corridor map & sourced road network | `store.load_snapshot()` + MCP `reroute` | `814` places (`76` villages, `4` towns, `734` junctions), `1,045` road segments (`100` on NH29 = `189.503 km`), `126` bridges, `24` hospitals, `7` observed events. Baseline Dimapur → Kohima route: **`125.2 min`** (`70.101 km` across `53` segments). |
| **2** | Enter simulated landslide location | `LocationResolver.resolve_location` | Input `"Landslide at NH29 near kilometre 42."` returns `status="ambiguous"` (`requires_clarification=True`) with `5` candidate segments ranked by cumulative distance from Dimapur; graph is **not** mutated. |
| **3** | Show resolved road segments & confidence | `LocationResolver.resolve_location` (confirmed) | Confirms trunk detour segment `seg-569069510-569069527-1353575333` (`confidence="unverified"`, `source="https://www.openstreetmap.org/way/1353575333"`) and branch cut-off segment `seg-2469699767-6286548534-239157848` (`confidence="unverified"`, `source="https://www.openstreetmap.org/way/239157848"`). |
| **4** | Apply simulated closures | MCP `apply_closure` / `POST /closure` | Creates active `ClosureEvent` nodes (`data_status="simulated"`, `status="active"`) with `:BLOCKS` edges in the graph. |
| **5** | Show reachability changes & hospital impact | MCP `find_cut_off_places` & `find_unreachable_hospitals` | Closing `seg-2469699767-6286548534-239157848` newly cuts off **`3` settlements** (`Kezo Town`, `Kikruma`, `Chakhabama` — `4` total places including `junction-6286548534`) from Dimapur (`808/812` places remain reachable) and evaluates road-network paths to all `24` hospitals. |
| **6** | Calculate alternate route | MCP `reroute` / `GET /reroute` | Closing `seg-569069510-569069527-1353575333` finds `status="route_found"` from Dimapur to Kohima across `67` unblocked segments. |
| **7** | Show total travel time & detour delta | MCP `explain_path` (`baseline_comparison`) | Baseline **`125.2 min` (`70.101 km`)** → Detour **`215.23 min` (`100.511 km`)**, adding **`+90.03 min` (`+30.41 km`)**. Verified `segment_times_sum_to_total=True` and `no_selected_segment_is_blocked=True`. |
| **8** | Draft community alerts | MCP `draft_alerts` / `GET /alerts/{id}/export` | Generates **`3` synthetic SMS draft alerts** (one each for `Kezo Town`, `Kikruma`, and `Chakhabama`). Every alert has `status="draft"` and `sent=False`. |
| **9** | Inspect graph paths & Cypher queries | `cypher` log & `mcp_handoffs` | Quotes all `12` read/write Cypher statements per tool call plus `15` logged MCP JSON-RPC 2.0 tool handoffs. |
| **10** | Display weak links & seasonal risk evidence | MCP `find_weak_links` & `seasonal_risk(month=7)` | Ranks `#1` weak-link segment `seg-569069510-569069527-1353575333` (`betweenness=0.458767`) and `#1` bridge `bridge-way-1073679103` (`betweenness=0.457556`), plus July monsoon risk rankings with non-predictive advisories. |
| **11** | Resolve simulated closures | MCP `clear_closure` / `POST /resolve/{id}` | Transitions both simulated closures from `active` → `resolved` (`blocked_segments_after=[]`) while preserving historical `ClosureEvent` nodes. |
| **12** | Audit resulting graph state | MCP `audit_closure` | Confirms `returned_to_baseline=True`, `other_active_closures=[]`, `discrepancies=[]`, and cryptographic fingerprint equality across all `1,045` segments. |

---

## 2. Clean Setup and Reset Procedure

### A. Soft Reset (Resolve all active simulated closures while keeping historical records)
```bat
backend\.venv\Scripts\activate.bat
python scripts\reset_demo.py
```
Or via HTTP:
```bat
curl -X POST http://127.0.0.1:8000/demo/reset
```

### B. Full Clean Re-Seed (Wipe all simulated closures and re-seed from validated `data/cache/normalized.json`)
```bat
backend\.venv\Scripts\activate.bat
python data\seed.py --reset --yes
python data\seed.py
```
Running `python data\seed.py` a second time verifies idempotency (`0 node(s) and 0 relationship(s)` created) and runs all 24 graph invariant checks (`[ok]`).

---

## 3. Technical Architecture Diagram

```
+-----------------------------------------------------------------------------------+
|                      PHASE 1: OFFLINE-CAPABLE DATA PIPELINE                       |
|  OpenStreetMap Overpass API  +  Curated Nagaland Risk Records (data/curated/)     |
|        |                                      |                                   |
|        v                                      v                                   |
|  data/fetch_osm.py (fetch -> normalize) --> data/cache/normalized.json (2,019 n)  |
|        |                                                                          |
|        v  (Idempotent MERGE + SHA-256 segment & dataset baseline fingerprints)    |
|  data/seed.py                                                                     |
+-----------------------------------------------------------------------------------+
                                        |
                                        v
+-----------------------------------------------------------------------------------+
|                FALKORDB PROPERTY GRAPH ("landslide" — Source of Truth)            |
|  Nodes: Place (814), RoadSegment (1045), Bridge (126), Hospital (24),             |
|         ClosureEvent (7 observed + sim), Season (1), Corridor (1), Dataset (1)    |
|  Edges: :CONNECTED_BY (1935), :SEGMENT_OF, :ON, :LOCATED_AT, :AFFECTS,            |
|         :NEAR_PLACE, :BLOCKS, :FAILED_IN, :TARGETS, :ROUTED_VIA                   |
+-----------------------------------------------------------------------------------+
                                        |
                   12 Cypher Read Queries + Logged Cypher Writes
                                        v
+-----------------------------------------------------------------------------------+
|          PHASE 2: FALKORSTORE & DETERMINISTIC GRAPH TOOLBOX (backend/app/graph/)  |
|  - FalkorStore (store.py): Logs every executed Cypher statement & parameter map   |
|  - Snapshot (snapshot.py): Excludes blocked segments while constructing graphs    |
|  - Algorithms (algorithms.py): Directed reachability, Weakly Connected Components,|
|    Dijkstra shortest path (weight=travel_time_min), Brandes Betweenness           |
+-----------------------------------------------------------------------------------+
                                        |
                JSON-RPC 2.0 over Official Python MCP SDK (stdio / memory)
                                        v
+-----------------------------------------------------------------------------------+
|      PHASE 3 & 4: MCP SERVER (mcp/server.py) + AGENT & LOCATION RESOLVER          |
|  - 12 MCP Tools discovered via tools/list and invoked via tools/call              |
|  - LocationResolver (resolver.py): Ambiguity gating (refuses to guess km/coords)  |
|  - LandslideAgent (agent.py): Deterministic or LLM-assisted MCP orchestration     |
+-----------------------------------------------------------------------------------+
                                        |
                           HTTP JSON + PII Redaction Filter
                                        v
+-----------------------------------------------------------------------------------+
|               PHASE 5, 7, 8, 9: FASTAPI BACKEND (backend/app/main.py)             |
|  /health | /graph | /closure | /cutoff/{id} | /reroute | /weak-links              |
|  /seasonal-risk | /alerts/{id} | /alerts/{id}/export | /resolve/{id}              |
|  /remember/{id} | /history | /replay/{id} | /demo/run | /demo/reset               |
+-----------------------------------------------------------------------------------+
                                        |
                                        v
+-----------------------------------------------------------------------------------+
|       PHASE 6 & 8: THREE-PANE REACT + MAPLIBRE WORKBENCH (frontend/src/)          |
|  [Left Pane: Controls & Timeline] | [Center: MapLibre NH29] | [Right: "Why?"]     |
|  - Confidence badges (verified/unverified, observed/derived/simulated/draft)      |
|  - Side-by-side Baseline vs Detour route comparison & timeline replay             |
|  - Searchable Data-Source & Provenance Inspector + Cypher query disclosure        |
+-----------------------------------------------------------------------------------+
```

---

## 4. Verified Claims Checklist

| # | Engineering / Product Claim | Supporting Graph Mechanism | Verification Test / Command |
|---|---|---|---|
| 1 | **The graph is the single source of truth** | `FalkorStore.load_snapshot()` reads all nodes and edges from FalkorDB via 12 Cypher queries on every tool call. | `pytest tests/test_tools.py` & `python data\seed.py` |
| 2 | **Every traversable connection identifies its underlying `RoadSegment`** | Invariant check in `seed.py` (`every CONNECTED_BY names an existing RoadSegment: violations=0`). | `python data\seed.py --verify-only` |
| 3 | **Active closures block traversal without mutating baseline road topology** | `Snapshot.blocked_map()` filters out `:CONNECTED_BY` edges whose `segment_id` is blocked by an active `ClosureEvent`. | `test_avoidance_of_blocked_segments` in `tests/test_tools.py` |
| 4 | **Newly unreachable settlements are distinguished from pre-existing baseline disconnections** | `find_cut_off_places` computes `reach(G_baseline, dimapur) - reach(G_closed, dimapur)` and reports `already_unreachable_in_baseline` separately. | `test_cutoff_distinguishes_preexisting_unreachable` in `tests/test_tools.py` |
| 5 | **Directed reachability is distinguished from undirected connected components** | `find_cut_off_places` reports both directed reachability from Dimapur and undirected `weakly_connected_components`. | `test_directed_reachability_vs_components` in `tests/test_tools.py` |
| 6 | **Minimum-travel-time routing uses weighted Dijkstra (`travel_time_min`), never unweighted hop counts** | `algorithms.route` runs `nx.dijkstra_path(G, a, b, weight="w")` and excludes edges with missing/invalid weights. | `test_weighted_shortest_path_beats_fewer_hops` in `tests/test_tools.py` |
| 7 | **Hospital accessibility is computed over the road network, never assumed from Euclidean proximity** | `find_unreachable_hospitals` computes road-network paths from each affected settlement to each hospital's `:LOCATED_AT` place node. | `test_hospital_reachability` in `tests/test_tools.py` |
| 8 | **Ambiguous locations (highway km markers, coordinate-only inputs, multi-segment junctions) require confirmation** | `LocationResolver.resolve_location` returns `status="ambiguous"`, `requires_clarification=True`, and does not mutate the graph. | `test_post_closure_ambiguous_does_not_mutate_graph` in `tests/test_api.py` |
| 9 | **Weak links are ranked by actual shortest-path betweenness centrality** | `find_weak_links` computes normalized node and edge betweenness centrality weighted by `travel_time_min`. | `test_betweenness_ranking` in `tests/test_tools.py` |
| 10 | **Historical closures persist and duplicate requests are idempotent** | `apply_closure` deduplicates identical active requests; `remember_closure` uses `MERGE (b)-[:FAILED_IN {event_id}]->(e)`. | `tests/test_phase7_history_seasonal.py` |
| 11 | **All 12 graph tools are discovered and invoked over the official MCP SDK** | `MCPToolClient` uses `mcp.client.session.ClientSession` (`tools/list` and `tools/call`) over stdio or memory streams. | `tests/test_mcp.py` |
| 12 | **Community alerts are strictly synthetic drafts (`status="draft"`, `sent=False`)** | `draft_alerts` and `GET /alerts/{id}/export` enforce `status="draft"` and `sent=False` on every alert. | `tests/test_phase8_polish.py` |
| 13 | **Resolving a closure restores the exact baseline network state** | `audit_closure` verifies SHA-256 `baseline_fp` on every segment and `network_fingerprint` on the `Dataset` node. | `Step 12` of `python scripts\run_demo.py` |

---

## 5. Limitations Statement

1. **Planning Aid, Not Emergency Dispatch**: All routes, travel times, and risk rankings are modeled estimates derived from public OpenStreetMap geometry and historical press/agency reports. Never use this prototype to dispatch emergency vehicles without verifying road status with official Nagaland authorities (NSDMA / BRO / Traffic Police).
2. **Assumed Planning Speeds on Untagged Roads**: Of the `1,045` road segments in the Dimapur–Kohima extract, `164` carry explicit OSM `maxspeed` tags, `10` have mixed tagging, and `871` use documented hill-road class assumptions (`trunk: 35 km/h`, `primary: 30 km/h`, `secondary: 25 km/h`, `tertiary: 20 km/h`). Every segment records its exact `travel_time_basis` (`tagged`, `assumed`, `mixed`).
3. **Nearest-Vertex Feature Attachment**: Settlements and hospitals are snapped to the nearest road network vertex within documented thresholds (`town: 2,500 m`, `village: 1,000 m`, `hospital: 800 m`). Seven features beyond those thresholds (6 villages and 1 hospital) are left unattached and explicitly listed in `data/cache/report.md` rather than silently forced onto distant roads.
4. **Missing Demographic & Clinical Attributes Stay Unknown**: OpenStreetMap lacks population figures for `75` villages and bed/trauma-level tags for the `24` hospitals. In accordance with our non-fabrication rule, these fields remain `null` until hand-verified via `data/curated/verification.json`.
5. **Whole-Graph Export Architecture**: Each tool call exports the 2,019-node corridor graph from FalkorDB via 12 Cypher queries so blocked-segment edge filtering and segment-level edge betweenness run with full provenance. This is fast at corridor scale (~2,000 nodes) but would require bounded subgraph projection for country-scale networks.

---

## 6. Additional Phase 9 Assets
- **Judge's Guide**: [`docs/JUDGES_GUIDE.md`](JUDGES_GUIDE.md)
- **Technical Blog Post Draft**: [`docs/BLOG_POST_DRAFT.md`](BLOG_POST_DRAFT.md)
- **Pitch Deck Outline**: [`docs/PITCH_DECK_OUTLINE.md`](PITCH_DECK_OUTLINE.md)
