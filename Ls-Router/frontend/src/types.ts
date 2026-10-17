export interface Provenance {
  source_url: string | null;
  source_id: string | null;
  retrieved_at: string | null;
  last_verified: string | null;
  confidence: string | null;
  data_status: string | null;
}

export interface PlaceNode {
  id: string;
  name: string | null;
  type: "village" | "town" | "junction" | string;
  lat: number | null;
  lon: number | null;
  population: number | null;
  reference_key: string | null;
  provenance: Provenance;
}

export interface HospitalNode {
  id: string;
  name: string | null;
  lat: number | null;
  lon: number | null;
  beds: number | null;
  trauma_level: string | null;
  access_place_id: string | null;
  snap_distance_m: number | null;
  provenance: Provenance;
}

export interface BridgeFailureRecord {
  event_id: string;
  year: number | null;
  event_data_status: string | null;
  history_label?: string;
}

export interface BridgeNode {
  id: string;
  name: string | null;
  road_name: string | null;
  lat: number | null;
  lon: number | null;
  monsoon_risk: string | null;
  segment_ids: string[];
  failure_history: BridgeFailureRecord[];
  provenance: Provenance;
}

export interface RoadSegmentEdge {
  id: string;
  name: string | null;
  ref: string | null;
  highway: string | null;
  surface: string | null;
  length_km: number | null;
  travel_time_min: number | null;
  travel_time_basis: string | null;
  is_bidirectional: boolean;
  on_nh29: boolean;
  monsoon_risk: string | null;
  corridor_id: string | null;
  from_place_id: string;
  to_place_id: string;
  status: "open" | "blocked";
  blocked_by: string[];
  geometry: [number, number][] | null;
  provenance: Provenance;
}

export interface GraphConnection {
  from_place_id: string;
  to_place_id: string;
  segment_id: string;
  direction: string;
  travel_time_min: number | null;
  status: "open" | "blocked";
  blocked_by: string[];
}

export interface ClosureEventNode {
  id: string;
  description: string | null;
  status: string;
  data_status: string;
  history_label?: string;
  remembered?: boolean;
  cause: string | null;
  origin: string | null;
  confidence: string | null;
  started_at: string | null;
  ended_at: string | null;
  remembered_at: string | null;
  location_text: string | null;
  source_url: string | null;
  blocks_segments: string[];
  blocks_bridges: string[];
  near_places?: string[];
}

export interface SeasonNode {
  id: string;
  name: string | null;
  start_month: number;
  end_month: number;
  segment_ids: string[];
}

export interface GraphResponse {
  dataset: {
    id: string | null;
    kind: string | null;
    retrieved_at: string | null;
    attribution: string | null;
    network_fingerprint: string | null;
  } | null;
  corridors: string[];
  counts: {
    places: number;
    hospitals: number;
    bridges: number;
    segments: number;
    connections: number;
    closures: number;
    active_closures: number;
    blocked_segments: number;
  };
  provenance_summary: {
    entities: number;
    verified: number;
    unverified: number;
    note: string;
  };
  places: PlaceNode[];
  hospitals: HospitalNode[];
  bridges: BridgeNode[];
  segments: RoadSegmentEdge[];
  connections: GraphConnection[];
  closures: ClosureEventNode[];
  seasons: SeasonNode[];
  warnings: string[];
  cypher: string[];
  params: Record<string, unknown>;
  disclaimer: string;
}

export interface CandidateSegment {
  rank: number;
  segment_id: string;
  name: string | null;
  ref: string | null;
  on_nh29: boolean;
  from_place_id: string;
  from_place_name: string | null;
  to_place_id: string;
  to_place_name: string | null;
  length_km: number | null;
  travel_time_min: number | null;
  monsoon_risk: string | null;
  carries_bridges: string[];
  confidence: string | null;
  data_status: string | null;
  source_url: string | null;
  match_reasons: string[];
  distance_m?: number;
  cumulative_km_from_dimapur?: [number, number];
}

export interface CandidateBridge {
  rank: number;
  bridge_id: string;
  name: string | null;
  road_name: string | null;
  segment_ids: string[];
  monsoon_risk: string | null;
  confidence: string | null;
  source_url: string | null;
  match_reasons: string[];
  distance_m?: number;
}

