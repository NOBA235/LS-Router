# Pitch Deck Outline — Landslide Router

*Graph Hacks: Context for AI Agents | Track 1: Agents That Act on Connected Data | FalkorDB*

---

## Slide 1 — Title & Mission
- **Title**: **Landslide Router** — Graph-Grounded Monsoon Closure & Emergency Access Planning for the Dimapur–Kohima Corridor (NH29, Nagaland, India)
- **Track**: Track 1 — Agents That Act on Connected Data (FalkorDB)
- **Core Principle**: *"The graph must shape decisions, not decorate them."*
- **Safety Banner**: *Planning aid, not emergency dispatch. Verify conditions with official authorities before acting.*

---

## Slide 2 — The Real-World Problem on NH29
- **Context**: NH29 is the critical mountain artery linking Dimapur (rail/air hub) to Kohima (state capital) and 76+ surrounding villages.
- **Monsoon Reality (June–September)**: Recurrent rockfalls, mudslides, and bridge washouts (Pagala Pahar, Chathe Bridge, Pherima, Piphema, Dzüdza) sever road links.
- **The Decision Gap**: When a road segment or bridge fails, planners must immediately know:
  1. Which settlements are newly isolated vs reachable via backroads?
  2. Which hospitals remain reachable over the road network?
  3. What is the fastest valid detour and exact time penalty?
  4. Which communities need alert drafts right now?

---

## Slide 3 — Why Standalone LLMs Fail at Disaster Routing
- **Hallucinated Topology**: LLMs guess road connectivity from prose rather than traversing edges.
- **Silent Snapping**: A vague report like *"Landslide near NH29 km 42"* gets silently mapped to a random location without warning the operator.
- **Unverifiable Numbers**: Standalone prompts invent travel times (`"adds ~45 minutes"`) with no mathematical proof or source attribution.
- **Our Solution**: Pair an MCP-orchestrated agent with a seeded **FalkorDB property graph** (`2,019` real nodes, `4,177` relationships) where every claim is backed by Cypher queries and deterministic graph algorithms.

---

## Slide 4 — FalkorDB Graph Schema & Real OpenStreetMap Pipeline
- **Nodes (`2,019` seeded from OpenStreetMap Overpass + curated Nagaland reports)**:
  - `Place` (`814`): `76` villages, `4` towns (Dimapur, Kohima, Chümoukedima, Medziphema), `734` road junctions
  - `RoadSegment` (`1,045`): `100` NH29 trunk segments (`189.5 km`) + hill connectors
  - `Bridge` (`126`): Mapped via `(:Bridge)-[:ON]->(:RoadSegment)`
  - `Hospital` (`24`): Mapped via `(:Hospital)-[:LOCATED_AT]->(:Place)` with explicit `snap_distance_m`
  - `ClosureEvent` (`7` observed historical + runtime simulated closures)
  - `Season` (`monsoon`, months 6–9), `Corridor`, `Dataset` (with SHA-256 `network_fingerprint`)
- **Relationships (`4,177`)**:
  - `(:Place)-[:CONNECTED_BY {segment_id, travel_time_min, direction}]->(:Place)` (`1,935` directed edges)
  - `(:ClosureEvent)-[:BLOCKS]->(:RoadSegment)`
  - `(:Bridge)-[:FAILED_IN {year}]->(:ClosureEvent)`
  - `(:Alert)-[:TARGETS]->(:Place)` & `(:Alert)-[:ROUTED_VIA]->(:RoadSegment)`

---

## Slide 5 — System Architecture (FalkorDB + MCP + FastAPI + MapLibre)
- **Data Layer**: FalkorDB Cloud / Docker (`landslide` graph) + idempotent `seed.py` with cryptographic baseline fingerprints.
- **Tool Layer (12 Tools via Official Python MCP SDK)**:
  - `resolve_location`, `apply_closure`, `clear_closure`, `find_cut_off_places`, `reroute`, `explain_path`, `find_unreachable_hospitals`, `find_weak_links`, `seasonal_risk`, `draft_alerts`, `remember_closure`, `audit_closure`.
- **API & Privacy Layer**: FastAPI (`backend/app/main.py`) with Pydantic validation, PII redaction (`backend/app/privacy.py`), timeline replay (`/replay/{event_id}`), and draft alert export (`/alerts/{event_id}/export`).
- **Workbench UI**: Three-pane React + TypeScript + MapLibre GL JS interface (Controls & Timeline | Interactive Corridor Map | "Why?" Provenance & Cypher Inspector).

---

## Slide 6 — Live 3-Minute Demo (Actual Numbers from the 2,019-Node Graph)
1. **Baseline Corridor**: Dimapur → Kohima fastest route is **`125.2 min`** (`70.101 km` across `53` segments).
2. **Ambiguity Gating**: Input *"Landslide at NH29 near kilometre 42"* → Resolver refuses to guess (`status="ambiguous"`), surfacing ranked candidate segments along cumulative NH29 distance.
3. **Trunk Closure & Detour (`seg-569069510-569069527-1353575333`)**:
   - Reroutes Dimapur → Kohima via secondary hill connectors: **`215.23 min` (`+90.03 min` detour)**.
4. **Branch Closure & Community Alerts (`seg-2469699767-6286548534-239157848`)**:
   - Cuts off **`3` settlements** (`Kezo Town`, `Chakhabama`, `Kikruma`), checks hospital reachability across all `24` hospitals, and generates **`3` draft SMS alerts** (`status="draft"`, `sent=False`).
5. **Weak Links & Monsoon Risk**:
   - Identifies Rank `#1` segment (`betweenness = 0.458767`) and Rank `#1` bridge (`bridge-way-1073679103`, `betweenness = 0.457556`), plus July monsoon risk rankings.
6. **Resolution & Cryptographic Audit**:
   - Clears both closures and verifies `returned_to_baseline: True` (`0` discrepancies).

---

## Slide 7 — Trust, Provenance & Safety Guardrails
- **Explicit Confidence Badges**: Every entity is badged `verified` vs `unverified` and `observed` vs `derived` vs `simulated` vs `draft`.
- **No Silent Fabrication**: Missing OSM attributes (`population`, `beds`, `trauma_level`, `maxspeed`) remain `null`/disclosed; schematic segments without polyline coordinates are rendered as dashed lines and marked `schematic_unverified`.
- **Draft-Only Alerts**: Alert exports (`.json` and `.txt`) enforce `status="draft"` and `sent=False`—no automated external dispatch.
- **Non-Predictive Risk Language**: Historical counts are presented as *"elevated modeled risk; historical pattern warrants review"*, never as failure probability predictions.

---

## Slide 8 — Honest Limitations & Future Roadmap
- **Current Limitations**:
  - Whole-graph Cypher export per tool call (fast at ~2,000 nodes; larger regional graphs will require bounded subgraph queries).
  - Travel times on untagged OSM roads use documented class-based planning speeds (`trunk: 35 km/h`, `primary: 30 km/h`, `secondary: 25 km/h`, `tertiary: 20 km/h`).
  - Settlement and hospital attachment uses nearest-vertex snapping thresholds (`town: 2.5 km`, `village: 1.0 km`, `hospital: 0.8 km`).
- **Next Steps**:
  - Hand-verifying priority NH29 bridges and Nagaland CMHIS hospital capacities via `data/curated/verification.json`.
  - Integrating NSDMA / BRO official road-bulletin feeds into the `LocationResolver` confirmation queue.
