import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { CORRIDOR_BOUNDS, MAP_STYLE_URL } from "./config";
import type {
  CutoffResponse,
  GraphResponse,
  PlaceNode,
  RerouteResponse,
  SelectedFeature,
  SeasonalRiskResponse,
  WeakLinksResponse,
} from "./types";

interface MapViewProps {
  graph: GraphResponse | null;
  cutoff: CutoffResponse | null;
  reroute: RerouteResponse | null;
  weakLinks: WeakLinksResponse | null;
  seasonalRisk: SeasonalRiskResponse | null;
  selectedFeature: SelectedFeature | null;
  onSelectFeature: (feature: SelectedFeature) => void;
  showTopologyOverlay: boolean;
  onToggleTopologyOverlay: () => void;
}

const INTERACTIVE_LAYERS = [
  "closures-circle",
  "hospitals-circle",
  "bridges-circle",
  "places-circle",
  "segments-geom-line",
  "segments-topo-line",
];

function validCoord(lon: number | null | undefined, lat: number | null | undefined): boolean {
  return (
    typeof lon === "number" &&
    typeof lat === "number" &&
    Number.isFinite(lon) &&
    Number.isFinite(lat) &&
    lon >= -180 &&
    lon <= 180 &&
    lat >= -90 &&
    lat <= 90
  );
}

function segmentMidpoint(
  coords: [number, number][],
): [number, number] | null {
  if (!coords.length) return null;
  const midIdx = Math.floor(coords.length / 2);
  if (coords.length === 2) {
    return [
      (coords[0][0] + coords[1][0]) / 2,
      (coords[0][1] + coords[1][1]) / 2,
    ];
  }
  return coords[midIdx];
}