export interface ResolutionResult {
  status: "resolved" | "ambiguous" | "no_match";
  requires_clarification: boolean;
  clarification_prompt: string | null;
  parsed_event: {
    raw_text: string;
    cause: string;
    intent: string;
    road_refs: string[];
    km_marker: number | null;
    coordinates: { lat: number; lon: number } | null;
    bridge_mentioned: boolean;
    between_pair: string[] | null;
    month: number | null;
    explicit_event_ids: string[];
  };
  selected_target: {
    segment_id: string;
    bridge_id: string | null;
    segment_name: string | null;
    ref: string | null;
    from_place_id: string;
    to_place_id: string;
    confidence: string | null;
    source_url: string | null;
    match_reasons: string[];
  } | null;
  matched_places: Array<{
    id: string;
    name: string | null;
    type: string;
    lat: number | null;
    lon: number | null;
    confidence: string | null;
    source_url: string | null;
  }>;
  candidate_segments: CandidateSegment[];
  candidate_bridges: CandidateBridge[];
  related_observed_events: Array<{
    id: string;
    started_at: string | null;
    cause: string | null;
    description: string | null;
    blocks_segments: string[];
    near_places: string[];
    source_url: string | null;
  }>;
}

export interface ClosureResponse {
  status: "applied" | "clarification_required" | "no_match";
  requires_clarification: boolean;
  created: boolean;
  event_id: string | null;
  event: ClosureEventNode | null;
  target_segment: {
    id: string;
    name: string | null;
    ref: string | null;
    from_place_id: string;
    to_place_id: string;
    confidence: string | null;
    source_url: string | null;
  } | null;
  location_confidence: string | null;
  state_change: {
    blocked_segments_before: string[];
    blocked_segments_after: string[];
    newly_blocked_segments: string[];
    already_blocked_by_other_events: Record<string, string[]>;
    connections_now_disabled: Array<{
      from: string;
      to: string;
      direction: string;
      segment_id: string;
    }>;
  } | null;
  resolution: ResolutionResult;
  closure: Record<string, unknown> | null;
  remembered: Record<string, unknown> | null;
  explanation: string;
  cypher: string[];
  params: Record<string, unknown>;
  graph: { nodes: Record<string, unknown>[]; edges: Record<string, unknown>[] };
  warnings: string[];
  algorithm: Record<string, unknown>;
  disclaimer: string;
}

export interface CutOffPlaceItem {
  id: string;
  name: string | null;
  type: string;
  attributable_to_this_event: boolean;
  evidence: {
    place_id: string;
    path_basis: string;
    baseline_path_nodes: string[];
    baseline_path_segments: string[];
    baseline_travel_time_min: number | null;
    blocked_segments_on_path: string[];
  };
}

export interface HospitalAccessRow {
  hospital_id: string;
  hospital_name: string | null;
  status: "reachable" | "newly_unreachable" | "unreachable_in_baseline_too";
  travel_time_min: number | null;
  baseline_travel_time_min: number | null;
  over_threshold: boolean;
  route_segments: string[] | null;
}

export interface CutoffResponse {
  event_id: string;
  closure_event: ClosureEventNode;
  blocked_segments: Record<string, string[]>;
  reachability: {
    kind: string;
    origin: { id: string; name: string | null };
    baseline_reachable_places: number;
    reachable_with_active_closures: number;
  };
  newly_unreachable: {
    count: number;
    villages: CutOffPlaceItem[];
    towns: CutOffPlaceItem[];
    junctions: CutOffPlaceItem[];
    other: CutOffPlaceItem[];
  };
  attributable_to_this_event: string[];
  also_cut_by_other_closures: string[];
  already_unreachable_in_baseline: Array<{ id: string; name: string | null; type: string }>;
  hospitals_newly_unreachable_from_dimapur: Array<{
    id: string;
    name: string | null;
    access_place_id: string | null;
  }>;
  cut_boundary: Array<{
    segment_id: string;
    from: string;
    to: string;
    direction: string;
    blocked_by: string[];
  }>;
  from_kohima: {
    place_id: string;
    baseline_reachable: number;
    reachable_with_closures: number;
    newly_unreachable: string[];
  } | null;
  component_analysis: {
    representation: string;
    baseline_component_count: number;
    component_count_with_closures: number;
    new_components: string[][];
    new_components_note: string;
    split_off_from_origin_component: string[][];
  };
  hospital_reachability: {
    closure_event: ClosureEventNode;
    affected_settlements: string[];
    threshold_min: number | null;
    per_settlement: Array<{
      village_id: string;
      name: string | null;
      type: string;
      hospitals: HospitalAccessRow[];
    }>;
    per_hospital: Array<{
      hospital_id: string;
      name: string | null;
      beds: number | null;
      trauma_level: string | null;
      access_place_id: string | null;
      confidence: string | null;
      source_url: string | null;
      reachable_from: string[];
      newly_unreachable_from: string[];
      unreachable_in_baseline_too_from: string[];
      over_threshold_from: string[];
    }>;
    hospitals_with_no_usable_route_from_any_affected_settlement: string[];
    hospitals_without_road_attachment: string[];
    note: string;
  };
  explanation: string;
  cypher: string[];
  params: Record<string, unknown>;
  warnings: string[];
  algorithm: Record<string, unknown>;
  disclaimer: string;
}

