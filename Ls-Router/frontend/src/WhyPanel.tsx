import { useMemo, useState } from "react";
import type {
  CutoffResponse,
  DraftAlertsResponse,
  GraphResponse,
  Provenance,
  RerouteResponse,
  ResolveClosureResponse,
  SelectedFeature,
  SeasonalRiskResponse,
  TimelineEntry,
  WeakLinksResponse,
} from "./types";

interface WhyPanelProps {
  graph: GraphResponse | null;
  timeline: TimelineEntry[];
  replayedEventId: string | null;
  onReplayEvent: (eventId: string) => void;
  onExitReplay: () => void;
  onRememberEvent: (eventId: string) => void;
  onExportAlerts: (eventId: string, format: "json" | "text") => void;
  exportedTextPreview: string | null;
  selectedFeature: SelectedFeature | null;
  onSelectFeature: (feature: SelectedFeature) => void;
  cutoff: CutoffResponse | null;
  reroute: RerouteResponse | null;
  weakLinks: WeakLinksResponse | null;
  seasonalRisk: SeasonalRiskResponse | null;
  alerts: DraftAlertsResponse | null;
  resolveAudit: ResolveClosureResponse | null;
  activeCypher: string[];
  activeAlgorithm: Record<string, unknown> | null;
  activeWarnings: string[];
  activeExplanation: string | null;
  showSourceSummary: boolean;
}

function ConfidenceBadge({ value }: { value: string | null | undefined }) {
  const v = value || "unverified";
  const cls = v === "verified" ? "badge verified" : "badge unverified";
  return (
    <span className={cls} title={`Source confidence: ${v}`}>
      {v}
    </span>
  );
}

function StatusBadge({ value }: { value: string | null | undefined }) {
  const v = value || "unknown";
  let cls = "badge neutral";
  if (v === "blocked" || v === "cut_off" || v === "newly_unreachable" || v === "high") {
    cls = "badge danger";
  } else if (
    v === "open" ||
    v === "reachable" ||
    v === "resolved" ||
    v === "route_found" ||
    v === "observed"
  ) {
    cls = "badge ok";
  } else if (
    v === "simulated" ||
    v === "draft" ||
    v === "medium" ||
    v === "assumed" ||
    v === "derived"
  ) {
    cls = "badge warn";
  }
  return <span className={cls}>{v}</span>;
}

function ProvenanceTable({ prov }: { prov: Provenance }) {
  return (
    <dl className="kv-list">
      <div>
        <dt>Confidence</dt>
        <dd>
          <ConfidenceBadge value={prov.confidence} />
        </dd>
      </div>
      <div>
        <dt>Data Status</dt>
        <dd>
          <StatusBadge value={prov.data_status} />
        </dd>
      </div>
      <div>
        <dt>Source ID</dt>
        <dd>
          <code>{prov.source_id ?? "unknown"}</code>
        </dd>
      </div>
      <div>
        <dt>Retrieved At</dt>
        <dd>{prov.retrieved_at ?? "unknown"}</dd>
      </div>
      <div>
        <dt>Last Verified</dt>
        <dd>{prov.last_verified ?? "Not manually verified (null)"}</dd>
      </div>
      <div>
        <dt>Source URL</dt>
        <dd>
          {prov.source_url ? (
            <a href={prov.source_url} target="_blank" rel="noopener noreferrer">
              {prov.source_url}
            </a>
          ) : (
            "unknown"
          )}
        </dd>
      </div>
    </dl>
  );
}

type SourceFilterKind =
  | "all"
  | "verified"
  | "unverified"
  | "place"
  | "segment"
  | "hospital"
  | "bridge"
  | "closure";

