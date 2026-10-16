# Phase 3: Location Resolver and Graph-Backed Agent

Code: `backend/app/agent/` (`resolver.py`, `orchestrator.py`, `llm.py`, `cli.py`, `__init__.py`) and `backend/app/graph/in_memory.py`.
MCP transport is Phase 4 and FastAPI tool endpoints are Phase 5.

## 1. Location Resolver (`backend/app/agent/resolver.py`)

`LocationResolver(store).resolve_location(...)` reads the current graph snapshot from `FalkorStore` with the 12 Cypher read statements in `app/graph/queries.py` and returns the standard tool envelope `{tool, result, cypher, params, explanation, graph, warnings, algorithm}`.

### Resolution rules and ambiguity gating

1. **Explicit confirmation (`target_segment_id` / `target_bridge_id`)**:
   - Validates that the segment and/or bridge exist in the graph and that a specified bridge sits `ON` the segment.
   - Resolves with `status="resolved"`, `requires_clarification=False`.
2. **Explicit identifiers or named bridges in text**:
   - Matches segment IDs (`s2`, `seg-...`) and bridge IDs or names (`BR1`, `"Bridge One"`).
   - Resolves when a single segment is identified; returns `status="ambiguous"` when multiple segments match.
3. **Endpoint pairs (`between X and Y`, `from X to Y`, or two named places)**:
   - Checks direct `RoadSegment`s connecting the two places (filtered by `on_nh29` or `carries_bridges` when mentioned).
   - If not directly adjacent, walks the baseline corridor path between the two places. A multi-segment stretch returns `status="ambiguous"` with the ordered path segments unless narrowed to a single segment by a bridge constraint (e.g. `"Bridge collapse between Dimapur and Kohima"`).
4. **Named roads (`s.name`)**:
   - Matches road names in the graph. A road name shared across multiple segments (such as `"Synthetic Highway"`) returns `status="ambiguous"` with all matching segments.
5. **Single place proximity (`near Alpha`, `at Bravo`, `at J2`)**:
   - Finds incident road segments touching the place. If only one matching segment touches the place (e.g. `Alpha` on spur `s6`), resolves to that segment; if multiple segments meet at that place (`Bravo` has `s4, s5`; `J2` has `s2, s3, s5, s6`), returns `status="ambiguous"` and refuses to pick an arbitrary segment.
6. **Kilometre / chainage markers (`"Landslide near NH29 kilometre 42."`, `"chainage km 163+980"`)**:
   - Computes cumulative road distance (`length_km`) from the `Dimapur` reference node along the corridor.
   - Because OpenStreetMap and the seeded graph do not store verified official highway milestone posts (and official chainage numbers can differ from cumulative geometry length from Dimapur), chainage matches **always** return `status="ambiguous"`, `requires_clarification=True`, rank the closest candidate segments with their `cumulative_km_from_dimapur` intervals, and warn if the requested marker exceeds the modelled corridor length.
7. **Geographic coordinates (`lat`, `lon`)**:
   - Computes point-to-segment and point-to-bridge distances in metres.
   - Per Section 10 (*"Nearest-neighbor search may use geographic coordinates, but proximity alone is not sufficient to establish that an event occurred on a particular road"*), coordinate-only queries **always** return `status="ambiguous"`, `requires_clarification=True` with ranked nearby candidates.
8. **Unmatched locations**:
   - Returns `status="no_match"` with zero candidates and an explicit warning when a named place or road does not exist in the seeded corridor graph.

## 2. Agent Loop (`backend/app/agent/orchestrator.py`)

`LandslideAgent(store, ...).run(prompt, ...)` executes the 11-step disaster workflow:

1. Parse the natural-language event (`parse_event`).
2. Call `resolve_location` against the corridor graph.
3. Surface candidate road segments and bridges with provenance.
4. **Ambiguity gate**: if `requires_clarification` is `True` (or `status == "no_match"`), stop immediately without modifying the graph (`status="clarification_required"` or `"no_match"`).
5. Once the target segment is resolved or confirmed (`--target-segment`), call `apply_closure`.
6. Call `find_cut_off_places` (directed reachability from Dimapur, Kohima secondary reachability, pre-existing islands, and undirected components).
7. Call `find_unreachable_hospitals` (hospital reachability through road access places for newly cut-off settlements).
8. Call `reroute` and `explain_path` (for the primary `Dimapur -> Kohima` corridor route and the local segment bypass).
9. Call `find_weak_links` (travel-time-weighted betweenness centrality) and `seasonal_risk` (when a month is specified).
10. Call `draft_alerts` (synthetic SMS-ready drafts with supporting graph paths).
11. Assemble a structured report containing `claims`, `provenance`, `warnings`, `tool_trace`, and `markdown_report`.

The agent also selects targeted tools when asked for routing (`reroute` + `explain_path`), weak links (`find_weak_links`), seasonal risk (`seasonal_risk`), historical memory (`remember_closure`), or closure resolution and baseline verification (`clear_closure` + `audit_closure`).

### Provenance on every claim

Every entry in `report["claims"]` records:
- `supported_by_tool`: the tool that produced the conclusion.
- `supporting_data`: the exact graph path, node/segment IDs, travel-time calculation, or centrality score.
- `provenance.algorithm`: the algorithm name and NetworkX/Cypher implementation details.
- `provenance.cypher_executed`: the exact Cypher statements executed during that tool call.
- `provenance.sources`: the graph entities (`id`, `label`, `confidence`, `data_status`, `source_url`) supporting the claim.

### Deterministic vs optional LLM planner

By default (`--planner deterministic`), tool selection runs deterministically with zero external API keys required. If `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is configured and `--planner auto` is selected, `llm.py` passes `SYSTEM_PROMPT` and `TOOL_SCHEMAS` to the model, while `orchestrator.py` enforces the ambiguity gate in code and builds all report data and claims strictly from the executed tool envelopes.