export interface RouteStep {
  from: string;
  to: string;
  segment_id: string;
  direction: string;
  travel_time_min: number;
  segment_name: string | null;
  ref: string | null;
  length_km: number | null;
  travel_time_basis: string | null;
  confidence: string | null;
  source_url: string | null;
  data_status: string | null;
  bridges_on_segment: string[];
}

export interface RoutePayload {
  nodes: string[];
  node_names: string[];
  segments: RouteStep[];
  total_travel_time_min: number;
  total_length_km: number | null;
  travel_time_is_estimate: boolean;
}

export interface PathContribution {
  segment_id: string;
  name: string | null;
  from: string;
  to: string;
  direction: string;
  travel_time_min: number;
  length_km: number | null;
  share_of_total: number | null;
  travel_time_basis: string | null;
  confidence: string | null;
  source_url: string | null;
  data_status: string | null;
  bridges_on_segment: string[];
}

export interface RerouteResponse {
  from_place_id: string;
  to_place_id: string;
  avoid_event_id: string | null;
  blocked_segments: Record<string, string[]>;
  status: "route_found" | "no_route";
  reason: string | null;
  route: RoutePayload | null;
  baseline_route: RoutePayload | null;
  additional_travel_time_min: number | null;
  objective: string;
  path_explanation: {
    status: "route_found" | "no_route";
    selected_path: { nodes: string[]; node_names: string[] } | null;
    contributions?: PathContribution[];
    total_travel_time_min?: number;
    total_length_km?: number | null;
    baseline_total_travel_time_min?: number | null;
    exclusions: {
      blocked_segments: Record<string, string[]>;
      excluded_invalid_weight: string[];
      dangling_connections?: number;
    };
    checks?: {
      segment_times_sum_to_total: boolean;
      no_selected_segment_is_blocked: boolean;
    };
    selection_rule?: string;
    reason?: string;
  };
  explanation: string;
  cypher: string[];
  params: Record<string, unknown>;
  warnings: string[];
  algorithm: Record<string, unknown>;
  disclaimer: string;
}

export interface WeakLinkSegment {
  rank: number;
  segment_id: string;
  name: string | null;
  ref: string | null;
  on_nh29: boolean;
  betweenness: number;
  ordered_pairs_whose_chosen_shortest_path_uses_it: number;
  example_paths: Array<{ from: string; to: string; nodes: string[] }>;
  carries_bridges: string[];
  confidence: string | null;
  source_url: string | null;
  data_status: string | null;
  if_closed?: {
    places_cut_off_from_dimapur: string[];
    settlements_cut_off: string[];
    undirected_components_after: number;
    undirected_components_before: number;
  };
}

export interface WeakLinkBridge {
  rank: number;
  bridge_id: string;
  name: string | null;
  road_name: string | null;
  score_inherited_from_segments: string[];
  betweenness: number;
  confidence: string | null;
  source_url: string | null;
}

export interface WeakLinkPlace {
  rank: number;
  place_id: string;
  name: string | null;
  type: string;
  betweenness: number;
  is_articulation_point_undirected: boolean;
}

export interface WeakLinksResponse {
  corridor_id: string;
  top_k: number;
  representation: string;
  segments: WeakLinkSegment[];
  bridges: WeakLinkBridge[];
  places: WeakLinkPlace[];
  graph_size: { places: number; directed_edges: number };
  explanation: string;
  cypher: string[];
  params: Record<string, unknown>;
  warnings: string[];
  algorithm: Record<string, unknown>;
  disclaimer: string;
}

