"""Every Cypher statement the tools run. Constants, so tool responses can quote exactly what was executed.
Written against FalkorDB's documented Cypher; see docs/PHASE2.md for what has and has not been run against a live server."""

# ----- reads (the export that the algorithms run on) -----
Q_PLACES = ("MATCH (p:Place) RETURN p.id, p.name, p.type, p.lat, p.lon, p.reference_key, p.confidence, p.source_url, "
            "p.data_status ORDER BY p.id")
Q_SEGMENTS = ("MATCH (s:RoadSegment) OPTIONAL MATCH (s)-[:SEGMENT_OF]->(c:Corridor) "
              "RETURN s.id, s.name, s.ref, s.highway, s.length_km, s.travel_time_min, s.travel_time_basis, s.is_bidirectional, "
              "s.on_nh29, s.monsoon_risk, s.confidence, s.source_url, s.data_status, s.from_place_id, s.to_place_id, "
              "s.baseline_fp, c.id ORDER BY s.id")
Q_CONNECTIONS = ("MATCH (a:Place)-[c:CONNECTED_BY]->(b:Place) RETURN a.id, b.id, c.segment_id, c.travel_time_min, c.direction "
                 "ORDER BY c.segment_id, c.direction, a.id")
Q_HOSPITALS = ("MATCH (h:Hospital) OPTIONAL MATCH (h)-[l:LOCATED_AT]->(p:Place) "
               "RETURN h.id, h.name, h.lat, h.lon, h.beds, h.trauma_level, h.confidence, h.source_url, h.data_status, p.id, "
               "l.snap_distance_m ORDER BY h.id, p.id")
Q_BRIDGES = ("MATCH (b:Bridge) OPTIONAL MATCH (b)-[:ON]->(s:RoadSegment) "
             "RETURN b.id, b.name, b.road_name, b.lat, b.lon, b.monsoon_risk, b.confidence, b.source_url, s.id ORDER BY b.id, s.id")
Q_EVENTS = ("MATCH (e:ClosureEvent) RETURN e.id, e.description, e.status, e.data_status, e.started_at, e.ended_at, e.cause, "
            "e.origin, e.confidence, e.remembered_at, e.source_url, e.location_text ORDER BY e.id")
Q_BLOCKS_SEGMENT = "MATCH (e:ClosureEvent)-[:BLOCKS]->(s:RoadSegment) RETURN e.id, s.id ORDER BY e.id, s.id"
Q_BLOCKS_BRIDGE = "MATCH (e:ClosureEvent)-[:BLOCKS]->(b:Bridge) RETURN e.id, b.id ORDER BY e.id, b.id"
Q_FAILED_IN = "MATCH (b:Bridge)-[f:FAILED_IN]->(e:ClosureEvent) RETURN b.id, e.id, f.year ORDER BY b.id, e.id"
Q_NEAR_PLACE = "MATCH (e:ClosureEvent)-[:NEAR_PLACE]->(p:Place) RETURN e.id, p.id ORDER BY e.id, p.id"
Q_SEASONS = ("MATCH (se:Season) OPTIONAL MATCH (se)-[:AFFECTS]->(s:RoadSegment) "
             "RETURN se.id, se.name, se.start_month, se.end_month, s.id ORDER BY se.id, s.id")
Q_DATASET = "MATCH (d:Dataset) RETURN d.id, d.kind, d.network_fingerprint, d.retrieved_at, d.attribution ORDER BY d.id"
Q_PROCEDURES = "CALL dbms.procedures() YIELD name, mode RETURN name, mode ORDER BY name"

SNAPSHOT_QUERIES = [("places", Q_PLACES), ("segments", Q_SEGMENTS), ("connections", Q_CONNECTIONS), ("hospitals", Q_HOSPITALS),
                    ("bridges", Q_BRIDGES), ("events", Q_EVENTS), ("blocks_segment", Q_BLOCKS_SEGMENT),
                    ("blocks_bridge", Q_BLOCKS_BRIDGE), ("failed_in", Q_FAILED_IN), ("near_place", Q_NEAR_PLACE),
                    ("seasons", Q_SEASONS), ("dataset", Q_DATASET)]

# ----- writes -----
# One statement creates the event and its BLOCKS link; if the segment does not exist nothing is created.
W_CREATE_CLOSURE = ("MATCH (s:RoadSegment {id: $segment_id}) "
                    "CREATE (e:ClosureEvent {id: $event_id, description: $description, cause: $cause, status: 'active', "
                    "data_status: 'simulated', origin: 'simulation', started_at: $now}) "
                    "CREATE (e)-[:BLOCKS]->(s) RETURN e.id")
W_CREATE_CLOSURE_WITH_BRIDGE = ("MATCH (s:RoadSegment {id: $segment_id}), (b:Bridge {id: $bridge_id}) "
                                "CREATE (e:ClosureEvent {id: $event_id, description: $description, cause: $cause, status: 'active', "
                                "data_status: 'simulated', origin: 'simulation', started_at: $now}) "
                                "CREATE (e)-[:BLOCKS]->(s) CREATE (e)-[:BLOCKS]->(b) RETURN e.id")
# Matches only an active simulated event, so a repeat or a wrong id changes nothing. BLOCKS links are kept as history.
W_RESOLVE_CLOSURE = ("MATCH (e:ClosureEvent {id: $event_id, status: 'active', data_status: 'simulated'}) "
                     "SET e.status = 'resolved', e.ended_at = $now RETURN e.id")
W_MARK_REMEMBERED = ("MATCH (e:ClosureEvent {id: $event_id}) SET e.remembered = true, "
                     "e.remembered_at = coalesce(e.remembered_at, $now) RETURN e.remembered_at")
W_RECORD_FAILURE = ("MATCH (b:Bridge {id: $bridge_id}), (e:ClosureEvent {id: $event_id}) "
                    "MERGE (b)-[f:FAILED_IN]->(e) SET f.year = $year RETURN b.id")
W_UPSERT_ALERT = ("MATCH (p:Place {id: $place_id}) MERGE (a:Alert {id: $alert_id}) "
                  "SET a.text = $text, a.channel = 'sms-draft', a.target_place_id = $place_id, a.status = 'draft', "
                  "a.simulation = true, a.closure_event_id = $event_id, a.supporting_json = $supporting, "
                  "a.created_at = coalesce(a.created_at, $now), a.updated_at = $now "
                  "MERGE (a)-[:TARGETS]->(p) RETURN a.id")
W_CLEAR_ALERT_ROUTES = "MATCH (a:Alert {id: $alert_id})-[r:ROUTED_VIA]->(:RoadSegment) DELETE r"
W_LINK_ALERT_ROUTE = ("MATCH (a:Alert {id: $alert_id}), (s:RoadSegment {id: $segment_id}) MERGE (a)-[:ROUTED_VIA]->(s) "
                      "RETURN s.id")
