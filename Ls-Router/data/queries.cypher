// Landslide Router - inspection queries for FalkorDB Browser (http://localhost:3000), graph "landslide".
// Run them one at a time. They only read. NOT executed in the authoring sandbox (no database there):
// if one errors in your FalkorDB version, tell me which and I will adjust it.
//
// MODEL (Phase 1)
//   (RoadSegment)-[:SEGMENT_OF {role: 'nh29'|'connector'}]->(Corridor)
//   (Place)-[:CONNECTED_BY {segment_id, travel_time_min, direction}]->(Place)   one per allowed direction
//   (Bridge)-[:ON]->(RoadSegment)        a bridge is ON every segment that carries it
//   (Hospital)-[:LOCATED_AT {snap_distance_m, method}]->(Place)   road access point (added relationship)
//   (Season)-[:AFFECTS {basis}]->(RoadSegment)
//   (ClosureEvent {data_status:'observed'})-[:NEAR_PLACE]->(Place)   only when a source names the place
//   NEAREST_HOSPITAL, BLOCKS, FAILED_IN, ALERT nodes: not populated in Phase 1 (see data/README.md)
// Unknown values are ABSENT properties (never guessed).

// 1. What is in the graph
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n ORDER BY label;

// 2. Data source, license, retrieval time, dataset kind (real vs synthetic)
MATCH (d:Dataset) RETURN d;

// 3. The road network (use Browser's graph view)
MATCH (a:Place)-[c:CONNECTED_BY]->(b:Place) RETURN a, c, b;

// 4. Every connection with the segment it depends on (the connectivity invariant)
MATCH (a:Place)-[c:CONNECTED_BY]->(b:Place)
MATCH (s:RoadSegment) WHERE s.id = c.segment_id
RETURN a.name AS from_name, a.id AS from_id, b.name AS to_name, b.id AS to_id, c.direction, c.travel_time_min,
       s.id AS segment, s.on_nh29, s.is_bidirectional, s.travel_time_basis
ORDER BY s.id, c.direction;

// 5. Integrity: connections whose segment_id matches no RoadSegment (expect 0)
MATCH (s:RoadSegment) WITH collect(s.id) AS ids
MATCH ()-[c:CONNECTED_BY]->() WHERE NOT c.segment_id IN ids RETURN count(c);

// 6. Segments: NH29 vs connectors, length, time, and how the time was obtained
MATCH (s:RoadSegment)
RETURN s.id, s.name, s.ref, s.highway, s.on_nh29, s.length_km, s.travel_time_min, s.travel_time_basis, s.is_bidirectional, s.surface
ORDER BY s.on_nh29 DESC, s.id;

// 7. Segments with no travel time (routing must reject these)
MATCH (s:RoadSegment) WHERE s.travel_time_min IS NULL RETURN s.id, s.travel_time_basis;

// 8. Settlements and junctions: type, confidence, how far the record was moved onto the road
MATCH (p:Place) RETURN p.type, p.confidence, p.data_status, count(p) AS n ORDER BY p.type;
MATCH (p:Place) WHERE p.type <> 'junction'
RETURN p.name, p.type, p.reference_key, p.population, p.snap_distance_m, p.source_url ORDER BY p.snap_distance_m DESC;

// 9. Hospitals, their road access point, and what is unknown
MATCH (h:Hospital) OPTIONAL MATCH (h)-[l:LOCATED_AT]->(p:Place)
RETURN h.name, h.beds, h.trauma_level, h.confidence, h.source_url, p.id AS access_place, l.snap_distance_m;

// 10. Bridges and the segment(s) they sit on
MATCH (b:Bridge)-[:ON]->(s:RoadSegment)
RETURN b.id, b.name, b.bridge_tag, b.length_m, b.monsoon_risk, s.id AS segment, s.on_nh29;

// 11. Documented historical events with sources (observed, not simulated)
MATCH (e:ClosureEvent) RETURN e.id, e.started_at, e.date_precision, e.cause, e.data_status, e.confidence, e.location_text, e.source_urls
ORDER BY e.started_at;
MATCH (e:ClosureEvent)-[n:NEAR_PLACE]->(p:Place) RETURN e.id, p.name, n.basis;

// 12. Verification status: nothing is 'verified' until a person says so with a date and a source
MATCH (n) WHERE n.confidence IS NOT NULL RETURN labels(n)[0] AS label, n.confidence, count(n) AS n ORDER BY label;
MATCH (n) WHERE n.confidence = 'verified' RETURN labels(n)[0] AS label, n.id, n.last_verified, n.verification_source_url;

// 13. Simulated vs observed (Phase 1 contains no simulated events; Phase 2 closures will appear here)
MATCH (e:ClosureEvent) RETURN e.data_status, count(e) AS n;

// 14. Seasons
MATCH (s:Season)-[a:AFFECTS]->(g:RoadSegment) RETURN s.name, s.start_month, s.end_month, a.basis, count(g) AS segments;

// 15. Indexes (created by seed.py; shown here for reference, safe to ignore errors if they exist)
// CREATE INDEX FOR (n:Place) ON (n.id);
// CREATE INDEX FOR (n:RoadSegment) ON (n.id);

// ---- Phase 2 additions -------------------------------------------------------------------------------
// 16. Closures: simulated (created by apply_closure) vs observed (seed). status 'active' blocks; 'resolved' and 'historical' do not.
MATCH (e:ClosureEvent) RETURN e.id, e.data_status, e.status, e.started_at, e.ended_at, e.cause, e.remembered_at ORDER BY e.data_status, e.id;

// 17. What each closure blocks (BLOCKS links are kept after a closure is resolved, as history)
MATCH (e:ClosureEvent)-[:BLOCKS]->(t) RETURN e.id, e.status, labels(t)[0] AS target_label, t.id AS target ORDER BY e.id;

// 18. Segments that are blocked RIGHT NOW (only active closures count)
MATCH (e:ClosureEvent {status: 'active'})-[:BLOCKS]->(s:RoadSegment) RETURN s.id, collect(e.id) AS blocked_by;

// 19. Bridge failure history; the event shows whether it is simulated or observed
MATCH (b:Bridge)-[f:FAILED_IN]->(e:ClosureEvent) RETURN b.id, f.year, e.id, e.data_status;

// 20. Draft alerts and the segments they route over (never sent)
MATCH (a:Alert)-[:TARGETS]->(p:Place) OPTIONAL MATCH (a)-[:ROUTED_VIA]->(s:RoadSegment)
RETURN a.id, a.status, a.channel, p.name, collect(s.id) AS routed_via, a.text;

// 21. Baseline fingerprint stored at seed time (audit_closure compares against it)
MATCH (d:Dataset) RETURN d.id, d.network_fingerprint;