export interface SeasonalEventRecord {
  id: string;
  started_at: string | null;
  cause: string | null;
  status: string;
  confidence: string | null;
  source_url: string | null;
  data_status: string;
  remembered?: boolean;
  remembered_at?: string | null;
  history_label?: string;
}

export interface SeasonalRankedItem {
  rank: number;
  kind: "segment" | "bridge";
  id: string;
  name: string | null;
  monsoon_risk: string | null;
  observed_in_season: SeasonalEventRecord[];
  simulated_in_season: SeasonalEventRecord[];
  bridge_failures_in_season?: Array<{
    bridge_id: string;
    event_id: string;
    year: number | null;
    data_status: string | null;
    history_label: string;
  }>;
  events_outside_season: string[];
  undated_events: string[];
  counts: { observed: number; simulated: number };
  scoring_breakdown?: {
    risk_tier: number;
    monsoon_risk_label: string;
    observed_in_season: number;
    simulated_in_season: number;
    remembered_in_season: number;
    bridge_failures_in_season: number;
    composite_score: number;
  };
  advisory?: string;
  evidence_summary?: string;
  carries_bridges?: string[];
  segments?: string[];
  confidence: string | null;
  source_url: string | null;
}

export interface SeasonalRiskResponse {
  month: number;
  seasons: Array<{ id: string; name: string | null; start_month: number; end_month: number }>;
  ranking_method: string | null;
  ranked: SeasonalRankedItem[];
  applicable_segments?: number;
  applicable_bridges?: number;
  unranked_applicable_items?: number;
  observed_events_without_links?: Array<{ id: string; started_at: string | null; near_places: string[] }>;
  explanation: string;
  cypher: string[];
  params: Record<string, unknown>;
  warnings: string[];
  algorithm: Record<string, unknown>;
  disclaimer: string;
}

export interface DraftAlertItem {
  alert_id: string;
  target_place_id: string;
  target_name: string | null;
  channel: string;
  status: string;
  sent: boolean;
  text: string;
  length: number;
  routed_via_segments: string[];
  supporting: {
    closure_event_id: string;
    blocked_segments: Record<string, string[]>;
    cut_off_evidence: Record<string, unknown>;
    cut_boundary: unknown[];
    hospital_route: {
      hospital_id: string;
      route_nodes: string[];
      route_segments: string[];
      travel_time_min: number;
    } | null;
    derived_from: string;
  };
}

export interface DraftAlertsResponse {
  event_id: string;
  closure_event: ClosureEventNode;
  alerts: DraftAlertItem[];
  settlements_considered: string[];
  explanation: string;
  cypher: string[];
  params: Record<string, unknown>;
  warnings: string[];
  algorithm: Record<string, unknown>;
  disclaimer: string;
}

export interface ResolveClosureResponse {
  event_id: string;
  event: ClosureEventNode;
  resolved: boolean | null;
  returned_to_baseline: boolean | null;
  restored_segments: string[];
  still_blocked_by_other_closures: Record<string, string[]>;
  history_preserved: {
    event_node_kept: boolean;
    blocks_links_kept: number;
    failure_records_kept: number;
  };
  clearance: Record<string, unknown>;
  audit: {
    event: ClosureEventNode;
    resolved: boolean | null;
    returned_to_baseline: boolean | null;
    returned_to_baseline_meaning: string;
    target_segments: string[];
    other_active_closures: string[];
    checks: Array<{ name: string; ok: boolean; detail: string }>;
    discrepancies: Array<{ code: string; message: string }>;
    places_unreachable_from_dimapur_due_to_active_closures: string[];
    blocked_segments_now: Record<string, string[]>;
  };
  remembered: Record<string, unknown> | null;
  explanation: string;
  cypher: string[];
  params: Record<string, unknown>;
  warnings: string[];
  algorithm: Record<string, unknown>;
  disclaimer: string;
}

export type SelectedFeature =
  | { kind: "place"; id: string }
  | { kind: "segment"; id: string }
  | { kind: "hospital"; id: string }
  | { kind: "bridge"; id: string }
  | { kind: "closure"; id: string };

export interface ChatLogEntry {
  id: string;
  timestamp: string;
  role: "user" | "system" | "error";
  title: string;
  detail: string;
  clarificationCandidates?: {
    originalDescription: string;
    lat?: number | null;
    lon?: number | null;
    prompt: string;
    segments: CandidateSegment[];
    bridges: CandidateBridge[];
  };
  eventId?: string;
}

