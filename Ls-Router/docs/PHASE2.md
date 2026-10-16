# Phase 2: graph tools

Code: `backend/app/graph/` (`tools.py` the eleven tools, `store.py` the only FalkorDB access, `queries.py` every Cypher string,
`algorithms.py` the only NetworkX module, `snapshot.py`, `fingerprint.py`, `cli.py`). Nothing here is exposed over HTTP or MCP yet.

## How a tool call works

1. **Read** the whole graph from FalkorDB with Cypher (12 read statements). Nothing is cached between calls, so FalkorDB stays the
   source of truth for topology and closure state.
2. **Compute** on that export: closures are applied *while building* the graph, so a blocked segment's connections never exist
   in it. Routing, reachability, components and centrality use NetworkX.
3. **Write** (only closure/alert/history tools) with Cypher, then re-read so the reported state is the state the graph is in.
4. Return the envelope `{tool, result, cypher, params, explanation, graph, warnings, algorithm}`. `cypher` is exactly the list of
   statements sent (verified by test), `params` holds the tool inputs and each statement's parameters, `graph` is the supporting
   subgraph (nodes and edges with source/confidence), `algorithm` names what ran.

### Why NetworkX instead of FalkorDB's algorithms (documented fallback)
FalkorDB has `algo.SPpaths` (weighted paths), `algo.WCC` and `algo.betweenness` (nodes only) per its documentation. None can drop an
edge because the `RoadSegment` it depends on is blocked by an active `ClosureEvent`, and the brief needs segment-level centrality. So
the closure filter is applied during export and NetworkX does the algorithm. `probe_capabilities` reports which `algo.*`
procedures your server really has; I could not run it against a server here.

## Semantics (decisions worth knowing)
- **Blocked set** = segments of every `ClosureEvent` with `status='active'`, directly or via a blocked `Bridge`'s `ON` segments.
  `resolved` (cleared) and `historical` (observed) events block nothing but keep their `BLOCKS` links as history. Two active closures on
  one segment: clearing one leaves it blocked until the other is cleared.
- **Baseline** = the network with no closures at all.
- **Traversable** = a `CONNECTED_BY` edge whose `segment_id` names an existing segment that is not blocked. A connection naming a missing segment
  is never traversable (and is reported).
- **Reachability** (tools 3, 5, 8) is *directed* from Dimapur, baseline vs now. "Newly cut off" = reachable in baseline, not now.
  Places already unreachable in the baseline are listed separately. The undirected component analysis is reported separately and labelled;
  every piece of a split component is a "new component" and `split_off_from_origin_component` names the ones cut from Dimapur's.
- **`attributable_to_this_event`**: cut off now, but reachable if this event alone were cleared (matters with several closures).
- **Routing** minimizes the sum of `travel_time_min` over directed usable edges (Dijkstra). Missing/NaN/zero/negative/non-numeric weights are excluded
  and reported, never treated as 0; if only such paths exist the result is `no_route` with that reason, while connectivity still counts the road.
  Parallel segments keep the fastest. Times are estimates (assumed or tagged speeds).
- **`avoid_event_id`** (tools 4, 9): all active closures are always excluded; naming an event additionally excludes its segments even if it is resolved
  (what-if), with a warning.
- **Hospitals** are reached through their road access `Place` (`LOCATED_AT`); `NEAREST_HOSPITAL` is not used or populated. Tool 5 judges
  each newly cut-off village/town against each hospital: `reachable`, `newly_unreachable`, or `unreachable_in_baseline_too` (a village behind a one-way road
  may never have reached some hospitals). Over-threshold is a separate list. No capability is assumed.
- **Weak links**: betweenness on the baseline corridor graph, weighted by travel time, directed; a segment's score sums its two directions; a bridge
  inherits its segments' maximum. Parallel segments keep only the fastest. It is *not* a failure probability. Each top item also reports which places would be
  cut off from Dimapur if closed, plus example shortest paths.
- **Seasonal risk** ranking is lexicographic: risk label (high>medium>low>none), then OBSERVED in-season events linked to the item, then SIMULATED ones, then id.
  No probabilities. Observed events with no `BLOCKS`/`FAILED_IN` link affect nothing (listed). Phase 7 will refine the scoring rule.
- **`remember_closure`** marks the event remembered once (`remembered_at` never changes) and records `FAILED_IN` only for bridges the closure explicitly targeted,
  with the event's start year. A closure on a segment that merely carries a bridge records nothing.
- **`draft_alerts`** writes `Alert` nodes with `status='draft'`, `channel='sms-draft'` (`TARGETS` the place, `ROUTED_VIA` the segments of the hospital route used);
  regenerating updates the same node. Nothing is sent. A hospital route is mentioned only if the closed network really has one.
- **`audit_closure`** says "back to baseline" only if the event is resolved, no other closure is active, every connection follows its segment, and every
  stored baseline fingerprint still matches. `None` means it cannot be verified (data seeded before Phase 2 has no fingerprints).

## Changes to Phase 1
`normalize` now adds `baseline_fp` to each segment and `network_fingerprint` to the `Dataset` node (identical hashing code in `data/osm_normalize.py` and
`backend/app/graph/fingerprint.py`, kept equal by a test). Re-run `normalize` then `seed`.

## What has and has not been verified
- Run here: all logic against an in-memory graph that answers only the exact Cypher strings in `queries.py`, 20 scenario checks built on hand-computed
  topologies, a randomized oracle test (600 random routes and cut-off sets vs exhaustive path search and naive BFS), and mutation tests.
- **Not run here: any Cypher against a real FalkorDB** (no database in the authoring sandbox). `backend/tests/test_tools_falkordb.py` runs the same 19
  scenario checks live; until you run it, treat the Cypher text as written-to-documentation but unproven. Likeliest trouble spots: `OPTIONAL MATCH` row
  shapes, `coalesce` inside `SET`, `MATCH ... CREATE` in one statement, and `CALL dbms.procedures()`.

## Limits
- Whole-graph export per call: fine at ~200 nodes, not a design for large graphs.
- Betweenness uses exact float equality for ties; weights are rounded to 2 decimals upstream.
- Alerts for places that stop being cut off are not deleted when regenerated.
- Seasons currently link to every segment by assumption, so seasonal ranking depends on risk labels and linked events, which the real data mostly lacks (no
  `monsoon_risk` labels yet; the seven observed events have no segment links until someone reviews and adds them).