export default function MapView({
  graph,
  cutoff,
  reroute,
  weakLinks,
  seasonalRisk,
  selectedFeature,
  onSelectFeature,
  showTopologyOverlay,
  onToggleTopologyOverlay,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [legendOpen, setLegendOpen] = useState(true);
  const lastDatasetKeyRef = useRef<string | null>(null);

  const onSelectRef = useRef(onSelectFeature);
  onSelectRef.current = onSelectFeature;

  const placesById = useMemo(() => {
    const map = new Map<string, PlaceNode>();
    if (graph) {
      for (const p of graph.places) {
        map.set(p.id, p);
      }
    }
    return map;
  }, [graph]);

  // Build GeoJSON collections from live backend responses
  const geojsonData = useMemo(() => {
    const emptyFC: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
    if (!graph) {
      return {
        roadGeom: emptyFC,
        topoEdges: emptyFC,
        places: emptyFC,
        hospitals: emptyFC,
        hospitalLinks: emptyFC,
        bridges: emptyFC,
        closures: emptyFC,
      };
    }

    const cutOffIds = new Set<string>();
    const baselineUnreachIds = new Set<string>();
    const unreachHospitalIds = new Set<string>();
    if (cutoff) {
      for (const group of [
        cutoff.newly_unreachable.villages,
        cutoff.newly_unreachable.towns,
        cutoff.newly_unreachable.junctions,
        cutoff.newly_unreachable.other,
      ]) {
        for (const item of group) cutOffIds.add(item.id);
      }
      for (const item of cutoff.already_unreachable_in_baseline) {
        baselineUnreachIds.add(item.id);
      }
      for (const h of cutoff.hospitals_newly_unreachable_from_dimapur) {
        unreachHospitalIds.add(h.id);
      }
      for (const hid of cutoff.hospital_reachability
        ?.hospitals_with_no_usable_route_from_any_affected_settlement ?? []) {
        unreachHospitalIds.add(hid);
      }
    }

    const routeSegIds = new Set<string>(
      reroute?.route?.segments.map((s) => s.segment_id) ?? [],
    );
    const baselineRouteSegIds = new Set<string>(
      reroute?.baseline_route?.segments.map((s) => s.segment_id) ?? [],
    );
    const weakSegMap = new Map<string, number>();
    const weakBridgeMap = new Map<string, number>();
    const weakPlaceMap = new Map<string, number>();
    if (weakLinks) {
      for (const s of weakLinks.segments) weakSegMap.set(s.segment_id, s.rank);
      for (const b of weakLinks.bridges) weakBridgeMap.set(b.bridge_id, b.rank);
      for (const p of weakLinks.places) weakPlaceMap.set(p.place_id, p.rank);
    }

    const seasonalSegMap = new Map<string, { rank: number; risk: string }>();
    const seasonalBridgeMap = new Map<string, { rank: number; risk: string }>();
    if (seasonalRisk) {
      for (const item of seasonalRisk.ranked) {
        const entry = { rank: item.rank, risk: item.monsoon_risk || "History" };
        if (item.kind === "segment") seasonalSegMap.set(item.id, entry);
        if (item.kind === "bridge") seasonalBridgeMap.set(item.id, entry);
      }
    }

    // Track parallel topological pairs so schematic edges don't overlap identically
    const pairCounts = new Map<string, number>();

    const roadGeomFeatures: GeoJSON.Feature[] = [];
    const topoEdgeFeatures: GeoJSON.Feature[] = [];
    const segmentCoordsById = new Map<string, [number, number][]>();

    for (const seg of graph.segments) {
      const isBlocked = seg.status === "blocked";
      const inRoute = routeSegIds.has(seg.id);
      const inBaseRoute = !inRoute && baselineRouteSegIds.has(seg.id);
      const weakRank = weakSegMap.get(seg.id) ?? 0;
      const seasonalInfo = seasonalSegMap.get(seg.id);
      const isSelected = selectedFeature?.kind === "segment" && selectedFeature.id === seg.id;

      let visualState = "other";
      if (isBlocked) visualState = "blocked";
      else if (inRoute) visualState = "route";
      else if (weakRank > 0) visualState = "weak_link";
      else if (seasonalInfo) {
        visualState =
          seasonalInfo.risk === "high" ? "seasonal_high" : "seasonal_medium";
      } else if (inBaseRoute) visualState = "baseline_route";
      else if (seg.on_nh29) visualState = "nh29";

      const commonProps = {
        feature_kind: "segment",
        id: seg.id,
        label: seg.name || seg.ref || seg.id,
        status: seg.status,
        visual_state: visualState,
        on_nh29: seg.on_nh29,
        is_selected: isSelected,
        weak_rank: weakRank,
        seasonal_rank: seasonalInfo?.rank ?? 0,
      };

      const hasRealGeometry = Array.isArray(seg.geometry) && seg.geometry.length >= 2;
      if (hasRealGeometry && seg.geometry) {
        segmentCoordsById.set(seg.id, seg.geometry);
        roadGeomFeatures.push({
          type: "Feature",
          geometry: {
            type: "LineString",
            coordinates: seg.geometry,
          },
          properties: commonProps,
        });
      }

      // Build schematic topological edge from endpoint Places
      const pFrom = placesById.get(seg.from_place_id);
      const pTo = placesById.get(seg.to_place_id);
      if (
        pFrom &&
        pTo &&
        validCoord(pFrom.lon, pFrom.lat) &&
        validCoord(pTo.lon, pTo.lat)
      ) {
        const pairKey = [seg.from_place_id, seg.to_place_id].sort().join("::");
        const idx = pairCounts.get(pairKey) ?? 0;
        pairCounts.set(pairKey, idx + 1);

        const aLon = pFrom.lon as number;
        const aLat = pFrom.lat as number;
        const bLon = pTo.lon as number;
        const bLat = pTo.lat as number;

        let coords: [number, number][];
        if (idx === 0) {
          coords = [
            [aLon, aLat],
            [bLon, bLat],
          ];
        } else {
          const dx = bLon - aLon;
          const dy = bLat - aLat;
          const len = Math.hypot(dx, dy) || 0.01;
          const sign = idx % 2 === 1 ? 1 : -1;
          const mag = 0.003 * Math.ceil(idx / 2) * sign;
          const midLon = (aLon + bLon) / 2 + (-dy / len) * mag;
          const midLat = (aLat + bLat) / 2 + (dx / len) * mag;
          coords = [
            [aLon, aLat],
            [midLon, midLat],
            [bLon, bLat],
          ];
        }

        if (!segmentCoordsById.has(seg.id)) {
          segmentCoordsById.set(seg.id, coords);
        }

        if (showTopologyOverlay || !hasRealGeometry) {
          topoEdgeFeatures.push({
            type: "Feature",
            geometry: {
              type: "LineString",
              coordinates: coords,
            },
            properties: {
              ...commonProps,
              is_schematic: true,
            },
          });
        }
      }
    }

    // Places
    const placeFeatures: GeoJSON.Feature[] = [];
    for (const p of graph.places) {
      if (!validCoord(p.lon, p.lat)) continue;
      const isSelected = selectedFeature?.kind === "place" && selectedFeature.id === p.id;
      let reachState = "reachable";
      if (cutOffIds.has(p.id)) reachState = "cut_off";
      else if (baselineUnreachIds.has(p.id)) reachState = "baseline_unreachable";
      else if (
        reroute &&
        (reroute.from_place_id === p.id || reroute.to_place_id === p.id)
      ) {
        reachState = "route_endpoint";
      } else if (weakPlaceMap.has(p.id)) {
        reachState = "weak_link";
      } else if (p.reference_key) {
        reachState = "reference";
      }

      placeFeatures.push({
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: [p.lon as number, p.lat as number],
        },
        properties: {
          feature_kind: "place",
          id: p.id,
          label: p.name || p.id,
          place_type: p.type,
          reach_state: reachState,
          is_selected: isSelected,
          confidence: p.provenance.confidence || "unverified",
        },
      });
    }

    // Hospitals + optional attachment links
    const hospitalFeatures: GeoJSON.Feature[] = [];
    const hospitalLinkFeatures: GeoJSON.Feature[] = [];
    for (const h of graph.hospitals) {
      if (!validCoord(h.lon, h.lat)) continue;
      const isSelected = selectedFeature?.kind === "hospital" && selectedFeature.id === h.id;
      let hospState = "reachable";
      if (!h.access_place_id) hospState = "unattached";
      else if (unreachHospitalIds.has(h.id) || cutOffIds.has(h.access_place_id)) {
        hospState = "unreachable";
      }

      hospitalFeatures.push({
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: [h.lon as number, h.lat as number],
        },
        properties: {
          feature_kind: "hospital",
          id: h.id,
          label: h.name || h.id,
          hosp_state: hospState,
          is_selected: isSelected,
        },
      });

      if (showTopologyOverlay && h.access_place_id) {
        const ap = placesById.get(h.access_place_id);
        if (ap && validCoord(ap.lon, ap.lat)) {
          hospitalLinkFeatures.push({
            type: "Feature",
            geometry: {
              type: "LineString",
              coordinates: [
                [h.lon as number, h.lat as number],
                [ap.lon as number, ap.lat as number],
              ],
            },
            properties: {
              hospital_id: h.id,
              place_id: ap.id,
            },
          });
        }
      }
    }

    // Bridges
    const blockedBridgeIds = new Set<string>();
    for (const c of graph.closures) {
      if (c.status === "active") {
        for (const bid of c.blocks_bridges) blockedBridgeIds.add(bid);
      }
    }

    const bridgeFeatures: GeoJSON.Feature[] = [];
    for (const b of graph.bridges) {
      let lon = b.lon;
      let lat = b.lat;
      if (!validCoord(lon, lat) && b.segment_ids.length > 0) {
        const segCoords = segmentCoordsById.get(b.segment_ids[0]);
        const mid = segCoords ? segmentMidpoint(segCoords) : null;
        if (mid) [lon, lat] = mid;
      }
      if (!validCoord(lon, lat)) continue;

      const isSelected = selectedFeature?.kind === "bridge" && selectedFeature.id === b.id;
      let bridgeState = "normal";
      if (blockedBridgeIds.has(b.id)) bridgeState = "blocked";
      else if (weakBridgeMap.has(b.id)) bridgeState = "weak_link";
      else if (seasonalBridgeMap.has(b.id)) bridgeState = "seasonal";

      bridgeFeatures.push({
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: [lon as number, lat as number],
        },
        properties: {
          feature_kind: "bridge",
          id: b.id,
          label: b.name || b.id,
          bridge_state: bridgeState,
          is_selected: isSelected,
        },
      });
    }

    // Active closure markers positioned at blocked segment midpoints
    const closureFeatures: GeoJSON.Feature[] = [];
    for (const c of graph.closures) {
      if (c.status !== "active") continue;
      for (const sid of c.blocks_segments) {
        const coords = segmentCoordsById.get(sid);
        const mid = coords ? segmentMidpoint(coords) : null;
        if (!mid) continue;
        closureFeatures.push({
          type: "Feature",
          geometry: {
            type: "Point",
            coordinates: mid,
          },
          properties: {
            feature_kind: "closure",
            id: c.id,
            segment_id: sid,
            label: `CLOSURE: ${c.id} (${sid})`,
            is_selected: selectedFeature?.kind === "closure" && selectedFeature.id === c.id,
          },
        });
      }
    }

    return {
      roadGeom: { type: "FeatureCollection", features: roadGeomFeatures } as GeoJSON.FeatureCollection,
      topoEdges: { type: "FeatureCollection", features: topoEdgeFeatures } as GeoJSON.FeatureCollection,
      places: { type: "FeatureCollection", features: placeFeatures } as GeoJSON.FeatureCollection,
      hospitals: { type: "FeatureCollection", features: hospitalFeatures } as GeoJSON.FeatureCollection,
      hospitalLinks: {
        type: "FeatureCollection",
        features: hospitalLinkFeatures,
      } as GeoJSON.FeatureCollection,
      bridges: { type: "FeatureCollection", features: bridgeFeatures } as GeoJSON.FeatureCollection,
      closures: { type: "FeatureCollection", features: closureFeatures } as GeoJSON.FeatureCollection,
    };
  }, [
    graph,
    cutoff,
    reroute,
    weakLinks,
    seasonalRisk,
    selectedFeature,
    showTopologyOverlay,
    placesById,
  ]);

  const fitToGraph = useCallback(() => {
    const map = mapRef.current;
    if (!map || !graph) return;
    const validPlaces = graph.places.filter((p) => validCoord(p.lon, p.lat));
    if (validPlaces.length === 0) {
      map.fitBounds(CORRIDOR_BOUNDS, { padding: 36, duration: 500 });
      return;
    }
    let minLon = Infinity;
    let maxLon = -Infinity;
    let minLat = Infinity;
    let maxLat = -Infinity;
    for (const p of validPlaces) {
      const lon = p.lon as number;
      const lat = p.lat as number;
      if (lon < minLon) minLon = lon;
      if (lon > maxLon) maxLon = lon;
      if (lat < minLat) minLat = lat;
      if (lat > maxLat) maxLat = lat;
    }
    if (Math.abs(maxLon - minLon) < 0.005) {
      minLon -= 0.01;
      maxLon += 0.01;
    }
    if (Math.abs(maxLat - minLat) < 0.005) {
      minLat -= 0.01;
      maxLat += 0.01;
    }
    map.fitBounds(
      [
        [minLon, minLat],
        [maxLon, maxLat],
      ],
      { padding: 48, maxZoom: 13, duration: 500 },
    );
  }, [graph]);

  // Initialize MapLibre instance
  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE_URL,
      bounds: CORRIDOR_BOUNDS,
      fitBoundsOptions: { padding: 32 },
    });
    mapRef.current = map;

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.on("error", (e) => {
      if (e.error?.message) {
        setProblem(e.error.message);
      }
    });

    map.on("load", () => {
      const empty: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
      map.addSource("segments-geom", { type: "geojson", data: empty });
      map.addSource("segments-topo", { type: "geojson", data: empty });
      map.addSource("hospital-links", { type: "geojson", data: empty });
      map.addSource("places", { type: "geojson", data: empty });
      map.addSource("bridges", { type: "geojson", data: empty });
      map.addSource("hospitals", { type: "geojson", data: empty });
      map.addSource("closures", { type: "geojson", data: empty });

      // 1. Stored road geometry layer (solid lines)
      map.addLayer({
        id: "segments-geom-line",
        type: "line",
        source: "segments-geom",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": [
            "match",
            ["get", "visual_state"],
            "blocked",
            "#c62828",
            "route",
            "#1565c0",
            "weak_link",
            "#ef6c00",
            "seasonal_high",
            "#6a1b9a",
            "seasonal_medium",
            "#9c27b0",
            "baseline_route",
            "#00838f",
            "nh29",
            "#263238",
            "#607d8b",
          ],
          "line-width": [
            "case",
            ["boolean", ["get", "is_selected"], false],
            7,
            [
              "match",
              ["get", "visual_state"],
              "blocked",
              5.5,
              "route",
              5,
              "weak_link",
              4.5,
              "seasonal_high",
              4.5,
              "seasonal_medium",
              4,
              "nh29",
              3.2,
              2.2,
            ],
          ],
          "line-opacity": 0.92,
        },
      });

      // 2. Schematic topological CONNECTED_BY edges (dashed lines)
      map.addLayer({
        id: "segments-topo-line",
        type: "line",
        source: "segments-topo",
        layout: { "line-cap": "butt", "line-join": "round" },
        paint: {
          "line-color": [
            "match",
            ["get", "visual_state"],
            "blocked",
            "#c62828",
            "route",
            "#1565c0",
            "weak_link",
            "#ef6c00",
            "seasonal_high",
            "#6a1b9a",
            "seasonal_medium",
            "#9c27b0",
            "baseline_route",
            "#00838f",
            "nh29",
            "#37474f",
            "#546e7a",
          ],
          "line-width": [
            "case",
            ["boolean", ["get", "is_selected"], false],
            6,
            [
              "match",
              ["get", "visual_state"],
              "blocked",
              4.5,
              "route",
              4.5,
              "weak_link",
              4,
              "seasonal_high",
              4,
              3,
            ],
          ],
          "line-dasharray": [2, 2],
          "line-opacity": 0.88,
        },
      });

      // 3. Hospital attachment links (when topology overlay is enabled)
      map.addLayer({
        id: "hospital-links-line",
        type: "line",
        source: "hospital-links",
        paint: {
          "line-color": "#00695c",
          "line-width": 1.5,
          "line-dasharray": [1, 2],
          "line-opacity": 0.7,
        },
      });

      // 4. Places (villages, towns, junctions)
      map.addLayer({
        id: "places-circle",
        type: "circle",
        source: "places",
        paint: {
          "circle-radius": [
            "case",
            ["boolean", ["get", "is_selected"], false],
            10,
            [
              "match",
              ["get", "place_type"],
              "town",
              8,
              "village",
              6.5,
              4.5,
            ],
          ],
          "circle-color": [
            "match",
            ["get", "reach_state"],
            "cut_off",
            "#d32f2f",
            "baseline_unreachable",
            "#78909c",
            "route_endpoint",
            "#1565c0",
            "weak_link",
            "#ef6c00",
            "reference",
            "#0d47a1",
            [
              "match",
              ["get", "place_type"],
              "town",
              "#1b5e20",
              "village",
              "#2e7d32",
              "#455a64",
            ],
          ],
          "circle-stroke-width": [
            "case",
            ["boolean", ["get", "is_selected"], false],
            3,
            ["==", ["get", "reach_state"], "cut_off"],
            2.5,
            1.5,
          ],
          "circle-stroke-color": "#ffffff",
        },
      });

      // 5. Bridges
      map.addLayer({
        id: "bridges-circle",
        type: "circle",
        source: "bridges",
        paint: {
          "circle-radius": [
            "case",
            ["boolean", ["get", "is_selected"], false],
            9,
            6.5,
          ],
          "circle-color": [
            "match",
            ["get", "bridge_state"],
            "blocked",
            "#c62828",
            "weak_link",
            "#ef6c00",
            "seasonal",
            "#6a1b9a",
            "#5d4037",
          ],
          "circle-stroke-width": 2,
          "circle-stroke-color": "#fff8e1",
        },
      });

      // 6. Hospitals
      map.addLayer({
        id: "hospitals-circle",
        type: "circle",
        source: "hospitals",
        paint: {
          "circle-radius": [
            "case",
            ["boolean", ["get", "is_selected"], false],
            10,
            7.5,
          ],
          "circle-color": [
            "match",
            ["get", "hosp_state"],
            "unreachable",
            "#b71c1c",
            "unattached",
            "#757575",
            "#00695c",
          ],
          "circle-stroke-width": 2.2,
          "circle-stroke-color": "#ffffff",
        },
      });

      // 7. Active Closures
      map.addLayer({
        id: "closures-circle",
        type: "circle",
        source: "closures",
        paint: {
          "circle-radius": 9,
          "circle-color": "#b71c1c",
          "circle-stroke-width": 3,
          "circle-stroke-color": "#ffeb3b",
        },
      });

      // Click handler across interactive layers
      map.on("click", (e) => {
        const features = map.queryRenderedFeatures(e.point, {
          layers: INTERACTIVE_LAYERS.filter((id) => Boolean(map.getLayer(id))),
        });
        if (!features.length) return;
        const top = features[0];
        const kind = top.properties?.feature_kind as SelectedFeature["kind"] | undefined;
        const id = top.properties?.id as string | undefined;
        if (kind && id) {
          onSelectRef.current({ kind, id });
        }
      });

      for (const layerId of INTERACTIVE_LAYERS) {
        map.on("mouseenter", layerId, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", layerId, () => {
          map.getCanvas().style.cursor = "";
        });
      }

      setMapReady(true);
    });

    return () => {
      mapRef.current = null;
      setMapReady(false);
      map.remove();
    };
  }, []);

  // Push updated GeoJSON data into MapLibre sources whenever backend data changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;

    const setSourceData = (name: string, data: GeoJSON.FeatureCollection) => {
      const src = map.getSource(name) as maplibregl.GeoJSONSource | undefined;
      if (src) src.setData(data);
    };

    setSourceData("segments-geom", geojsonData.roadGeom);
    setSourceData("segments-topo", geojsonData.topoEdges);
    setSourceData("hospital-links", geojsonData.hospitalLinks);
    setSourceData("places", geojsonData.places);
    setSourceData("bridges", geojsonData.bridges);
    setSourceData("hospitals", geojsonData.hospitals);
    setSourceData("closures", geojsonData.closures);
  }, [geojsonData, mapReady]);

  // Fit bounds when dataset first loads or switches
  useEffect(() => {
    if (!mapReady || !graph) return;
    const key = `${graph.dataset?.id ?? "none"}:${graph.counts.places}:${graph.counts.segments}`;
    if (lastDatasetKeyRef.current !== key) {
      lastDatasetKeyRef.current = key;
      fitToGraph();
    }
  }, [mapReady, graph, fitToGraph]);

  const missingGeometryCount = useMemo(() => {
    if (!graph) return 0;
    return graph.segments.filter((s) => !Array.isArray(s.geometry) || s.geometry.length < 2).length;
  }, [graph]);

  return (
    <div className="map-stage">
      <div
        ref={containerRef}
        className="map"
        aria-label="Interactive map of the Dimapur to Kohima NH29 corridor road graph"
      />

      {/* Map Toolbar */}
      <div className="map-toolbar" role="toolbar" aria-label="Map overlay controls">
        <button type="button" className="btn-chip" onClick={fitToGraph}>
          Fit to Graph
        </button>
        <button
          type="button"
          className={`btn-chip ${showTopologyOverlay ? "active" : ""}`}
          onClick={onToggleTopologyOverlay}
          aria-pressed={showTopologyOverlay}
        >
          {showTopologyOverlay ? "Hide Graph Topology" : "Show Graph Topology"}
        </button>
        <button
          type="button"
          className="btn-chip"
          onClick={() => setLegendOpen((v) => !v)}
          aria-expanded={legendOpen}
        >
          {legendOpen ? "Hide Legend" : "Show Legend"}
        </button>
      </div>

      {/* Empty or Synthetic Dataset Notice */}
      {!graph || graph.counts.segments === 0 ? (
        <aside className="map-banner warn" role="status">
          <strong>No corridor graph segments loaded</strong>
          <span>
            Seed the graph first (<code>python data\seed.py</code>) or start the backend with{" "}
            <code>LANDSLIDE_USE_FIXTURE=1</code>.
          </span>
        </aside>
      ) : graph.dataset?.kind?.toLowerCase().includes("synthetic") ? (
        <aside className="map-banner warn" role="status">
          <strong>Synthetic Test Fixture Active</strong>
          <span>
            Showing deterministic test topology ({graph.counts.places} places, {graph.counts.segments}{" "}
            segments), not verified Dimapur–Kohima road geometry.
          </span>
        </aside>
      ) : missingGeometryCount > 0 ? (
        <aside className="map-banner info" role="status">
          <span>
            {missingGeometryCount} segment(s) lack stored road geometry and are rendered as dashed
            schematic <code>CONNECTED_BY</code> graph connections.
          </span>
        </aside>
      ) : null}

      {/* Visual Legend */}
      {legendOpen && (
        <aside className="map-legend" aria-label="Map symbol and color legend">
          <div className="legend-title">Corridor Graph Legend</div>
          <div className="legend-grid">
            <div className="legend-item">
              <span className="swatch-line nh29" />
              <span>NH29 road segment (open)</span>
            </div>
            <div className="legend-item">
              <span className="swatch-line secondary" />
              <span>Secondary/spur segment (open)</span>
            </div>
            <div className="legend-item">
              <span className="swatch-line schematic" />
              <span>Schematic graph edge (dashed)</span>
            </div>
            <div className="legend-item">
              <span className="swatch-line blocked" />
              <span>Blocked segment / Active closure</span>
            </div>
            <div className="legend-item">
              <span className="swatch-line route" />
              <span>Calculated alternate route</span>
            </div>
            <div className="legend-item">
              <span className="swatch-line weak" />
              <span>Centrality weak link</span>
            </div>
            <div className="legend-item">
              <span className="swatch-line seasonal" />
              <span>Elevated seasonal risk</span>
            </div>
            <div className="legend-item">
              <span className="swatch-dot ref" />
              <span>Reference town (Dimapur / Kohima)</span>
            </div>
            <div className="legend-item">
              <span className="swatch-dot reach" />
              <span>Reachable settlement (village/town)</span>
            </div>
            <div className="legend-item">
              <span className="swatch-dot cutoff" />
              <span>Newly unreachable settlement</span>
            </div>
            <div className="legend-item">
              <span className="swatch-dot junction" />
              <span>Road junction</span>
            </div>
            <div className="legend-item">
              <span className="swatch-dot hosp-ok" />
              <span>Hospital (reachable / unattached)</span>
            </div>
            <div className="legend-item">
              <span className="swatch-dot hosp-bad" />
              <span>Hospital (newly unreachable)</span>
            </div>
            <div className="legend-item">
              <span className="swatch-dot bridge" />
              <span>Bridge node</span>
            </div>
          </div>
          <div className="legend-hint">
            Click any road segment, settlement, bridge, hospital, or closure marker to inspect its
            provenance and calculations in the right pane.
          </div>
        </aside>
      )}

      {problem && (
        <p className="map-problem" role="alert">
          Basemap notice: {problem}
        </p>
      )}
    </div>
  );
}
