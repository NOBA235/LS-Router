import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import BackendStatus from "./BackendStatus";
import MapView from "./MapView";
import WhyPanel from "./WhyPanel";
import {
  fetchAlertPlanExportJson,
  fetchAlertPlanExportText,
  fetchCutoff,
  fetchGraph,
  fetchHistory,
  fetchReplay,
  fetchReroute,
  fetchSeasonalRisk,
  fetchWeakLinks,
  postClosure,
  postDraftAlerts,
  postRememberClosure,
  postResolveClosure,
} from "./api";
import { SAFETY_DISCLAIMER } from "./config";
import type {
  ChatLogEntry,
  CutoffResponse,
  DraftAlertsResponse,
  GraphResponse,
  HistoryResponse,
  RerouteResponse,
  ResolveClosureResponse,
  SelectedFeature,
  SeasonalRiskResponse,
  TimelineEntry,
  WeakLinksResponse,
} from "./types";

function nowTimeStr(): string {
  return new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function triggerFileDownload(filename: string, content: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function App() {
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [loadingLabel, setLoadingLabel] = useState<string>("Working…");
  const [errorBanner, setErrorBanner] = useState<string | null>(null);

  // Active backend analysis states
  const [cutoff, setCutoff] = useState<CutoffResponse | null>(null);
  const [reroute, setReroute] = useState<RerouteResponse | null>(null);
  const [weakLinks, setWeakLinks] = useState<WeakLinksResponse | null>(null);
  const [seasonalRisk, setSeasonalRisk] = useState<SeasonalRiskResponse | null>(null);
  const [alerts, setAlerts] = useState<DraftAlertsResponse | null>(null);
  const [exportedTextPreview, setExportedTextPreview] = useState<string | null>(null);
  const [resolveAudit, setResolveAudit] = useState<ResolveClosureResponse | null>(null);
  const [replayedEventId, setReplayedEventId] = useState<string | null>(null);

  // Latest executed Cypher, algorithm, warnings, and explanation
  const [activeCypher, setActiveCypher] = useState<string[]>([]);
  const [activeAlgorithm, setActiveAlgorithm] = useState<Record<string, unknown> | null>(null);
  const [activeWarnings, setActiveWarnings] = useState<string[]>([]);
  const [activeExplanation, setActiveExplanation] = useState<string | null>(null);

  // Interactive selection & overlay controls
  const [selectedFeature, setSelectedFeature] = useState<SelectedFeature | null>(null);
  const [showTopologyOverlay, setShowTopologyOverlay] = useState<boolean>(false);
  const [showSourceSummary, setShowSourceSummary] = useState<boolean>(false);
  const [mobilePane, setMobilePane] = useState<"controls" | "map" | "why">("map");

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

  // Load corridor graph from GET /api/graph and stored event timeline from GET /api/history
  const loadGraphData = useCallback(
    async (silent = false): Promise<GraphResponse | null> => {
      if (!silent) {
        setLoading(true);
        setLoadingLabel("Loading corridor graph & event history…");
      }
      setErrorBanner(null);
      try {
        const [g, h] = await Promise.all([
          fetchGraph(true),
          fetchHistory().catch(() => null),
        ]);
        setGraph(g);
        if (h) setHistory(h);
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
      setLoadingLabel("Resolving location & simulating road closure…");
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
        setReplayedEventId(null);
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
      setLoadingLabel(`Resolving closure ${eventId} & auditing baseline…`);
      setErrorBanner(null);
      try {
        const res = await postResolveClosure(eventId, rememberInHistory);
        const updatedGraph = await loadGraphData(true);
        setResolveAudit(res);
        setReplayedEventId(null);

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

  // Remember a closure in historical memory via POST /api/remember/{event_id}
  const handleRememberClosure = useCallback(
    async (eventId: string) => {
      setLoading(true);
      setLoadingLabel(`Persisting ${eventId} in historical memory…`);
      setErrorBanner(null);
      try {
        const res = await postRememberClosure(eventId);
        await loadGraphData(true);
        const expl =
          typeof res.explanation === "string"
            ? res.explanation
            : `Persisted ${eventId} in historical memory.`;
        setActiveExplanation(expl);
        appendLog({
          role: "system",
          title: `Remembered ${eventId} in History`,
          detail: expl,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: `Failed to Remember ${eventId}`,
          detail: msg,
        });
      } finally {
        setLoading(false);
      }
    },
    [loadGraphData, appendLog],
  );

  // Replay any stored closure event from the timeline via GET /api/replay/{event_id}
  const handleReplayClosure = useCallback(
    async (eventId: string) => {
      setLoading(true);
      setLoadingLabel(`Replaying stored event ${eventId} from timeline…`);
      setErrorBanner(null);
      try {
        const res = await fetchReplay(eventId, routeFromId || null, routeToId || null, true);
        setReplayedEventId(eventId);
        setCutoff(res.cutoff);
        setReroute(res.reroute);
        setAlerts(res.alerts);
        setExportedTextPreview(null);
        setActiveCypher(res.cypher);
        setActiveAlgorithm(res.algorithm);
        setActiveWarnings(res.warnings);
        setActiveExplanation(res.explanation);
        if (res.event.blocks_segments.length > 0) {
          setSelectedFeature({ kind: "segment", id: res.event.blocks_segments[0] });
        } else {
          setSelectedFeature({ kind: "closure", id: eventId });
        }
        appendLog({
          role: "system",
          title: `Timeline Replay: ${eventId}`,
          detail: res.explanation,
          eventId,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: `Timeline Replay Failed (${eventId})`,
          detail: msg,
        });
      } finally {
        setLoading(false);
      }
    },
    [routeFromId, routeToId, appendLog],
  );

  const handleExitReplay = useCallback(async () => {
    setReplayedEventId(null);
    setCutoff(null);
    setAlerts(null);
    setExportedTextPreview(null);
    if (routeFromId && routeToId) {
      try {
        const baseRoute = await fetchReroute(routeFromId, routeToId);
        setReroute(baseRoute);
      } catch {
        // ignore
      }
    }
    appendLog({
      role: "system",
      title: "Exited Timeline Replay",
      detail: "Restored view to current active corridor state.",
    });
  }, [routeFromId, routeToId, appendLog]);

  // Draft community SMS alerts via POST /api/alerts/{event_id}
  const handleDraftAlerts = useCallback(
    async (eventId: string) => {
      setLoading(true);
      setLoadingLabel(`Drafting synthetic community alerts for ${eventId}…`);
      setErrorBanner(null);
      try {
        const res = await postDraftAlerts(eventId);
        setAlerts(res);
        setExportedTextPreview(null);
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

  // Export draft alert plan as JSON or plain text via GET /api/alerts/{event_id}/export
  const handleExportAlerts = useCallback(
    async (eventId: string, format: "json" | "text") => {
      setLoading(true);
      setLoadingLabel(`Exporting draft alert plan (${format.toUpperCase()}) for ${eventId}…`);
      setErrorBanner(null);
      try {
        if (format === "json") {
          const exported = await fetchAlertPlanExportJson(eventId, true);
          setAlerts(exported);
          setExportedTextPreview(exported.text_plan);
          triggerFileDownload(
            `draft-alerts-${eventId}.json`,
            JSON.stringify(exported, null, 2),
            "application/json",
          );
          appendLog({
            role: "system",
            title: `Exported Draft Alert Plan (JSON) for ${eventId}`,
            detail: `${exported.alerts.length} draft alert(s) exported (all_drafts=${exported.all_drafts}, any_sent=${exported.any_sent}).`,
          });
        } else {
          const textPlan = await fetchAlertPlanExportText(eventId, true);
          setExportedTextPreview(textPlan);
          triggerFileDownload(`draft-alerts-${eventId}.txt`, textPlan, "text/plain;charset=utf-8");
          appendLog({
            role: "system",
            title: `Exported Draft Alert Plan (Text) for ${eventId}`,
            detail: `Downloaded draft-alerts-${eventId}.txt (SYNTHETIC DRAFTS — NOT SENT).`,
          });
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorBanner(msg);
        appendLog({
          role: "error",
          title: `Alert Export (${format.toUpperCase()}) Failed`,
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
      setLoadingLabel(`Calculating reachability & hospital access for ${eventId}…`);
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
    setLoadingLabel(`Calculating minimum-time route ${routeFromId} → ${routeToId}…`);
    setErrorBanner(null);
    try {
      const res = await fetchReroute(routeFromId, routeToId, replayedEventId);
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
  }, [routeFromId, routeToId, replayedEventId, appendLog]);

  // Show weak links via GET /api/weak-links
  const handleShowWeakLinks = useCallback(async () => {
    setLoading(true);
    setLoadingLabel("Computing corridor betweenness centrality weak links…");
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
      setLoadingLabel(`Computing seasonal monsoon risk ranking for ${label}…`);
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

  // Chronological timeline generated from real stored event history
  const timelineEvents: TimelineEntry[] = useMemo(() => {
    if (history && history.timeline.length > 0) {
      return history.timeline;
    }
    if (!graph) return [];
    const sorted = [...graph.closures].sort((a, b) => {
      const aHas = a.started_at ? 0 : 1;
      const bHas = b.started_at ? 0 : 1;
      if (aHas !== bHas) return aHas - bHas;
      const cmp = (a.started_at || "").localeCompare(b.started_at || "");
      return cmp !== 0 ? cmp : a.id.localeCompare(b.id);
    });
    return sorted.map((c, i) => ({ ...c, index: i + 1 }));
  }, [history, graph]);

  const currentReplayIdx = useMemo(() => {
    if (!replayedEventId) return -1;
    return timelineEvents.findIndex((e) => e.id === replayedEventId);
  }, [replayedEventId, timelineEvents]);

  // Pick sample segments from the loaded graph for 1-click presets
  const sampleSegments = useMemo(() => {
    if (!graph) return [];
    const nh29 = graph.segments.filter((s) => s.on_nh29);
    const others = graph.segments.filter((s) => !s.on_nh29);
    return [...nh29.slice(0, 2), ...others.slice(0, 1)];
  }, [graph]);

  return (
    <div className="app" aria-busy={loading}>
      {/* Accessibility Skip Link */}
      <a href="#main-map-pane" className="skip-link">
        Skip to interactive corridor map
      </a>

      {/* Top Bar */}
      <header className="bar">
        <div className="brand">
          <h1>Landslide Router</h1>
          <span className="subtitle">Dimapur–Kohima NH29 Corridor Graph</span>
        </div>
        {graph && (
          <div className="graph-stats" aria-label="Loaded corridor graph counts and source confidence">
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
            <span className="badge verified" title="Manually verified records">
              {graph.provenance_summary.verified} verified
            </span>
            <span className="badge unverified" title="Derived / unverified records">
              {graph.provenance_summary.unverified} unverified
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
      <div className="disclaimer" role="note" aria-label="Safety disclaimer">
        <span className="badge warn">PLANNING PROTOTYPE</span>{" "}
        <strong>Safety Disclaimer:</strong> {SAFETY_DISCLAIMER}
      </div>

      {/* Clear Loading Status Indicator */}
      {loading && (
        <div className="loading-banner" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          <span>{loadingLabel}</span>
        </div>
      )}

      {/* Active Replay Mode Banner */}
      {replayedEventId && (
        <div className="replay-banner" role="status" aria-live="polite">
          <div>
            <span className="badge info">TIMELINE REPLAY</span>{" "}
            <strong>Replaying stored event:</strong> <code>{replayedEventId}</code> (live active
            closures are unchanged)
          </div>
          <div className="btn-row">
            {timelineEvents.length > 1 && (
              <>
                <button
                  type="button"
                  className="btn-chip"
                  disabled={loading || currentReplayIdx <= 0}
                  onClick={() => {
                    if (currentReplayIdx > 0) {
                      void handleReplayClosure(timelineEvents[currentReplayIdx - 1].id);
                    }
                  }}
                >
                  ◀ Prev Event
                </button>
                <button
                  type="button"
                  className="btn-chip"
                  disabled={
                    loading ||
                    currentReplayIdx < 0 ||
                    currentReplayIdx >= timelineEvents.length - 1
                  }
                  onClick={() => {
                    if (currentReplayIdx >= 0 && currentReplayIdx < timelineEvents.length - 1) {
                      void handleReplayClosure(timelineEvents[currentReplayIdx + 1].id);
                    }
                  }}
                >
                  Next Event ▶
                </button>
              </>
            )}
            <button
              type="button"
              className="btn-chip"
              disabled={loading}
              onClick={() => void handleExitReplay()}
            >
              Exit Replay
            </button>
          </div>
        </div>
      )}

      {errorBanner && (
        <div className="error-banner" role="alert">
          <div>
            <strong>Backend Error:</strong> {errorBanner}
          </div>
          <div className="btn-row">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => void loadGraphData(false)}
              disabled={loading}
            >
              Retry
            </button>
            <button
              type="button"
              className="btn-dismiss"
              onClick={() => setErrorBanner(null)}
              aria-label="Dismiss error"
            >
              ×
            </button>
          </div>
        </div>
      )}

      {/* Mobile / Narrow-Viewport Pane Switcher */}
      <nav className="mobile-pane-tabs" aria-label="Workspace pane selector">
        <button
          type="button"
          className={mobilePane === "controls" ? "active" : ""}
          onClick={() => setMobilePane("controls")}
        >
          1. Controls ({activeClosures.length} active)
        </button>
        <button
          type="button"
          className={mobilePane === "map" ? "active" : ""}
          onClick={() => setMobilePane("map")}
        >
          2. Corridor Map
        </button>
        <button
          type="button"
          className={mobilePane === "why" ? "active" : ""}
          onClick={() => setMobilePane("why")}
        >
          3. &ldquo;Why?&rdquo; Evidence &amp; Timeline
        </button>
      </nav>

      {/* Three-Pane Main Workspace */}
      <div className={`workspace mobile-show-${mobilePane}`}>
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
                          {s.on_nh29 ? ", NH29" : ""}) [{s.provenance.confidence}]
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
                          {b.id} ({b.name || "bridge"}) [{b.provenance.confidence}]
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

              {logEntries[0]?.clarificationCandidates && (
                <div className="clarification-box" role="region" aria-label="Ambiguous location clarification">
                  <strong>Clarification Required (Graph Not Modified):</strong>
                  <p className="log-detail">{logEntries[0].detail}</p>
                  <div className="chip-row">
                    {logEntries[0].clarificationCandidates.segments.map((cand) => (
                      <button
                        key={cand.segment_id}
                        type="button"
                        className="btn-chip confirm-chip"
                        disabled={loading}
                        onClick={() => {
                          setTargetSegmentId(cand.segment_id);
                          void submitClosure(
                            logEntries[0].clarificationCandidates!.originalDescription,
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
                  {graph?.segments.some((s) => s.id === "seg-569069510-569069527-1353575333") && (
                    <button
                      type="button"
                      className="btn-chip"
                      disabled={loading}
                      onClick={() => {
                        const sid = "seg-569069510-569069527-1353575333";
                        const desc = "Landslide on NH29 trunk near Piphema (seg-569069510-569069527-1353575333)";
                        setIncidentText(desc);
                        setTargetSegmentId(sid);
                        void submitClosure(desc, sid, null);
                      }}
                    >
                      NH29 Trunk Detour (+90 min)
                    </button>
                  )}
                  {graph?.segments.some((s) => s.id === "seg-2469699767-6286548534-239157848") && (
                    <button
                      type="button"
                      className="btn-chip"
                      disabled={loading}
                      onClick={() => {
                        const sid = "seg-2469699767-6286548534-239157848";
                        const desc = "Landslide on NH29 between Kezo Town and Chakhabama";
                        setIncidentText(desc);
                        setTargetSegmentId(sid);
                        void submitClosure(desc, sid, null);
                      }}
                    >
                      Kezo Town–Chakhabama (Cut Off 3 Villages)
                    </button>
                  )}
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
                        <div>
                          <span className="badge warn">{c.data_status}</span>{" "}
                          <span
                            className={`badge ${
                              c.confidence === "verified" ? "verified" : "unverified"
                            }`}
                          >
                            {c.confidence || "unverified"}
                          </span>
                        </div>
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
                        <button
                          type="button"
                          className="btn-secondary"
                          disabled={loading}
                          onClick={() => void handleExportAlerts(c.id, "json")}
                        >
                          Export JSON
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          disabled={loading}
                          onClick={() => void handleExportAlerts(c.id, "text")}
                        >
                          Export Text
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* 3. Stored Closure Timeline & Replay */}
            <section className="card">
              <div className="section-title-row">
                <h3>3. Closure Timeline &amp; Replay ({timelineEvents.length})</h3>
                {timelineEvents.length > 0 && (
                  <button
                    type="button"
                    className="btn-chip"
                    disabled={loading}
                    onClick={() => {
                      const nextIdx =
                        currentReplayIdx >= 0 && currentReplayIdx < timelineEvents.length - 1
                          ? currentReplayIdx + 1
                          : 0;
                      void handleReplayClosure(timelineEvents[nextIdx].id);
                    }}
                  >
                    {replayedEventId ? "Step Next ▶" : "▶ Replay Timeline"}
                  </button>
                )}
              </div>
              {timelineEvents.length === 0 ? (
                <p className="muted-note">
                  No stored closure events in history yet. Simulate a closure above to populate the
                  timeline.
                </p>
              ) : (
                <div className="timeline-compact-list">
                  {timelineEvents.map((ev) => {
                    const isSim = ev.data_status === "simulated";
                    const isReplaying = replayedEventId === ev.id;
                    return (
                      <div
                        key={ev.id}
                        className={`subcard timeline-item ${isReplaying ? "active-replay" : ""}`}
                      >
                        <div className="closure-item-head">
                          <span>
                            <strong>
                              #{ev.index} {ev.id}
                            </strong>
                          </span>
                          <div>
                            <span className={`badge ${isSim ? "warn" : "info"}`}>
                              {isSim ? "SIMULATED" : "OBSERVED"}
                            </span>{" "}
                            <span
                              className={`badge ${
                                ev.confidence === "verified" ? "verified" : "unverified"
                              }`}
                            >
                              {ev.confidence || "unverified"}
                            </span>
                          </div>
                        </div>
                        <div className="closure-item-meta">
                          <span>{ev.started_at || "undated"}</span> | status:{" "}
                          <code>{ev.status}</code>
                          {ev.remembered && (
                            <>
                              {" "}
                              | <span className="badge ok">remembered</span>
                            </>
                          )}
                        </div>
                        <div className="closure-item-desc">{ev.description}</div>
                        <div className="btn-row">
                          <button
                            type="button"
                            className={`btn-secondary ${isReplaying ? "active" : ""}`}
                            disabled={loading}
                            onClick={() => void handleReplayClosure(ev.id)}
                          >
                            {isReplaying ? "Replaying" : "Replay Event"}
                          </button>
                          {!ev.remembered && (
                            <button
                              type="button"
                              className="btn-secondary"
                              disabled={loading}
                              onClick={() => void handleRememberClosure(ev.id)}
                            >
                              Remember
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn-secondary"
                            disabled={loading}
                            onClick={() => void handleExportAlerts(ev.id, "text")}
                          >
                            Export Drafts
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>

            {/* 4. Inspect & Compare Alternate Routes */}
            <section className="card">
              <h3>4. Inspect &amp; Compare Alternate Routes</h3>
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
                Inspect &amp; Compare Alternate Route
              </button>
            </section>

            {/* 5. Weak Links, Seasonal Risk & Provenance Controls */}
            <section className="card">
              <h3>5. Network Risk &amp; Data-Source Inspection</h3>
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
                    setShowSourceSummary((v) => !v);
                    setMobilePane("why");
                    appendLog({
                      role: "system",
                      title: "Data-Source & Confidence Inspection",
                      detail: graph
                        ? `${graph.provenance_summary.verified} verified, ${graph.provenance_summary.unverified} unverified entities (${graph.provenance_summary.note})`
                        : "Graph not loaded yet.",
                    });
                  }}
                >
                  Inspect Data Sources
                </button>
              </div>
            </section>

            {/* 6. Compact Chat / Event Log */}
            <section className="card">
              <h3>6. Agent &amp; Tool Event Log</h3>
              {logEntries.length === 0 ? (
                <p className="muted-note">
                  Use the controls above to simulate a landslide, replay stored timeline closures,
                  compare alternate routes, or export draft alert plans.
                </p>
              ) : (
                <div className="chat-log" role="log" aria-label="Agent and tool execution log">
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
        <main id="main-map-pane" className="center-pane" tabIndex={-1}>
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
          timeline={timelineEvents}
          replayedEventId={replayedEventId}
          onReplayEvent={(id) => void handleReplayClosure(id)}
          onExitReplay={() => void handleExitReplay()}
          onRememberEvent={(id) => void handleRememberClosure(id)}
          onExportAlerts={(id, fmt) => void handleExportAlerts(id, fmt)}
          exportedTextPreview={exportedTextPreview}
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