export default function WhyPanel({
  graph,
  timeline,
  replayedEventId,
  onReplayEvent,
  onExitReplay,
  onRememberEvent,
  onExportAlerts,
  exportedTextPreview,
  selectedFeature,
  onSelectFeature,
  cutoff,
  reroute,
  weakLinks,
  seasonalRisk,
  alerts,
  resolveAudit,
  activeCypher,
  activeAlgorithm,
  activeWarnings,
  activeExplanation,
  showSourceSummary,
}: WhyPanelProps) {
  const [sourceFilter, setSourceFilter] = useState<SourceFilterKind>("all");
  const [sourceQuery, setSourceQuery] = useState<string>("");
  const [copiedNotice, setCopiedNotice] = useState<string | null>(null);

  const placeName = (id: string) => {
    const p = graph?.places.find((x) => x.id === id);
    return p?.name ? `${p.name} (${p.id})` : id;
  };

  // Unified data-source inspection rows across all graph entity types
  const allSourceRows = useMemo(() => {
    if (!graph) return [];
    const rows: Array<{
      kind: SelectedFeature["kind"];
      typeLabel: string;
      id: string;
      name: string;
      confidence: string;
      dataStatus: string;
      lastVerified: string | null;
      retrievedAt: string | null;
      sourceId: string | null;
      sourceUrl: string | null;
    }> = [];

    for (const p of graph.places) {
      rows.push({
        kind: "place",
        typeLabel: `Place (${p.type})`,
        id: p.id,
        name: p.name || p.id,
        confidence: p.provenance.confidence || "unverified",
        dataStatus: p.provenance.data_status || "unknown",
        lastVerified: p.provenance.last_verified,
        retrievedAt: p.provenance.retrieved_at,
        sourceId: p.provenance.source_id,
        sourceUrl: p.provenance.source_url,
      });
    }
    for (const s of graph.segments) {
      rows.push({
        kind: "segment",
        typeLabel: "RoadSegment",
        id: s.id,
        name: s.name || s.ref || `${s.from_place_id}→${s.to_place_id}`,
        confidence: s.provenance.confidence || "unverified",
        dataStatus: s.provenance.data_status || "unknown",
        lastVerified: s.provenance.last_verified,
        retrievedAt: s.provenance.retrieved_at,
        sourceId: s.provenance.source_id,
        sourceUrl: s.provenance.source_url,
      });
    }
    for (const h of graph.hospitals) {
      rows.push({
        kind: "hospital",
        typeLabel: "Hospital",
        id: h.id,
        name: h.name || h.id,
        confidence: h.provenance.confidence || "unverified",
        dataStatus: h.provenance.data_status || "unknown",
        lastVerified: h.provenance.last_verified,
        retrievedAt: h.provenance.retrieved_at,
        sourceId: h.provenance.source_id,
        sourceUrl: h.provenance.source_url,
      });
    }
    for (const b of graph.bridges) {
      rows.push({
        kind: "bridge",
        typeLabel: "Bridge",
        id: b.id,
        name: b.name || b.id,
        confidence: b.provenance.confidence || "unverified",
        dataStatus: b.provenance.data_status || "unknown",
        lastVerified: b.provenance.last_verified,
        retrievedAt: b.provenance.retrieved_at,
        sourceId: b.provenance.source_id,
        sourceUrl: b.provenance.source_url,
      });
    }
    for (const c of graph.closures) {
      rows.push({
        kind: "closure",
        typeLabel: "ClosureEvent",
        id: c.id,
        name: c.description || c.id,
        confidence: c.confidence || "unverified",
        dataStatus: c.data_status || "simulated",
        lastVerified: null,
        retrievedAt: c.started_at,
        sourceId: c.origin,
        sourceUrl: c.source_url,
      });
    }
    return rows;
  }, [graph]);

  const filteredSourceRows = useMemo(() => {
    const q = sourceQuery.trim().toLowerCase();
    return allSourceRows.filter((r) => {
      if (sourceFilter === "verified" && r.confidence !== "verified") return false;
      if (sourceFilter === "unverified" && r.confidence === "verified") return false;
      if (
        sourceFilter !== "all" &&
        sourceFilter !== "verified" &&
        sourceFilter !== "unverified" &&
        r.kind !== sourceFilter
      ) {
        return false;
      }
      if (!q) return true;
      return (
        r.id.toLowerCase().includes(q) ||
        r.name.toLowerCase().includes(q) ||
        r.typeLabel.toLowerCase().includes(q) ||
        (r.sourceId || "").toLowerCase().includes(q)
      );
    });
  }, [allSourceRows, sourceFilter, sourceQuery]);

  // Resolve selected feature details from the loaded graph + active calculations
  const renderSelectedFeature = () => {
    if (!graph || !selectedFeature) {
      return (
        <p className="muted-note">
          Click any road segment, village, town, junction, hospital, bridge, or closure on the map
          (or in the tables below) to inspect its coordinates, provenance, and graph calculations.
        </p>
      );
    }

    if (selectedFeature.kind === "place") {
      const p = graph.places.find((x) => x.id === selectedFeature.id);
      if (!p) return <p className="muted-note">Place {selectedFeature.id} not found in graph.</p>;

      const cutItem = [
        ...(cutoff?.newly_unreachable.villages ?? []),
        ...(cutoff?.newly_unreachable.towns ?? []),
        ...(cutoff?.newly_unreachable.junctions ?? []),
        ...(cutoff?.newly_unreachable.other ?? []),
      ].find((x) => x.id === p.id);
      const baseUnreach = cutoff?.already_unreachable_in_baseline.some((x) => x.id === p.id);
      const weakEntry = weakLinks?.places.find((x) => x.place_id === p.id);

      return (
        <div className="feature-detail">
          <div className="feature-header">
            <span className="feature-type">Place ({p.type})</span>
            <strong>{p.name || p.id}</strong>
            <code>{p.id}</code>
            <ConfidenceBadge value={p.provenance.confidence} />
            <StatusBadge value={p.provenance.data_status} />
          </div>
          <dl className="kv-list">
            <div>
              <dt>Coordinates</dt>
              <dd>
                {p.lat != null && p.lon != null
                  ? `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`
                  : "unknown"}
              </dd>
            </div>
            <div>
              <dt>Population</dt>
              <dd>{p.population != null ? p.population.toLocaleString() : "unknown (null)"}</dd>
            </div>
            {p.reference_key && (
              <div>
                <dt>Reference Node</dt>
                <dd>
                  <code>{p.reference_key}</code>
                </dd>
              </div>
            )}
            {cutoff && (
              <div>
                <dt>Reachability from Dimapur</dt>
                <dd>
                  {cutItem ? (
                    <StatusBadge value="cut_off" />
                  ) : baseUnreach ? (
                    <StatusBadge value="baseline_unreachable" />
                  ) : (
                    <StatusBadge value="reachable" />
                  )}
                </dd>
              </div>
            )}
            {weakEntry && (
              <div>
                <dt>Centrality Rank</dt>
                <dd>
                  #{weakEntry.rank} (betweenness: {weakEntry.betweenness.toFixed(4)}
                  {weakEntry.is_articulation_point_undirected ? ", articulation point" : ""})
                </dd>
              </div>
            )}
          </dl>
          {cutItem && (
            <div className="subcard danger-subcard">
              <strong>Cut-Off Evidence Path from Dimapur:</strong>
              <div>
                Attributable to closure <code>{cutoff?.event_id}</code>:{" "}
                <strong>{cutItem.attributable_to_this_event ? "Yes" : "No"}</strong>
              </div>
              <div>
                Baseline path nodes: <code>{cutItem.evidence.baseline_path_nodes.join(" → ")}</code>
              </div>
              <div>
                Baseline path segments:{" "}
                <code>{cutItem.evidence.baseline_path_segments.join(", ")}</code>
              </div>
              <div>
                Blocked segment(s) on path:{" "}
                <code>{cutItem.evidence.blocked_segments_on_path.join(", ")}</code>
              </div>
            </div>
          )}
          <h4>Provenance</h4>
          <ProvenanceTable prov={p.provenance} />
        </div>
      );
    }

    if (selectedFeature.kind === "segment") {
      const s = graph.segments.find((x) => x.id === selectedFeature.id);
      if (!s) return <p className="muted-note">Segment {selectedFeature.id} not found.</p>;

      const routeContrib = reroute?.path_explanation?.contributions?.find(
        (c) => c.segment_id === s.id,
      );
      const weakSeg = weakLinks?.segments.find((w) => w.segment_id === s.id);
      const seasonalSeg = seasonalRisk?.ranked.find(
        (r) => r.kind === "segment" && r.id === s.id,
      );

      return (
        <div className="feature-detail">
          <div className="feature-header">
            <span className="feature-type">RoadSegment</span>
            <strong>{s.name || s.ref || s.id}</strong>
            <code>{s.id}</code>
            <ConfidenceBadge value={s.provenance.confidence} />
            <StatusBadge value={s.provenance.data_status} />
          </div>
          <dl className="kv-list">
            <div>
              <dt>Status</dt>
              <dd>
                <StatusBadge value={s.status} />
                {s.blocked_by.length > 0 && (
                  <span>
                    {" "}
                    by <code>{s.blocked_by.join(", ")}</code>
                  </span>
                )}
              </dd>
            </div>
            <div>
              <dt>Endpoints</dt>
              <dd>
                <button
                  type="button"
                  className="inline-link"
                  onClick={() => onSelectFeature({ kind: "place", id: s.from_place_id })}
                >
                  {placeName(s.from_place_id)}
                </button>
                {" → "}
                <button
                  type="button"
                  className="inline-link"
                  onClick={() => onSelectFeature({ kind: "place", id: s.to_place_id })}
                >
                  {placeName(s.to_place_id)}
                </button>{" "}
                ({s.is_bidirectional ? "bidirectional" : "one-way"})
              </dd>
            </div>
            <div>
              <dt>Highway / Ref</dt>
              <dd>
                {s.highway ?? "unknown"} / {s.ref ?? "none"} ({s.on_nh29 ? "NH29 trunk" : "non-NH29"})
              </dd>
            </div>
            <div>
              <dt>Length &amp; Travel Time</dt>
              <dd>
                {s.length_km != null ? `${s.length_km} km` : "unknown km"} /{" "}
                {s.travel_time_min != null ? `${s.travel_time_min} min` : "unknown min"} (basis:{" "}
                <code>{s.travel_time_basis ?? "unknown"}</code>)
              </dd>
            </div>
            <div>
              <dt>Surface &amp; Monsoon Risk</dt>
              <dd>
                surface: <code>{s.surface ?? "unknown (null)"}</code> | risk:{" "}
                <StatusBadge value={s.monsoon_risk ?? "unlabelled"} />
              </dd>
            </div>
            <div>
              <dt>Stored Road Geometry</dt>
              <dd>
                {s.geometry
                  ? `${s.geometry.length} coordinate vertices`
                  : "None (null — rendered schematically)"}
              </dd>
            </div>
          </dl>
          {routeContrib && (
            <div className="subcard info-subcard">
              <strong>Active Route Contribution:</strong> {routeContrib.travel_time_min} min (
              {routeContrib.share_of_total != null
                ? `${(routeContrib.share_of_total * 100).toFixed(1)}% of route`
                : "n/a"}
              ), direction: <code>{routeContrib.direction}</code>
            </div>
          )}
          {weakSeg && (
            <div className="subcard warn-subcard">
              <strong>Weak-Link Rank #{weakSeg.rank}:</strong> betweenness{" "}
              <code>{weakSeg.betweenness.toFixed(4)}</code>, used by{" "}
              <strong>{weakSeg.ordered_pairs_whose_chosen_shortest_path_uses_it}</strong> shortest
              paths.
              {weakSeg.if_closed && (
                <div>
                  If closed: cuts off{" "}
                  <strong>{weakSeg.if_closed.settlements_cut_off.length}</strong> settlement(s) from
                  Dimapur.
                </div>
              )}
            </div>
          )}
          {seasonalSeg && (
            <div className="subcard warn-subcard">
              <strong>Seasonal Risk Rank #{seasonalSeg.rank}:</strong> risk label{" "}
              <code>{seasonalSeg.monsoon_risk ?? "unlabelled"}</code>, observed in-season events:{" "}
              <strong>{seasonalSeg.counts.observed}</strong>, simulated in-season events:{" "}
              <strong>{seasonalSeg.counts.simulated}</strong>
              {seasonalSeg.scoring_breakdown && (
                <span>
                  {" "}
                  (composite score: <code>{seasonalSeg.scoring_breakdown.composite_score}</code>)
                </span>
              )}
              .
              {seasonalSeg.advisory && <div>{seasonalSeg.advisory}</div>}
              {seasonalSeg.evidence_summary && (
                <div className="muted-note">{seasonalSeg.evidence_summary}</div>
              )}
            </div>
          )}
          <h4>Provenance</h4>
          <ProvenanceTable prov={s.provenance} />
        </div>
      );
    }

    if (selectedFeature.kind === "hospital") {
      const h = graph.hospitals.find((x) => x.id === selectedFeature.id);
      if (!h) return <p className="muted-note">Hospital {selectedFeature.id} not found.</p>;
      const hospSummary = cutoff?.hospital_reachability?.per_hospital.find(
        (x) => x.hospital_id === h.id,
      );

      return (
        <div className="feature-detail">
          <div className="feature-header">
            <span className="feature-type">Hospital</span>
            <strong>{h.name || h.id}</strong>
            <code>{h.id}</code>
            <ConfidenceBadge value={h.provenance.confidence} />
            <StatusBadge value={h.provenance.data_status} />
          </div>
          <dl className="kv-list">
            <div>
              <dt>Coordinates</dt>
              <dd>
                {h.lat != null && h.lon != null
                  ? `${h.lat.toFixed(5)}, ${h.lon.toFixed(5)}`
                  : "unknown"}
              </dd>
            </div>
            <div>
              <dt>Beds &amp; Trauma Level</dt>
              <dd>
                beds: <code>{h.beds != null ? h.beds : "unknown (null)"}</code> | trauma:{" "}
                <code>{h.trauma_level ?? "unknown (null)"}</code>
              </dd>
            </div>
            <div>
              <dt>Road Network Attachment</dt>
              <dd>
                {h.access_place_id ? (
                  <>
                    Attached to{" "}
                    <button
                      type="button"
                      className="inline-link"
                      onClick={() =>
                        onSelectFeature({ kind: "place", id: h.access_place_id as string })
                      }
                    >
                      {placeName(h.access_place_id)}
                    </button>{" "}
                    ({h.snap_distance_m != null ? `${h.snap_distance_m} m snap` : "n/a"})
                  </>
                ) : (
                  <StatusBadge value="unattached (no road access node)" />
                )}
              </dd>
            </div>
          </dl>
          {hospSummary && (
            <div className="subcard info-subcard">
              <strong>Reachability from Affected Settlements:</strong>
              <div>
                Reachable from: <code>{hospSummary.reachable_from.join(", ") || "none"}</code>
              </div>
              <div>
                Newly unreachable from:{" "}
                <code>{hospSummary.newly_unreachable_from.join(", ") || "none"}</code>
              </div>
            </div>
          )}
          <h4>Provenance</h4>
          <ProvenanceTable prov={h.provenance} />
        </div>
      );
    }

    if (selectedFeature.kind === "bridge") {
      const b = graph.bridges.find((x) => x.id === selectedFeature.id);
      if (!b) return <p className="muted-note">Bridge {selectedFeature.id} not found.</p>;
      const weakBr = weakLinks?.bridges.find((w) => w.bridge_id === b.id);
      const seasonalBr = seasonalRisk?.ranked.find(
        (r) => r.kind === "bridge" && r.id === b.id,
      );

      return (
        <div className="feature-detail">
          <div className="feature-header">
            <span className="feature-type">Bridge</span>
            <strong>{b.name || b.id}</strong>
            <code>{b.id}</code>
            <ConfidenceBadge value={b.provenance.confidence} />
            <StatusBadge value={b.provenance.data_status} />
          </div>
          <dl className="kv-list">
            <div>
              <dt>Coordinates</dt>
              <dd>
                {b.lat != null && b.lon != null
                  ? `${b.lat.toFixed(5)}, ${b.lon.toFixed(5)}`
                  : "unknown"}
              </dd>
            </div>
            <div>
              <dt>Road &amp; Segments</dt>
              <dd>
                {b.road_name ?? "unnamed road"} | ON segment(s):{" "}
                {b.segment_ids.map((sid, i) => (
                  <span key={sid}>
                    {i > 0 && ", "}
                    <button
                      type="button"
                      className="inline-link"
                      onClick={() => onSelectFeature({ kind: "segment", id: sid })}
                    >
                      {sid}
                    </button>
                  </span>
                ))}
              </dd>
            </div>
            <div>
              <dt>Monsoon Risk</dt>
              <dd>
                <StatusBadge value={b.monsoon_risk ?? "unlabelled"} />
              </dd>
            </div>
            <div>
              <dt>Recorded Failure History</dt>
              <dd>
                {b.failure_history.length > 0
                  ? b.failure_history
                      .map(
                        (f) =>
                          `${f.event_id} (${f.year ?? "?"}, ${f.history_label ?? f.event_data_status})`,
                      )
                      .join("; ")
                  : "None recorded"}
              </dd>
            </div>
          </dl>
          {weakBr && (
            <div className="subcard warn-subcard">
              <strong>Weak-Link Rank #{weakBr.rank}:</strong> inherited betweenness{" "}
              <code>{weakBr.betweenness.toFixed(4)}</code> from segment(s){" "}
              <code>{weakBr.score_inherited_from_segments.join(", ")}</code>.
            </div>
          )}
          {seasonalBr && (
            <div className="subcard warn-subcard">
              <strong>Seasonal Risk Rank #{seasonalBr.rank}:</strong> observed in-season:{" "}
              <strong>{seasonalBr.counts.observed}</strong>, simulated in-season:{" "}
              <strong>{seasonalBr.counts.simulated}</strong>.
              {seasonalBr.advisory && <div>{seasonalBr.advisory}</div>}
              {seasonalBr.evidence_summary && (
                <div className="muted-note">{seasonalBr.evidence_summary}</div>
              )}
            </div>
          )}
          <h4>Provenance</h4>
          <ProvenanceTable prov={b.provenance} />
        </div>
      );
    }

    if (selectedFeature.kind === "closure") {
      const c = graph.closures.find((x) => x.id === selectedFeature.id);
      if (!c) return <p className="muted-note">Closure event {selectedFeature.id} not found.</p>;
      return (
        <div className="feature-detail">
          <div className="feature-header">
            <span className="feature-type">ClosureEvent</span>
            <strong>{c.id}</strong>
            <StatusBadge value={c.status} />
            <ConfidenceBadge value={c.confidence} />
            <StatusBadge value={c.history_label ?? c.data_status} />
          </div>
          <dl className="kv-list">
            <div>
              <dt>History Classification</dt>
              <dd>
                <StatusBadge value={c.data_status} />{" "}
                <span>{c.history_label ?? c.data_status}</span>
                {c.remembered_at && (
                  <span>
                    {" "}
                    | remembered at <code>{c.remembered_at}</code>
                  </span>
                )}
              </dd>
            </div>
            <div>
              <dt>Description</dt>
              <dd>{c.description ?? "none"}</dd>
            </div>
            <div>
              <dt>Cause &amp; Origin</dt>
              <dd>
                cause: <code>{c.cause ?? "unknown"}</code> | origin:{" "}
                <code>{c.origin ?? "unknown"}</code>
              </dd>
            </div>
            <div>
              <dt>Blocks Segments</dt>
              <dd>
                {c.blocks_segments.length > 0
                  ? c.blocks_segments.map((sid, i) => (
                      <span key={sid}>
                        {i > 0 && ", "}
                        <button
                          type="button"
                          className="inline-link"
                          onClick={() => onSelectFeature({ kind: "segment", id: sid })}
                        >
                          {sid}
                        </button>
                      </span>
                    ))
                  : "none"}
              </dd>
            </div>
            <div>
              <dt>Blocks Bridges</dt>
              <dd>{c.blocks_bridges.join(", ") || "none"}</dd>
            </div>
            <div>
              <dt>Started / Ended</dt>
              <dd>
                {c.started_at ?? "unknown"} → {c.ended_at ?? "active"}
              </dd>
            </div>
            <div>
              <dt>Source URL</dt>
              <dd>
                {c.source_url ? (
                  <a href={c.source_url} target="_blank" rel="noopener noreferrer">
                    {c.source_url}
                  </a>
                ) : (
                  "synthetic / simulation"
                )}
              </dd>
            </div>
          </dl>
          <div className="btn-row">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => onReplayEvent(c.id)}
            >
              Replay Event Impact
            </button>
            {!c.remembered_at && (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => onRememberEvent(c.id)}
              >
                Remember in History
              </button>
            )}
            <button
              type="button"
              className="btn-secondary"
              onClick={() => onExportAlerts(c.id, "json")}
            >
              Export Drafts (JSON)
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => onExportAlerts(c.id, "text")}
            >
              Export Drafts (Text)
            </button>
          </div>
        </div>
      );
    }

    return null;
  };

  const baseComp = reroute?.path_explanation?.baseline_comparison;

  return (
    <aside className="pane right-pane" aria-label="Why explanation, provenance, and graph details">
      <div className="pane-header">
        <h2>&ldquo;Why?&rdquo; Graph Evidence, Timeline &amp; Provenance</h2>
      </div>

      <div className="pane-scroll scroller">
        {/* Latest Backend Summary */}
        {activeExplanation && (
          <section className="card highlight-card" aria-live="polite">
            <h3>Latest Graph Calculation</h3>
            <p className="explanation-text">{activeExplanation}</p>
          </section>
        )}

        {/* Warnings */}
        {activeWarnings.length > 0 && (
          <section className="card warn-card" role="region" aria-label="Data and model warnings">
            <h3>Uncertainty &amp; Coverage Warnings ({activeWarnings.length})</h3>
            <ul className="compact-list">
              {activeWarnings.map((w, idx) => (
                <li key={idx}>{w}</li>
              ))}
            </ul>
          </section>
        )}

        {/* 1. Interactive Feature Inspector */}
        <section className="card">
          <div className="section-title-row">
            <h3>Selected Feature Inspector</h3>
            {graph && graph.segments.length > 0 && (
              <select
                className="compact-select"
                aria-label="Select a feature to inspect"
                value={selectedFeature ? `${selectedFeature.kind}:${selectedFeature.id}` : ""}
                onChange={(e) => {
                  const val = e.target.value;
                  if (!val) return;
                  const [k, ...rest] = val.split(":");
                  onSelectFeature({
                    kind: k as SelectedFeature["kind"],
                    id: rest.join(":"),
                  });
                }}
              >
                <option value="">Jump to entity…</option>
                <optgroup label="Road Segments">
                  {graph.segments.map((s) => (
                    <option key={s.id} value={`segment:${s.id}`}>
                      {s.id} ({s.name || s.ref || `${s.from_place_id}->${s.to_place_id}`}) [
                      {s.status}]
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Places">
                  {graph.places.map((p) => (
                    <option key={p.id} value={`place:${p.id}`}>
                      {p.name || p.id} ({p.type})
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Hospitals">
                  {graph.hospitals.map((h) => (
                    <option key={h.id} value={`hospital:${h.id}`}>
                      {h.name || h.id}
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Bridges">
                  {graph.bridges.map((b) => (
                    <option key={b.id} value={`bridge:${b.id}`}>
                      {b.name || b.id}
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Closures">
                  {graph.closures.map((c) => (
                    <option key={c.id} value={`closure:${c.id}`}>
                      {c.id} [{c.data_status}]
                    </option>
                  ))}
                </optgroup>
              </select>
            )}
          </div>
          {renderSelectedFeature()}
        </section>

        {/* 2. Closure Reachability & Hospital Impact */}
        {cutoff && (
          <section className="card">
            <div className="section-title-row">
              <h3>
                Closure Impact &amp; Reachability (<code>{cutoff.event_id}</code>)
              </h3>
              {replayedEventId === cutoff.event_id && (
                <span className="badge info">TIMELINE REPLAY</span>
              )}
            </div>
            <dl className="kv-list">
              <div>
                <dt>Origin Reference</dt>
                <dd>
                  {cutoff.reachability.origin.name || cutoff.reachability.origin.id} (
                  <code>{cutoff.reachability.origin.id}</code>)
                </dd>
              </div>
              <div>
                <dt>Reachable Places</dt>
                <dd>
                  <strong>{cutoff.reachability.reachable_with_active_closures}</strong> of{" "}
                  <strong>{cutoff.reachability.baseline_reachable_places}</strong> baseline-reachable
                  places
                </dd>
              </div>
              <div>
                <dt>Newly Unreachable</dt>
                <dd>
                  <strong>{cutoff.newly_unreachable.count}</strong> total (
                  {cutoff.newly_unreachable.villages.length} village(s),{" "}
                  {cutoff.newly_unreachable.towns.length} town(s),{" "}
                  {cutoff.newly_unreachable.junctions.length} junction(s))
                </dd>
              </div>
              {cutoff.from_kohima && (
                <div>
                  <dt>From Kohima</dt>
                  <dd>
                    {cutoff.from_kohima.reachable_with_closures} /{" "}
                    {cutoff.from_kohima.baseline_reachable} reachable (newly unreachable:{" "}
                    {cutoff.from_kohima.newly_unreachable.join(", ") || "none"})
                  </dd>
                </div>
              )}
              <div>
                <dt>Undirected Components</dt>
                <dd>
                  baseline: {cutoff.component_analysis.baseline_component_count} → with closures:{" "}
                  <strong>{cutoff.component_analysis.component_count_with_closures}</strong>
                </dd>
              </div>
            </dl>

            {cutoff.newly_unreachable.count > 0 && (
              <div className="subcard danger-subcard">
                <strong>Newly Cut-Off Places (click to inspect evidence):</strong>
                <div className="chip-row">
                  {[
                    ...cutoff.newly_unreachable.villages,
                    ...cutoff.newly_unreachable.towns,
                    ...cutoff.newly_unreachable.junctions,
                    ...cutoff.newly_unreachable.other,
                  ].map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className="entity-chip danger"
                      onClick={() => onSelectFeature({ kind: "place", id: item.id })}
                    >
                      {item.name || item.id} ({item.type})
                    </button>
                  ))}
                </div>
              </div>
            )}

            {cutoff.already_unreachable_in_baseline.length > 0 && (
              <div className="subcard">
                <strong>Already Unreachable in Baseline (Not Attributed to Closure):</strong>
                <div className="chip-row">
                  {cutoff.already_unreachable_in_baseline.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      className="entity-chip"
                      onClick={() => onSelectFeature({ kind: "place", id: p.id })}
                    >
                      {p.name || p.id} ({p.type})
                    </button>
                  ))}
                </div>
              </div>
            )}

            <h4>Hospital Access from Cut-Off Settlements</h4>
            {cutoff.hospital_reachability.per_settlement.length === 0 ? (
              <p className="muted-note">
                No settlement (village/town) is newly cut off from Dimapur.
              </p>
            ) : (
              <div className="table-wrapper scroller">
                <table
                  className="data-table"
                  style={
                    {
                      "--label-1": '"Settlement"',
                      "--label-2": '"Hospital"',
                      "--label-3": '"Status"',
                      "--label-4": '"Est. Time"',
                    } as React.CSSProperties
                  }
                >
                  <thead>
                    <tr>
                      <th>Settlement</th>
                      <th>Hospital</th>
                      <th>Status</th>
                      <th>Est. Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cutoff.hospital_reachability.per_settlement.flatMap((s) =>
                      s.hospitals.map((h) => (
                        <tr key={`${s.village_id}-${h.hospital_id}`}>
                          <th>
                            <button
                              type="button"
                              className="inline-link"
                              onClick={() =>
                                onSelectFeature({ kind: "place", id: s.village_id })
                              }
                            >
                              {s.name || s.village_id}
                            </button>
                          </th>
                          <td>
                            <button
                              type="button"
                              className="inline-link"
                              onClick={() =>
                                onSelectFeature({ kind: "hospital", id: h.hospital_id })
                              }
                            >
                              {h.hospital_name || h.hospital_id}
                            </button>
                          </td>
                          <td>
                            <StatusBadge value={h.status} />
                          </td>
                          <td>
                            {h.travel_time_min != null
                              ? `${h.travel_time_min} min`
                              : "unreachable"}
                            {h.baseline_travel_time_min != null &&
                              ` (base: ${h.baseline_travel_time_min}m)`}
                          </td>
                        </tr>
                      )),
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        {/* 3. Alternate Route & Baseline Comparison */}
        {reroute && (
          <section className="card">
            <h3>
              Alternative-Route Comparison ({placeName(reroute.from_place_id)} →{" "}
              {placeName(reroute.to_place_id)})
            </h3>

            {/* Side-by-Side Comparison Grid */}
            <div className="comparison-grid">
              <div className="subcard">
                <strong>Baseline Route (Open Network)</strong>
                {reroute.baseline_route ? (
                  <dl className="kv-list">
                    <div>
                      <dt>Travel Time</dt>
                      <dd>
                        <strong>{reroute.baseline_route.total_travel_time_min} min</strong>
                      </dd>
                    </div>
                    <div>
                      <dt>Distance</dt>
                      <dd>
                        {reroute.baseline_route.total_length_km != null
                          ? `${reroute.baseline_route.total_length_km} km`
                          : "unknown"}
                      </dd>
                    </div>
                    <div>
                      <dt>Segments</dt>
                      <dd>
                        <code>
                          {reroute.baseline_route.segments.map((s) => s.segment_id).join(" → ")}
                        </code>
                      </dd>
                    </div>
                    <div>
                      <dt>Nodes</dt>
                      <dd>
                        <code>{reroute.baseline_route.node_names.join(" → ")}</code>
                      </dd>
                    </div>
                  </dl>
                ) : (
                  <p className="muted-note">No baseline route between these places.</p>
                )}
              </div>

              <div
                className={`subcard ${
                  reroute.status === "route_found" ? "info-subcard" : "danger-subcard"
                }`}
              >
                <strong>
                  Active / Detour Route{" "}
                  {reroute.avoid_event_id ? `(avoiding ${reroute.avoid_event_id})` : ""}
                </strong>
                {reroute.route ? (
                  <dl className="kv-list">
                    <div>
                      <dt>Travel Time</dt>
                      <dd>
                        <strong>{reroute.route.total_travel_time_min} min</strong>
                        {reroute.additional_travel_time_min != null && (
                          <span className="detour-delta">
                            {" "}
                            ({reroute.additional_travel_time_min >= 0 ? "+" : ""}
                            {reroute.additional_travel_time_min} min)
                          </span>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Distance</dt>
                      <dd>
                        {reroute.route.total_length_km != null
                          ? `${reroute.route.total_length_km} km`
                          : "unknown"}
                        {baseComp?.detour_length_delta_km != null && (
                          <span>
                            {" "}
                            ({baseComp.detour_length_delta_km >= 0 ? "+" : ""}
                            {baseComp.detour_length_delta_km} km)
                          </span>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Segments</dt>
                      <dd>
                        <code>
                          {reroute.route.segments.map((s) => s.segment_id).join(" → ")}
                        </code>
                      </dd>
                    </div>
                    <div>
                      <dt>Nodes</dt>
                      <dd>
                        <code>{reroute.route.node_names.join(" → ")}</code>
                      </dd>
                    </div>
                  </dl>
                ) : (
                  <p className="muted-note">
                    <StatusBadge value="no_route" /> {reroute.reason || "No directed path exists."}
                  </p>
                )}
              </div>
            </div>

            {baseComp && !baseComp.same_as_baseline && (
              <div className="subcard warn-subcard">
                <div>
                  <strong>Detour Segment Diff vs. Baseline:</strong>
                </div>
                {baseComp.segments_avoided_from_baseline.length > 0 && (
                  <div>
                    Avoided baseline segment(s):{" "}
                    {baseComp.segments_avoided_from_baseline.map((sid) => (
                      <button
                        key={sid}
                        type="button"
                        className="entity-chip danger"
                        onClick={() => onSelectFeature({ kind: "segment", id: sid })}
                      >
                        − {sid}
                      </button>
                    ))}
                  </div>
                )}
                {baseComp.segments_added_in_detour.length > 0 && (
                  <div>
                    Added detour segment(s):{" "}
                    {baseComp.segments_added_in_detour.map((sid) => (
                      <button
                        key={sid}
                        type="button"
                        className="entity-chip"
                        onClick={() => onSelectFeature({ kind: "segment", id: sid })}
                      >
                        + {sid}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {reroute.path_explanation.contributions &&
              reroute.path_explanation.contributions.length > 0 && (
                <>
                  <h4>Selected Route Segment Breakdown (Dijkstra Minimum Travel Time)</h4>
                  <div className="table-wrapper scroller">
                    <table
                      className="data-table"
                      style={
                        {
                          "--label-1": '"Segment"',
                          "--label-2": '"From → To"',
                          "--label-3": '"Time (Share)"',
                          "--label-4": '"Basis / Conf."',
                        } as React.CSSProperties
                      }
                    >
                      <thead>
                        <tr>
                          <th>Segment</th>
                          <th>From → To</th>
                          <th>Time (Share)</th>
                          <th>Basis / Conf.</th>
                        </tr>
                      </thead>
                      <tbody>
                        {reroute.path_explanation.contributions.map((c, idx) => (
                          <tr key={`${c.segment_id}-${idx}`}>
                            <th>
                              <button
                                type="button"
                                className="inline-link"
                                onClick={() =>
                                  onSelectFeature({ kind: "segment", id: c.segment_id })
                                }
                              >
                                {c.segment_id}
                              </button>
                            </th>
                            <td>
                              {c.from} → {c.to}
                            </td>
                            <td>
                              {c.travel_time_min} min (
                              {c.share_of_total != null
                                ? `${(c.share_of_total * 100).toFixed(0)}%`
                                : "n/a"}
                              )
                            </td>
                            <td>
                              <code>{c.travel_time_basis ?? "?"}</code> /{" "}
                              <ConfidenceBadge value={c.confidence} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
          </section>
        )}

        {/* 4. Synthetic Draft Alerts & Export */}
        {alerts && (
          <section className="card">
            <div className="section-title-row">
              <h3>
                Synthetic Community Draft Alerts (<code>{alerts.event_id}</code>)
              </h3>
              <div className="btn-row">
                <button
                  type="button"
                  className="btn-chip"
                  onClick={() => onExportAlerts(alerts.event_id, "json")}
                >
                  Export JSON
                </button>
                <button
                  type="button"
                  className="btn-chip"
                  onClick={() => onExportAlerts(alerts.event_id, "text")}
                >
                  Export Text (.txt)
                </button>
                {exportedTextPreview && (
                  <button
                    type="button"
                    className="btn-chip"
                    onClick={() => {
                      void navigator.clipboard?.writeText(exportedTextPreview);
                      setCopiedNotice("Copied draft alert sheet to clipboard.");
                      setTimeout(() => setCopiedNotice(null), 2500);
                    }}
                  >
                    Copy Text
                  </button>
                )}
              </div>
            </div>
            <p className="muted-note">
              {alerts.disclaimer} ({alerts.alerts.length} draft(s) generated — all{" "}
              <code>status=&quot;draft&quot;</code>, <code>sent=false</code>)
            </p>
            {copiedNotice && (
              <p className="muted-note" role="status">
                <strong>{copiedNotice}</strong>
              </p>
            )}
            {alerts.alerts.map((a) => (
              <div key={a.alert_id} className="subcard warn-subcard">
                <div className="alert-header">
                  <strong>{a.target_name || a.target_place_id}</strong>
                  <div>
                    <StatusBadge value={a.status} />{" "}
                    <span className="badge neutral">sent: {String(a.sent)}</span>
                  </div>
                </div>
                <p className="alert-body">{a.text}</p>
                <div className="alert-meta">
                  Alert ID: <code>{a.alert_id}</code> | Channel: <code>{a.channel}</code> | Hospital
                  route segments: <code>{a.routed_via_segments.join(", ") || "none"}</code>
                </div>
              </div>
            ))}
            {exportedTextPreview && (
              <details open>
                <summary>Exported Plain-Text Draft Alert Sheet Preview</summary>
                <pre className="code-block">{exportedTextPreview}</pre>
              </details>
            )}
          </section>
        )}

        {/* 5. Closure Event Timeline & Historical Replay */}
        <section className="card">
          <div className="section-title-row">
            <h3>Closure Event Timeline ({timeline.length} stored events)</h3>
            {replayedEventId && (
              <button type="button" className="btn-chip" onClick={onExitReplay}>
                Exit Replay ({replayedEventId})
              </button>
            )}
          </div>
          <p className="muted-note">
            Generated from real stored <code>ClosureEvent</code> records in the graph. Click{" "}
            <strong>Replay</strong> on any historical or simulated event to inspect its reachability,
            detour route comparison, and draft alert plan.
          </p>
          {timeline.length === 0 ? (
            <p className="muted-note">No closure events stored in the graph yet.</p>
          ) : (
            <div className="table-wrapper scroller">
              <table
                className="data-table"
                style={
                  {
                    "--label-1": '"# / Event ID"',
                    "--label-2": '"Started / Status"',
                    "--label-3": '"Origin / Conf."',
                    "--label-4": '"Actions"',
                  } as React.CSSProperties
                }
              >
                <thead>
                  <tr>
                    <th># / Event ID</th>
                    <th>Started / Status</th>
                    <th>Origin / Conf.</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {timeline.map((ev) => {
                    const isSim = ev.data_status === "simulated";
                    const isReplaying = replayedEventId === ev.id;
                    return (
                      <tr
                        key={ev.id}
                        className={isReplaying ? "row-highlight" : ""}
                      >
                        <th>
                          #{ev.index}{" "}
                          <button
                            type="button"
                            className="inline-link"
                            onClick={() => onSelectFeature({ kind: "closure", id: ev.id })}
                          >
                            {ev.id}
                          </button>
                          <div className="muted-note">
                            Segs: <code>{ev.blocks_segments.join(", ") || "none"}</code>
                          </div>
                        </th>
                        <td>
                          <div>{ev.started_at || "undated"}</div>
                          <StatusBadge value={ev.status} />{" "}
                          {ev.remembered && <span className="badge ok">remembered</span>}
                        </td>
                        <td>
                          <span className={`badge ${isSim ? "warn" : "info"}`}>
                            {isSim ? "SIMULATED" : "OBSERVED"}
                          </span>{" "}
                          <ConfidenceBadge value={ev.confidence} />
                        </td>
                        <td>
                          <div className="btn-row">
                            <button
                              type="button"
                              className={`btn-chip ${isReplaying ? "active" : ""}`}
                              onClick={() => onReplayEvent(ev.id)}
                            >
                              {isReplaying ? "Replaying" : "Replay"}
                            </button>
                            <button
                              type="button"
                              className="btn-chip"
                              onClick={() => onExportAlerts(ev.id, "json")}
                            >
                              JSON
                            </button>
                            <button
                              type="button"
                              className="btn-chip"
                              onClick={() => onExportAlerts(ev.id, "text")}
                            >
                              TXT
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* 6. Closure Resolution & Baseline Audit */}
        {resolveAudit && (
          <section className="card">
            <h3>
              Closure Resolution &amp; Baseline Audit (<code>{resolveAudit.event_id}</code>)
            </h3>
            <dl className="kv-list">
              <div>
                <dt>Event Status</dt>
                <dd>
                  <StatusBadge value={resolveAudit.event.status} /> (resolved:{" "}
                  <strong>{String(resolveAudit.resolved)}</strong>)
                </dd>
              </div>
              <div>
                <dt>Returned to Baseline</dt>
                <dd>
                  <StatusBadge
                    value={
                      resolveAudit.returned_to_baseline === true
                        ? "true (verified)"
                        : resolveAudit.returned_to_baseline === false
                          ? "false"
                          : "unverifiable"
                    }
                  />
                </dd>
              </div>
              <div>
                <dt>Restored Segments</dt>
                <dd>
                  <code>{resolveAudit.restored_segments.join(", ") || "none"}</code>
                </dd>
              </div>
              <div>
                <dt>History Preserved</dt>
                <dd>
                  event node kept: <code>{String(resolveAudit.history_preserved.event_node_kept)}</code>
                  , BLOCKS links kept: <code>{resolveAudit.history_preserved.blocks_links_kept}</code>
                </dd>
              </div>
            </dl>
            <ul className="compact-list">
              {resolveAudit.audit.checks.map((chk) => (
                <li key={chk.name}>
                  <strong>{chk.ok ? "PASS" : "FAIL"}</strong> <code>{chk.name}</code>: {chk.detail}
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* 7. Network Weak Links */}
        {weakLinks && (
          <section className="card">
            <h3>
              Corridor Weak Links (Betweenness Centrality, <code>{weakLinks.corridor_id}</code>)
            </h3>
            <p className="muted-note">{weakLinks.representation}</p>
            <div className="table-wrapper scroller">
              <table
                className="data-table"
                style={
                  {
                    "--label-1": '"Rank / Segment"',
                    "--label-2": '"Betweenness"',
                    "--label-3": '"SP Pairs"',
                    "--label-4": '"Cut-Off / Conf."',
                  } as React.CSSProperties
                }
              >
                <thead>
                  <tr>
                    <th>Rank / Segment</th>
                    <th>Betweenness</th>
                    <th>SP Pairs</th>
                    <th>Cut-Off / Conf.</th>
                  </tr>
                </thead>
                <tbody>
                  {weakLinks.segments.map((s) => (
                    <tr key={s.segment_id}>
                      <th>
                        #{s.rank}{" "}
                        <button
                          type="button"
                          className="inline-link"
                          onClick={() =>
                            onSelectFeature({ kind: "segment", id: s.segment_id })
                          }
                        >
                          {s.segment_id}
                        </button>{" "}
                        ({s.name || s.ref || "road"})
                      </th>
                      <td>{s.betweenness.toFixed(4)}</td>
                      <td>{s.ordered_pairs_whose_chosen_shortest_path_uses_it}</td>
                      <td>
                        {s.if_closed
                          ? `${s.if_closed.settlements_cut_off.length} settlement(s)`
                          : "n/a"}{" "}
                        <ConfidenceBadge value={s.confidence} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {weakLinks.bridges.length > 0 && (
              <>
                <h4>Ranked Bridges</h4>
                <div className="chip-row">
                  {weakLinks.bridges.map((b) => (
                    <button
                      key={b.bridge_id}
                      type="button"
                      className="entity-chip"
                      onClick={() => onSelectFeature({ kind: "bridge", id: b.bridge_id })}
                    >
                      #{b.rank} {b.name || b.bridge_id} ({b.betweenness.toFixed(4)}) [
                      {b.confidence || "unverified"}]
                    </button>
                  ))}
                </div>
              </>
            )}
          </section>
        )}

        {/* 8. Seasonal Risk Ranking */}
        {seasonalRisk && (
          <section className="card">
            <h3>Seasonal Monsoon Risk (Month {seasonalRisk.month})</h3>
            <p className="muted-note">
              Seasons:{" "}
              {seasonalRisk.seasons.map((s) => `${s.name || s.id} (m${s.start_month}-m${s.end_month})`).join(", ") ||
                "No active season for this month"}
            </p>
            {seasonalRisk.ranking_method && (
              <p className="muted-note">{seasonalRisk.ranking_method}</p>
            )}
            {seasonalRisk.ranked.length === 0 ? (
              <p className="muted-note">
                No segments or bridges carry an in-season risk label or closure history for month{" "}
                {seasonalRisk.month}.
              </p>
            ) : (
              <>
                <div className="table-wrapper scroller">
                  <table
                    className="data-table"
                    style={
                      {
                        "--label-1": '"Rank / Item"',
                        "--label-2": '"Risk / Score"',
                        "--label-3": '"Observed Events"',
                        "--label-4": '"Simulated Events"',
                      } as React.CSSProperties
                    }
                  >
                    <thead>
                      <tr>
                        <th>Rank / Item</th>
                        <th>Risk / Score</th>
                        <th>Observed Events</th>
                        <th>Simulated Events</th>
                      </tr>
                    </thead>
                    <tbody>
                      {seasonalRisk.ranked.map((r) => (
                        <tr key={`${r.kind}-${r.id}`}>
                          <th>
                            #{r.rank}{" "}
                            <button
                              type="button"
                              className="inline-link"
                              onClick={() =>
                                onSelectFeature({
                                  kind: r.kind === "bridge" ? "bridge" : "segment",
                                  id: r.id,
                                })
                              }
                            >
                              {r.id}
                            </button>{" "}
                            ({r.kind}) <ConfidenceBadge value={r.confidence} />
                          </th>
                          <td>
                            <StatusBadge value={r.monsoon_risk ?? "unlabelled"} />
                            {r.scoring_breakdown && (
                              <span className="muted-note">
                                {" "}
                                ({r.scoring_breakdown.composite_score} pts)
                              </span>
                            )}
                          </td>
                          <td>
                            <span className="badge info">{r.counts.observed} OBSERVED</span>
                          </td>
                          <td>
                            <span className="badge warn">{r.counts.simulated} SIMULATED</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <h4>Seasonal Advisory &amp; Evidence Breakdown</h4>
                {seasonalRisk.ranked.slice(0, 5).map((r) => (
                  <div key={`ev-${r.kind}-${r.id}`} className="subcard info-subcard">
                    <div>
                      <strong>
                        Rank #{r.rank} {r.name || r.id} (<code>{r.id}</code>)
                      </strong>{" "}
                      — {r.advisory || "Elevated modeled risk — historical pattern warrants review."}
                    </div>
                    {r.evidence_summary && (
                      <div className="muted-note">{r.evidence_summary}</div>
                    )}
                  </div>
                ))}
              </>
            )}
          </section>
        )}

        {/* 9. Data-Source & Confidence Inspection */}
        {graph && (
          <section className="card">
            <h3>Data-Source &amp; Confidence Inspection</h3>
            <dl className="kv-list">
              <div>
                <dt>Dataset ID / Kind</dt>
                <dd>
                  <code>{graph.dataset?.id ?? "none"}</code> ({graph.dataset?.kind ?? "unknown"})
                </dd>
              </div>
              <div>
                <dt>Attribution</dt>
                <dd>{graph.dataset?.attribution ?? "OpenStreetMap contributors"}</dd>
              </div>
              <div>
                <dt>Verification Counts</dt>
                <dd>
                  <span className="badge verified">
                    {graph.provenance_summary.verified} verified
                  </span>{" "}
                  <span className="badge unverified">
                    {graph.provenance_summary.unverified} unverified
                  </span>{" "}
                  (of {graph.provenance_summary.entities} total entities)
                </dd>
              </div>
              <div>
                <dt>Network Fingerprint</dt>
                <dd>
                  <code className="fingerprint-code">
                    {graph.dataset?.network_fingerprint ?? "none"}
                  </code>
                </dd>
              </div>
            </dl>
            <p className="muted-note">{graph.provenance_summary.note}</p>

            <details open={showSourceSummary}>
              <summary>
                Inspect Entity Provenance &amp; Data Sources ({filteredSourceRows.length} /{" "}
                {allSourceRows.length} records)
              </summary>
              <div className="source-filter-bar">
                <div className="chip-row" role="group" aria-label="Filter entities by confidence or type">
                  {(
                    [
                      ["all", "All"],
                      ["verified", "Verified"],
                      ["unverified", "Unverified"],
                      ["place", "Places"],
                      ["segment", "Segments"],
                      ["hospital", "Hospitals"],
                      ["bridge", "Bridges"],
                      ["closure", "Closures"],
                    ] as const
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      type="button"
                      className={`btn-chip ${sourceFilter === key ? "active" : ""}`}
                      onClick={() => setSourceFilter(key)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <input
                  type="text"
                  value={sourceQuery}
                  onChange={(e) => setSourceQuery(e.target.value)}
                  placeholder="Filter by ID, name, or source_id…"
                  aria-label="Filter provenance records by text"
                />
              </div>
              <div className="table-wrapper scroller">
                <table
                  className="data-table"
                  style={
                    {
                      "--label-1": '"Entity"',
                      "--label-2": '"Confidence / Status"',
                      "--label-3": '"Last Verified"',
                      "--label-4": '"Source"',
                    } as React.CSSProperties
                  }
                >
                  <thead>
                    <tr>
                      <th>Entity</th>
                      <th>Confidence / Status</th>
                      <th>Last Verified</th>
                      <th>Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredSourceRows.slice(0, 80).map((row) => (
                      <tr key={`${row.kind}:${row.id}`}>
                        <th>
                          <button
                            type="button"
                            className="inline-link"
                            onClick={() => onSelectFeature({ kind: row.kind, id: row.id })}
                          >
                            {row.id}
                          </button>
                          <div className="muted-note">
                            {row.name} ({row.typeLabel})
                          </div>
                        </th>
                        <td>
                          <ConfidenceBadge value={row.confidence} />{" "}
                          <StatusBadge value={row.dataStatus} />
                        </td>
                        <td>{row.lastVerified ?? "null (unverified)"}</td>
                        <td>
                          {row.sourceUrl ? (
                            <a
                              href={row.sourceUrl}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              {row.sourceId || "source link"}
                            </a>
                          ) : (
                            <code>{row.sourceId ?? "synthetic"}</code>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </section>
        )}

        {/* 10. Executed Cypher Queries & Algorithm Traceability */}
        <section className="card">
          <h3>Executed Cypher &amp; Algorithm Trace ({activeCypher.length} queries)</h3>
          {activeAlgorithm && (
            <pre className="code-block">
              {JSON.stringify(activeAlgorithm, null, 2)}
            </pre>
          )}
          {activeCypher.length > 0 ? (
            <details>
              <summary>View {activeCypher.length} executed Cypher statement(s)</summary>
              <ol className="cypher-list">
                {activeCypher.map((q, i) => (
                  <li key={i}>
                    <pre className="code-block">{q}</pre>
                  </li>
                ))}
              </ol>
            </details>
          ) : (
            <p className="muted-note">No Cypher queries recorded yet.</p>
          )}
        </section>
      </div>
    </aside>
  );
}
