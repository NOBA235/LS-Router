# When the Mountain Moves: Building a Graph-Grounded Disaster Routing Agent on FalkorDB and MCP

*Draft for Graph Hacks — Track 1: Agents That Act on Connected Data (FalkorDB)*

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

---

## 1. The Problem: Why Unassisted LLMs Fail When a Highway Closes

National Highway 29 (NH29) is the primary lifeline connecting **Dimapur** (Nagaland's rail and air gateway in the plains) to **Kohima** (the state capital in the Naga Hills) and dozens of surrounding hill settlements. Every monsoon season (June–September), intense rainfall triggers rockfalls, mudslides, and bridge washouts along the Chathe River valley, Pagala Pahar, Pherima, Piphema, and Zubza.

When an incident report arrives—such as *"Landslide at NH29 near kilometre 42"* or *"Landslide on NH29 between Kezo Town and Chakhabama"*—planners immediately need answers to concrete topological questions:

1. Which settlements remain reachable from Dimapur, and which are newly cut off?
2. Which hospitals can affected villages still reach over the remaining road network?
3. Does a valid detour exist, and how many minutes of travel time does it add?
4. Which road segments and bridges are structural single points of failure across the corridor?
5. What community alert drafts should be prepared for isolated villages?

Ask a standalone Large Language Model these questions and it will confidently hallucinate road names, invent detour times, or guess which villages are isolated. **Topology, directed reachability, and weighted shortest paths are properties of a connected graph—not of token co-occurrence.**

Our core engineering principle for **Landslide Router** is simple:

> **The graph must shape decisions, not decorate them.**

---

## 2. Grounding the Corridor in Real Data (`2,019` Nodes in FalkorDB)

Instead of mocking a toy network, we built a reproducible, offline-capable pipeline (`data/fetch_osm.py`, `data/osm_normalize.py`, and `data/seed.py`) that ingests real **OpenStreetMap** data via the Overpass API for the Dimapur–Kohima bounding box (`25.55°N, 93.60°E` to `26.00°N, 94.25°E`) alongside curated historical landslide records (`data/curated/risk_events.json`):

- **Places (`814`)**: `76` villages, `4` towns/cities (including Dimapur and Kohima), and `734` road junctions.
- **RoadSegments (`1,045`)**: Including `100` NH29 trunk segments (`189.5 km`) and secondary/tertiary hill connectors, mapped to `1,935` directed `:CONNECTED_BY` relationships weighted by `travel_time_min`.
- **Bridges (`126`)**: Linked via `(:Bridge)-[:ON]->(:RoadSegment)` so a bridge washout automatically blocks the road segments it carries.
- **Hospitals (`24`)**: Including Naga Hospital Authority Kohima, CIHSR, and Faith Hospital, snapped to the road graph via `:LOCATED_AT` with explicit `snap_distance_m`.
- **ClosureEvents (`7` observed seed events + runtime simulated events)**: Strictly separated by `data_status: "observed"` vs `"simulated"` and `confidence: "verified"` vs `"unverified"`.

Every write in `data/seed.py` uses idempotent `MERGE` statements keyed on `id` and stores cryptographic baseline fingerprints (`baseline_fp` on every segment and `network_fingerprint` on the `Dataset` node) so the system can mathematically prove when a resolved simulation has returned the network to its exact baseline state.

---

## 3. Refusing to Guess: Ambiguity Gating in the Location Resolver

Real-world incident reports are messy. When a user enters:

```text
"Landslide at NH29 near kilometre 42."
```

OpenStreetMap does not contain verified physical highway milestone posts for NH29. Rather than silently snapping the report to an arbitrary road segment, our `LocationResolver` (`backend/app/agent/resolver.py`) computes cumulative road distance from the Dimapur reference node along the NH29 spine, ranks the nearest candidate segments (`seg-12522975151-12523010253-1038180947`, `seg-12523010253-569069527-1038180947`, `seg-569069510-569069527-1353575333`), and halts with `status: "ambiguous"` and `requires_clarification: True`.

The graph is **never mutated** until a specific `segment_id` or `bridge_id` is confirmed.

---

## 4. Twelve Graph Tools Over the Official Model Context Protocol (MCP)

We exposed all 12 graph operations through the official Python `mcp` SDK (`mcp/server.py` and `backend/app/mcp_bridge/`):

1. `resolve_location` — Multi-step resolution ladder with ambiguity gating.
2. `apply_closure` — Creates an active simulated `ClosureEvent` with idempotency protection.
3. `clear_closure` — Resolves a simulated closure while preserving historical audit trails.
4. `find_cut_off_places` — Computes directed reachability from Dimapur, distinguishing *newly unreachable* settlements from *pre-existing baseline disconnections*, alongside undirected component splits.
5. `reroute` — Computes minimum-travel-time paths (`dijkstra_path` on `travel_time_min`) while excluding any edge whose underlying `RoadSegment` is blocked.
6. `explain_path` — Breaks down every segment's travel-time contribution, provenance, and side-by-side comparison against the unblocked baseline route.
7. `find_unreachable_hospitals` — Evaluates actual road-network paths from every affected settlement to every corridor hospital (never relying on static Euclidean proximity).
8. `find_weak_links` — Ranks road segments, bridges, and places by travel-time-weighted shortest-path betweenness centrality (`betweenness_centrality` and `edge_betweenness_centrality`).
9. `seasonal_risk` — Combines monsoon exposure (June–September), observed historical closures, and remembered failures into a transparent, non-predictive ranking.
10. `draft_alerts` — Generates synthetic community SMS drafts (`status="draft"`, `sent=False`) for newly cut-off settlements.
11. `remember_closure` — Archives a closure event and links bridge failures via idempotent `MERGE (b)-[:FAILED_IN]->(e)`.
12. `audit_closure` — Verifies that resolving a closure restores all baseline segment and dataset fingerprints.

Every tool returns a uniform provenance envelope:

```json
{
  "tool": "explain_path",
  "result": { "...": "..." },
  "cypher": ["MATCH (p:Place) RETURN ...", "..."],
  "params": { "...": "..." },
  "explanation": "...",
  "graph": { "nodes": [...], "edges": [...] },
  "warnings": [...],
  "algorithm": { "name": "...", "implementation": "..." }
}
```

---

## 5. What the Real NH29 Graph Reveals

When we run our reproducible 12-step demonstration (`python scripts\run_demo.py`) against the 2,019-node NH29 corridor graph in FalkorDB:

- **Baseline Dimapur → Kohima Route**: `125.2 minutes` (`70.101 km` across `53` road segments).
- **Structural Weak Links (`find_weak_links`)**: Segment `seg-569069510-569069527-1353575333` on NH29 ranks `#1` across the entire corridor with normalized shortest-path betweenness `0.458767`, followed by `seg-569069686-6257823113-44886119` (`0.457556`), which carries the `#1` weak-link bridge `bridge-way-1073679103`.
- **Trunk Detour Impact**: Closing `seg-569069510-569069527-1353575333` forces traffic off the primary NH29 alignment onto secondary hill connectors, increasing Dimapur → Kohima travel time from **`125.2 min` to `215.23 min` (`+90.03 min` detour)**.
- **Settlement Isolation & Draft Alerts**: Closing `seg-2469699767-6286548534-239157848` (between **Kezo Town** and **Chakhabama**) cuts off **3 settlements** (`Kezo Town`, `Chakhabama`, and `Kikruma`) from Dimapur and automatically generates **3 synthetic SMS draft alerts** (`status="draft"`, `sent=False`) citing the exact blocked segment ID.
- **Mathematical Baseline Audit**: Resolving both closures and running `audit_closure` confirms `returned_to_baseline: True` with `0` fingerprint discrepancies across all `1,045` road segments.

---

## 6. Three-Pane Transparency: The "Why?" Inspector

In our React + MapLibre GL JS workbench, the map is paired with a dedicated **"Why? — Provenance & Graph Evidence"** panel. Clicking any road segment, settlement, hospital, bridge, or closure event surfaces:

- Verification and provenance badges (`verified` vs `unverified`, `observed` vs `derived` vs `simulated` vs `draft`).
- Side-by-side **Baseline Route vs Active/Detour Route** comparison cards and dashed-vs-solid map overlays.
- Non-destructive **Timeline Replay** of any historical or resolved closure event (`GET /replay/{event_id}`).
- Searchable **Data-Source & Provenance Inspector** across all 2,019 entities.
- Every Cypher statement and parameter map executed to produce the current view.

By letting FalkorDB hold the connected ground truth, MCP govern tool handoffs, and deterministic graph algorithms compute reachability and routing, **Landslide Router** turns natural-language incident reports into auditable, provenance-backed planning intelligence.
