import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import BackendStatus from "./BackendStatus";
import MapView from "./MapView";
import WhyPanel from "./WhyPanel";
import {
  fetchCutoff,
  fetchGraph,
  fetchReroute,
  fetchSeasonalRisk,
  fetchWeakLinks,
  postClosure,
  postDraftAlerts,
  postResolveClosure,
} from "./api";
import { SAFETY_DISCLAIMER } from "./config";
import type {
  ChatLogEntry,
  CutoffResponse,
  DraftAlertsResponse,
  GraphResponse,
  RerouteResponse,
  ResolveClosureResponse,
  SelectedFeature,
  SeasonalRiskResponse,
  WeakLinksResponse,
} from "./types";

function nowTimeStr(): string {
  return new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export default function App() {
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);

  // Active backend analysis states
  const [cutoff, setCutoff] = useState<CutoffResponse | null>(null);
  const [reroute, setReroute] = useState<RerouteResponse | null>(null);
  const [weakLinks, setWeakLinks] = useState<WeakLinksResponse | null>(null);
  const [seasonalRisk, setSeasonalRisk] = useState<SeasonalRiskResponse | null>(null);
  const [alerts, setAlerts] = useState<DraftAlertsResponse | null>(null);
  const [resolveAudit, setResolveAudit] = useState<ResolveClosureResponse | null>(null);

  // Latest executed Cypher, algorithm, warnings, and explanation
  const [activeCypher, setActiveCypher] = useState<string[]>([]);
  const [activeAlgorithm, setActiveAlgorithm] = useState<Record<string, unknown> | null>(null);
  const [activeWarnings, setActiveWarnings] = useState<string[]>([]);
  const [activeExplanation, setActiveExplanation] = useState<string | null>(null);

  // Interactive selection & overlay controls
  const [selectedFeature, setSelectedFeature] = useState<SelectedFeature | null>(null);
  const [showTopologyOverlay, setShowTopologyOverlay] = useState<boolean>(false);
  const [showSourceSummary, setShowSourceSummary] = useState<boolean>(false);

  // Left-pane form controls
  const [incidentText, setIncidentText] = useState<string>("Landslide at NH29 near kilometre 42.");
  const [targetSegmentId, setTargetSegmentId] = useState<string>("");
  const [targetBridgeId, setTargetBridgeId] = useState<string>("");
  const [rememberInHistory, setRememberInHistory] = useState<boolean>(false);

  const [routeFromId, setRouteFromId] = useState<string>("");
  const [routeToId, setRouteToId] = useState<string>("");

  // Compact chat / event log
  const [logEntries, setLogEntries] = useState<ChatLogEntry[]>([]);

  const appendLog = useCallback((entry: Omit<ChatLogEntry, "id" | "timestamp">) => {
    setLogEntries((prev) => [
      {
        ...entry,
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        timestamp: nowTimeStr(),
      },
      ...prev.slice(0, 29),
    ]);
  }, []);

  // Load corridor graph from GET /api/graph
  const loadGraphData = useCallback(
    async (silent = false): Promise<GraphResponse | null> => {
      if (!silent) setLoading(true);
      setErrorBanner(null);
      try {
        const g = await fetchGraph(true);
        setGraph(g);
        if (!silent) {
          setActiveCypher(g.cypher);
          setActiveWarnings(g.warnings);
        }

        // Default origin / destination to Dimapur and Kohima reference nodes when unset
        setRouteFromId((prev) => {
          if (prev && g.places.some((p) => p.id === prev)) return prev;
          const dimapur = g.places.find((p) => p.reference_key === "dimapur");
          return dimapur?.id || g.places[0]?.id || "";
        });
        setRouteToId((prev) => {
          if (prev && g.places.some((p) => p.id === prev)) return prev;
          const kohima = g.places.find((p) => p.reference_key === "kohima");
          return kohima?.id || g.places[g.places.length - 1]?.id || "";
        });

        return g;
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: "Failed to load corridor graph",
          detail: msg,
        });
        return null;
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [appendLog],
  );

  useEffect(() => {
    void loadGraphData(false);
  }, [loadGraphData]);

  // Apply or resolve a closure via POST /api/closure, then refresh graph, cutoff, and reroute
  const submitClosure = useCallback(
    async (
      description: string,
      explicitSegmentId?: string | null,
      explicitBridgeId?: string | null,
    ) => {
      if (!description.trim()) return;
      setLoading(true);
      setErrorBanner(null);

      const segToUse =
        explicitSegmentId !== undefined
          ? explicitSegmentId
          : targetSegmentId.trim() || null;
      const bridgeToUse =
        explicitBridgeId !== undefined
          ? explicitBridgeId
          : targetBridgeId.trim() || null;

      appendLog({
        role: "user",
        title: "Simulate Landslide / Closure",
        detail: `${description.trim()}${segToUse ? ` [segment: ${segToUse}]` : ""}${
          bridgeToUse ? ` [bridge: ${bridgeToUse}]` : ""
        }`,
      });

      try {
        const res = await postClosure({
          description: description.trim(),
          target_segment_id: segToUse,
          target_bridge_id: bridgeToUse,
          remember_in_history: rememberInHistory,
        });

        setActiveCypher(res.cypher);
        setActiveAlgorithm(res.algorithm);
        setActiveWarnings(res.warnings);
        setActiveExplanation(res.explanation);

        if (res.status === "clarification_required") {
          appendLog({
            role: "system",
            title: "Clarification Required (Graph Not Modified)",
            detail: res.resolution.clarification_prompt || res.explanation,
            clarificationCandidates: {
              originalDescription: description.trim(),
              prompt: res.resolution.clarification_prompt || "Select the intended road segment:",
              segments: res.resolution.candidate_segments,
              bridges: res.resolution.candidate_bridges,
            },
          });
          return;
        }

        if (res.status === "no_match") {
          appendLog({
            role: "system",
            title: "No Matching Location in Corridor Graph",
            detail: res.explanation,
          });
          return;
        }

        // Applied closure: refresh graph, calculate cut-off settlements/hospitals, and calculate alternate route
        const eventId = res.event_id!;
        const blockedSegId = res.target_segment?.id;
        if (blockedSegId) {
          setSelectedFeature({ kind: "segment", id: blockedSegId });
        }

        const updatedGraph = await loadGraphData(true);
        const cutoffData = await fetchCutoff(eventId);
        setCutoff(cutoffData);
        setResolveAudit(null);

        const originId =
          routeFromId ||
          updatedGraph?.places.find((p) => p.reference_key === "dimapur")?.id ||
          "";
        const destId =
          routeToId ||
          updatedGraph?.places.find((p) => p.reference_key === "kohima")?.id ||
          "";

        let routeSummary = "";
        let combinedCypher = [...res.cypher, ...cutoffData.cypher];
        let combinedWarnings = Array.from(new Set([...res.warnings, ...cutoffData.warnings]));

        if (originId && destId) {
          const rerouteData = await fetchReroute(originId, destId, eventId);
          setReroute(rerouteData);
          combinedCypher = [...combinedCypher, ...rerouteData.cypher];
          combinedWarnings = Array.from(new Set([...combinedWarnings, ...rerouteData.warnings]));
          if (rerouteData.status === "route_found" && rerouteData.route) {
            const delta =
              rerouteData.additional_travel_time_min != null
                ? ` (${rerouteData.additional_travel_time_min >= 0 ? "+" : ""}${
                    rerouteData.additional_travel_time_min
                  } min vs baseline)`
                : "";
            routeSummary = ` Alternate route ${originId}→${destId}: ${rerouteData.route.total_travel_time_min} min${delta}.`;
          } else {
            routeSummary = ` No valid route ${originId}→${destId}: ${rerouteData.reason || "blocked"}.`;
          }
        }

        setActiveCypher(combinedCypher);
        setActiveWarnings(combinedWarnings);
        setActiveExplanation(`${res.explanation} ${cutoffData.explanation}${routeSummary}`);

        appendLog({
          role: "system",
          title: `Closure ${eventId} Applied on ${blockedSegId}`,
          detail: `${cutoffData.explanation}${routeSummary}`,
          eventId,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: "Closure Request Failed",
          detail: msg,
        });
      } finally {
        setLoading(false);
      }
    },
    [
      targetSegmentId,
      targetBridgeId,
      rememberInHistory,
      appendLog,
      loadGraphData,
      routeFromId,
      routeToId,
    ],
  );

  const handleClosureFormSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submitClosure(incidentText);
  };

  // Clear a simulated closure and run baseline audit via POST /api/resolve/{event_id}
  const handleClearClosure = useCallback(
    async (eventId: string) => {
      setLoading(true);
      setErrorBanner(null);
      try {
        const res = await postResolveClosure(eventId, rememberInHistory);
        const updatedGraph = await loadGraphData(true);
        setResolveAudit(res);

        // If no active closures remain, clear cutoff/alerts and recompute baseline route
        const remainingActive =
          updatedGraph?.closures.filter((c) => c.status === "active") ?? [];
        if (remainingActive.length === 0) {
          setCutoff(null);
          setAlerts(null);
        } else {
          const nextCutoff = await fetchCutoff(remainingActive[0].id);
          setCutoff(nextCutoff);
        }

        if (routeFromId && routeToId) {
          const rerouteData = await fetchReroute(routeFromId, routeToId);
          setReroute(rerouteData);
        }

        setActiveCypher(res.cypher);
        setActiveAlgorithm(res.algorithm);
        setActiveWarnings(res.warnings);
        setActiveExplanation(res.explanation);

        appendLog({
          role: "system",
          title: `Resolved & Audited ${eventId}`,
          detail: res.explanation,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: `Failed to Resolve ${eventId}`,
          detail: msg,
        });
      } finally {
        setLoading(false);
      }
    },
    [rememberInHistory, loadGraphData, routeFromId, routeToId, appendLog],
  );

  // Draft community SMS alerts via POST /api/alerts/{event_id}
  const handleDraftAlerts = useCallback(
    async (eventId: string) => {
      setLoading(true);
      setErrorBanner(null);
      try {
        const res = await postDraftAlerts(eventId);
        setAlerts(res);
        setActiveCypher(res.cypher);
        setActiveAlgorithm(res.algorithm);
        setActiveWarnings(res.warnings);
        setActiveExplanation(res.explanation);
        appendLog({
          role: "system",
          title: `Drafted ${res.alerts.length} Synthetic Alert(s) for ${eventId}`,
          detail: res.explanation,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: "Alert Drafting Failed",
          detail: msg,
        });
      } finally {
        setLoading(false);
      }
    },
    [appendLog],
  );

  // Inspect cutoff for an event via GET /api/cutoff/{event_id}
  const handleInspectCutoff = useCallback(
    async (eventId: string) => {
      setLoading(true);
      setErrorBanner(null);
      try {
        const res = await fetchCutoff(eventId);
        setCutoff(res);
        setActiveCypher(res.cypher);
        setActiveAlgorithm(res.algorithm);
        setActiveWarnings(res.warnings);
        setActiveExplanation(res.explanation);
        appendLog({
          role: "system",
          title: `Inspected Reachability for ${eventId}`,
          detail: res.explanation,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
      } finally {
        setLoading(false);
      }
    },
    [appendLog],
  );

  // Calculate route via GET /api/reroute
  const handleCalculateReroute = useCallback(async () => {
    if (!routeFromId || !routeToId) return;
    setLoading(true);
    setErrorBanner(null);
    try {
      const res = await fetchReroute(routeFromId, routeToId);
      setReroute(res);
      setActiveCypher(res.cypher);
      setActiveAlgorithm(res.algorithm);
      setActiveWarnings(res.warnings);
      setActiveExplanation(res.explanation);
      appendLog({
        role: "system",
        title: `Route ${routeFromId} → ${routeToId}`,
        detail: res.explanation,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorBanner(msg);
      appendLog({
        role: "error",
        title: "Route Calculation Failed",
        detail: msg,
      });
    } finally {
      setLoading(false);
    }
  }, [routeFromId, routeToId, appendLog]);

  // Show weak links via GET /api/weak-links
  const handleShowWeakLinks = useCallback(async () => {
    setLoading(true);
    setErrorBanner(null);
    try {
      const res = await fetchWeakLinks(graph?.corridors[0] ?? null, 5);
      setWeakLinks(res);
      setActiveCypher(res.cypher);
      setActiveAlgorithm(res.algorithm);
      setActiveWarnings(res.warnings);
      setActiveExplanation(res.explanation);
      if (res.segments[0]) {
        setSelectedFeature({ kind: "segment", id: res.segments[0].segment_id });
      }
      appendLog({
        role: "system",
        title: "Corridor Weak Links Ranked",
        detail: res.explanation,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorBanner(msg);
      appendLog({
        role: "error",
        title: "Weak-Links Analysis Failed",
        detail: msg,
      });
    } finally {
      setLoading(false);
    }
  }, [graph, appendLog]);

  // Seasonal risk via GET /api/seasonal-risk?month=...
  const handleSeasonalRisk = useCallback(
    async (month: number, label: string) => {
      setLoading(true);
      setErrorBanner(null);
      try {
        const res = await fetchSeasonalRisk(month);
        setSeasonalRisk(res);
        setActiveCypher(res.cypher);
        setActiveAlgorithm(res.algorithm);
        setActiveWarnings(res.warnings);
        setActiveExplanation(res.explanation);
        appendLog({
          role: "system",
          title: `Seasonal Risk (${label})`,
          detail: res.explanation,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: `Seasonal Risk (${label}) Failed`,
          detail: msg,
        });
      } finally {
        setLoading(false);
      }
    },
    [appendLog],
  );

  const activeClosures = useMemo(
    () => graph?.closures.filter((c) => c.status === "active") ?? [],
    [graph],
  );

  // Pick sample segments from the loaded graph for 1-click presets
  const sampleSegments = useMemo(() => {
    if (!graph) return [];
    const nh29 = graph.segments.filter((s) => s.on_nh29);
    const others = graph.segments.filter((s) => !s.on_nh29);
    return [...nh29.slice(0, 2), ...others.slice(0, 1)];
  }, [graph]);

  return (
    <div className="app">
      {/* Top Bar */}
      <header className="bar">
        <div className="brand">
          <h1>Landslide Router</h1>
          <span className="subtitle">Dimapur–Kohima NH29 Corridor Graph</span>
        </div>
        {graph && (
          <div className="graph-stats" aria-label="Loaded corridor graph counts">
            <span>
              <strong>{graph.counts.places}</strong> places
            </span>
            <span>
              <strong>{graph.counts.segments}</strong> segments
            </span>
            <span>
              <strong>{graph.counts.hospitals}</strong> hospitals
            </span>
            <span>
              <strong>{graph.counts.bridges}</strong> bridges
            </span>
            <span className={graph.counts.active_closures > 0 ? "stat-alert" : ""}>
              <strong>{graph.counts.active_closures}</strong> active closure(s)
            </span>
          </div>
        )}
        <div className="bar-actions">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => void loadGraphData(false)}
            disabled={loading}
          >
            {loading ? "Working…" : "Reload Graph"}
          </button>
        </div>
      </header>

      {/* Mandatory Persistent Safety Disclaimer */}
      <p className="disclaimer" role="note">
        <strong>Safety Disclaimer:</strong> {SAFETY_DISCLAIMER}
      </p>

      {errorBanner && (
        <div className="error-banner" role="alert">
          <strong>Backend Error:</strong> {errorBanner}
          <button
            type="button"
            className="btn-dismiss"
            onClick={() => setErrorBanner(null)}
            aria-label="Dismiss error"
          >
            ×
          </button>
        </div>
      )}

      {/* Three-Pane Main Workspace */}
      <div className="workspace">
        {/* LEFT PANE: Chat & Event Controls */}
        <aside className="pane left-pane" aria-label="Event simulation and routing controls">
          <div className="pane-header">
            <h2>Corridor Controls &amp; Incident Input</h2>
          </div>

          <div className="pane-scroll scroller">
            {/* 1. Simulate Landslide Form */}
            <section className="card">
              <h3>1. Simulate Landslide / Road Closure</h3>
              <form onSubmit={handleClosureFormSubmit} className="control-form">
                <label className="field-label" htmlFor="incident-input">
                  Natural-Language Incident Description
                </label>
                <textarea
                  id="incident-input"
                  rows={2}
                  value={incidentText}
                  onChange={(e) => setIncidentText(e.target.value)}
                  placeholder="e.g. Landslide at NH29 near kilometre 42."
                  required
                />

                <div className="form-row">
                  <div className="form-col">
                    <label className="field-label" htmlFor="target-seg-select">
                      Target Segment (Optional)
                    </label>
                    <select
                      id="target-seg-select"
                      value={targetSegmentId}
                      onChange={(e) => setTargetSegmentId(e.target.value)}
                    >
                      <option value="">Auto-resolve from text…</option>
                      {graph?.segments.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.id} ({s.from_place_id}→{s.to_place_id}
                          {s.on_nh29 ? ", NH29" : ""})
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="form-col">
                    <label className="field-label" htmlFor="target-bridge-select">
                      Target Bridge (Optional)
                    </label>
                    <select
                      id="target-bridge-select"
                      value={targetBridgeId}
                      onChange={(e) => setTargetBridgeId(e.target.value)}
                    >
                      <option value="">None</option>
                      {graph?.bridges.map((b) => (
                        <option key={b.id} value={b.id}>
                          {b.id} ({b.name || "bridge"})
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={rememberInHistory}
                    onChange={(e) => setRememberInHistory(e.target.checked)}
                  />
                  <span>Persist closure in historical memory (remember_closure)</span>
                </label>

                <button type="submit" className="btn-primary" disabled={loading}>
                  Simulate Landslide
                </button>
              </form>

              {/* Quick Scenario Presets */}
              <div className="preset-group">
                <span className="preset-label">Quick Test Scenarios:</span>
                <div className="chip-row">
                  <button
                    type="button"
                    className="btn-chip"
                    disabled={loading}
                    onClick={() => {
                      setIncidentText("Landslide at NH29 near kilometre 42.");
                      setTargetSegmentId("");
                      setTargetBridgeId("");
                      void submitClosure("Landslide at NH29 near kilometre 42.", null, null);
                    }}
                  >
                    NH29 km 42 (Ambiguous)
                  </button>
                  {sampleSegments.map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      className="btn-chip"
                      disabled={loading}
                      onClick={() => {
                        const desc = `Landslide on ${s.name || s.ref || "road"} (${s.id})`;
                        setIncidentText(desc);
                        setTargetSegmentId(s.id);
                        void submitClosure(desc, s.id, null);
                      }}
                    >
                      Block {s.id} ({s.from_place_id}→{s.to_place_id})
                    </button>
                  ))}
                </div>
              </div>
            </section>

            {/* 2. Active Closures & Clear Closure */}
            <section className="card">
              <h3>2. Active Closures ({activeClosures.length})</h3>
              {activeClosures.length === 0 ? (
                <p className="muted-note">
                  No active closures in the graph. All corridor segments are currently open.
                </p>
              ) : (
                <div className="closure-list">
                  {activeClosures.map((c) => (
                    <div key={c.id} className="subcard danger-subcard">
                      <div className="closure-item-head">
                        <strong>{c.id}</strong>
                        <span className="badge warn">{c.data_status}</span>
                      </div>
                      <div className="closure-item-desc">{c.description}</div>
                      <div className="closure-item-meta">
                        Blocks segment(s): <code>{c.blocks_segments.join(", ")}</code>
                        {c.blocks_bridges.length > 0 && (
                          <>
                            {" "}
                            | bridge(s): <code>{c.blocks_bridges.join(", ")}</code>
                          </>
                        )}
                      </div>
                      <div className="btn-row">
                        <button
                          type="button"
                          className="btn-secondary"
                          disabled={loading}
                          onClick={() => void handleClearClosure(c.id)}
                        >
                          Clear Closure
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          disabled={loading}
                          onClick={() => void handleInspectCutoff(c.id)}
                        >
                          Inspect Cutoff
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          disabled={loading}
                          onClick={() => void handleDraftAlerts(c.id)}
                        >
                          Draft Alerts
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* 3. Inspect Alternate Routes */}
            <section className="card">
              <h3>3. Inspect Alternate Routes</h3>
              <div className="form-row">
                <div className="form-col">
                  <label className="field-label" htmlFor="route-from-select">
                    From Place
                  </label>
                  <select
                    id="route-from-select"
                    value={routeFromId}
                    onChange={(e) => setRouteFromId(e.target.value)}
                  >
                    {graph?.places.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name ? `${p.name} (${p.id})` : p.id} [{p.type}]
                      </option>
                    ))}
                  </select>
                </div>
                <div className="form-col">
                  <label className="field-label" htmlFor="route-to-select">
                    To Place
                  </label>
                  <select
                    id="route-to-select"
                    value={routeToId}
                    onChange={(e) => setRouteToId(e.target.value)}
                  >
                    {graph?.places.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name ? `${p.name} (${p.id})` : p.id} [{p.type}]
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <button
                type="button"
                className="btn-secondary full-width"
                disabled={loading || !routeFromId || !routeToId}
                onClick={() => void handleCalculateReroute()}
              >
                Inspect Alternate Route
              </button>
            </section>

            {/* 4. Weak Links, Seasonal Risk & Provenance Controls */}
            <section className="card">
              <h3>4. Network Risk &amp; Provenance</h3>
              <div className="action-grid">
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={loading}
                  onClick={() => void handleShowWeakLinks()}
                >
                  Show Weak Links
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={loading}
                  onClick={() => void handleSeasonalRisk(7, "July")}
                >
                  Set Month to July
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={loading}
                  onClick={() => void handleSeasonalRisk(8, "August")}
                >
                  Set Month to August
                </button>
                <button
                  type="button"
                  className={`btn-secondary ${showSourceSummary ? "active" : ""}`}
                  onClick={() => {
                    setShowSourceSummary(true);
                    appendLog({
                      role: "system",
                      title: "Source & Confidence Summary",
                      detail: graph
                        ? `${graph.provenance_summary.verified} verified, ${graph.provenance_summary.unverified} unverified entities (${graph.provenance_summary.note})`
                        : "Graph not loaded yet.",
                    });
                  }}
                >
                  Source &amp; Confidence
                </button>
              </div>
            </section>

            {/* 5. Compact Chat / Event Log */}
            <section className="card">
              <h3>5. Agent &amp; Tool Event Log</h3>
              {logEntries.length === 0 ? (
                <p className="muted-note">
                  Use the controls above to simulate a landslide, inspect alternate routes, or run
                  weak-link and monsoon-risk analyses.
                </p>
              ) : (
                <div className="chat-log">
                  {logEntries.map((entry) => (
                    <div key={entry.id} className={`log-item ${entry.role}`}>
                      <div className="log-head">
                        <strong>{entry.title}</strong>
                        <span className="log-time">{entry.timestamp}</span>
                      </div>
                      <p className="log-detail">{entry.detail}</p>
                      {entry.clarificationCandidates && (
                        <div className="clarification-box">
                          <strong>Confirm Candidate Segment to Close:</strong>
                          <div className="chip-row">
                            {entry.clarificationCandidates.segments.map((cand) => (
                              <button
                                key={cand.segment_id}
                                type="button"
                                className="btn-chip confirm-chip"
                                disabled={loading}
                                onClick={() => {
                                  setTargetSegmentId(cand.segment_id);
                                  void submitClosure(
                                    entry.clarificationCandidates!.originalDescription,
                                    cand.segment_id,
                                    null,
                                  );
                                }}
                              >
                                Confirm {cand.segment_id} ({cand.from_place_name || cand.from_place_id}
                                →{cand.to_place_name || cand.to_place_id})
                              </button>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        </aside>

        {/* CENTER PANE: Interactive MapLibre Map */}
        <main className="center-pane">
          <MapView
            graph={graph}
            cutoff={cutoff}
            reroute={reroute}
            weakLinks={weakLinks}
            seasonalRisk={seasonalRisk}
            selectedFeature={selectedFeature}
            onSelectFeature={setSelectedFeature}
            showTopologyOverlay={showTopologyOverlay}
            onToggleTopologyOverlay={() => setShowTopologyOverlay((v) => !v)}
          />
        </main>

        {/* RIGHT PANE: "Why?" Explanation, Provenance & Result Details */}
        <WhyPanel
          graph={graph}
          selectedFeature={selectedFeature}
          onSelectFeature={setSelectedFeature}
          cutoff={cutoff}
          reroute={reroute}
          weakLinks={weakLinks}
          seasonalRisk={seasonalRisk}
          alerts={alerts}
          resolveAudit={resolveAudit}
          activeCypher={activeCypher}
          activeAlgorithm={activeAlgorithm}
          activeWarnings={activeWarnings}
          activeExplanation={activeExplanation}
          showSourceSummary={showSourceSummary}
        />
      </div>

      {/* Footer Status */}
      <footer>
        <BackendStatus />
      </footer>
    </div>
  );
}
