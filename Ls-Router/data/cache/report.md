# Phase 1 normalization report

Dataset kind: real  
Retrieved: 2026-10-17T13:26:06Z  OSM base: 2026-10-17T13:24:01Z

## Counts

- corridor: 1
- dataset: 1
- season: 1
- places_total: 814
- places_village: 76
- places_town: 4
- places_junction: 734
- hospitals: 24
- bridges: 126
- segments: 1045
- connections: 1935
- observed_events: 7
- graph_nodes_total: 2019

Brief expects roughly 150-200 nodes where sources allow; this is reported, never padded.

## NH29

- {'segments': 100, 'length_km': 189.5}
- directionality: {'bidirectional_segments': 890, 'one_way_segments': 155}
- travel-time basis per segment: {'assumed': 871, 'tagged': 164, 'mixed': 10} (assumed speeds: {'motorway': 50.0, 'trunk': 35.0, 'primary': 30.0, 'secondary': 25.0, 'tertiary': 20.0, 'default': 20.0})

## Refs seen (top)

- (no ref): 742
- NH29: 76
- NH229: 33
- NH129A: 25
- NH129: 11
- NH2: 9
- NH329A: 6
- NH2;29: 3
- Bayavu Circular Road: 2
- NH2; NH29: 2

## Skipped / dropped

- edges_clipped_outside_bbox: 7284
- places_unnamed_skipped: 2
- ways_access_restricted_skipped: 4
- components without NH29 dropped: 17

## Not attached to the road network (NOT in the graph)

- place: Chedema Model (village, node/7924945335): no free road vertex within threshold, nearest vertex 1298.7 m
- place: Dihoma (village, node/8148438009): no free road vertex within threshold, nearest vertex 5362.8 m
- place: Gariphema (village, node/8148438007): no free road vertex within threshold, nearest vertex 1782.2 m
- place: Poilwa (village, node/8148134763): no free road vertex within threshold, nearest vertex 2988.0 m
- place: Rebwak (village, node/8148134762): no free road vertex within threshold, nearest vertex 2023.6 m
- place: Seiyhama (village, node/8148437998): no free road vertex within threshold, nearest vertex 2423.4 m
- hospital: Referral Hospital (node/7614502604): no road vertex within threshold, nearest vertex 839.7 m

## Missing data (left unknown, never inferred)

- places_population_unknown: 75
- hospitals_beds_unknown: 24
- hospitals_trauma_level_unknown: 24
- hospitals_unnamed: 0
- bridges_unnamed: 124
- segments_missing_travel_time: 0
- segments_without_monsoon_risk_label: 1045

## Hospital cross-check against the Nagaland CMHIS empanelled list

- source: https://cmhis.nagaland.gov.in/hospitals/2
- name hints matched: ['Naga Hospital Authority Kohima', 'Christian Institute of Health Sciences and Research', 'Faith Hospital']
- NOT found in the graph (add via curated additions after checking coordinates): ['District Hospital Dimapur', 'CHC Medziphema']

## Network

- reference places: {'dimapur': 'place-node-7418609008', 'kohima': 'place-node-3613137018'}
- connected components (by place count): [814]
- observed-event place links unresolved: [{'event_id': 'obs-2024-08-dzudza-landslide', 'place_name': 'Dzüdza', 'matches': 0}, {'event_id': 'obs-2024-09-pherima-landslide', 'place_name': 'Pherima', 'matches': 0}, {'event_id': 'obs-2025-09-nh29-chathe-paglapahar-landslides', 'place_name': 'Chümoukedima', 'matches': 0}, {'event_id': 'obs-2025-09-chathe-suspension-bridge', 'place_name': 'Chümoukedima', 'matches': 0}]

## Assumptions

- Settlements and hospitals are attached to the nearest road vertex within a distance threshold; access roads below that distance are not modelled. Distances are stored per record (snap_distance_m).
- Segments are stretches of road between junctions/attached features; a bridge sits ON every segment that carries it, so closing a bridge is modelled as closing those segments.
- Roads are bidirectional unless OSM tags them one-way; reversible/alternating values are treated as bidirectional.
- Travel time uses maxspeed tags where present, otherwise placeholder planning speeds per road class (assumed, not measured).
- No monsoon_risk labels, failure histories, bed counts or trauma levels are inferred. Observed events are attached to places only when a source names the place.
- NEAREST_HOSPITAL is not populated in Phase 1; it needs the routing of Phase 2 and is never authoritative.

## Validation: 0 error(s), 0 warning(s)

