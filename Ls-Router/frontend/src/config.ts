// Keyless OpenFreeMap vector style (OpenStreetMap data, attribution is added by the style).
// Override with VITE_MAP_STYLE_URL in .env. The public service has no availability guarantee.
export const MAP_STYLE_URL: string =
  import.meta.env.VITE_MAP_STYLE_URL || "https://tiles.openfreemap.org/styles/liberty";

// Viewport framing only: approximate [west, south] / [east, north] around Dimapur and Kohima.
// This is NOT graph data and is not a verified extraction boundary (that comes in Phase 1).
export const CORRIDOR_BOUNDS: [[number, number], [number, number]] = [
  [93.55, 25.5],
  [94.3, 26.05],
];

export const SAFETY_DISCLAIMER =
  "Planning aid, not emergency dispatch. Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.";
