import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { CORRIDOR_BOUNDS, MAP_STYLE_URL } from "./config";

export default function MapView() {
  const container = useRef<HTMLDivElement>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const map = new maplibregl.Map({
      container: container.current,
      style: MAP_STYLE_URL,
      bounds: CORRIDOR_BOUNDS,
      fitBoundsOptions: { padding: 24 },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.on("error", (e) => setProblem(e.error?.message ?? "The basemap could not be loaded."));
    return () => map.remove();
  }, []);

  return (
    <>
      <div ref={container} className="map" aria-label="Basemap of the Dimapur to Kohima area" />
      {problem && (
        <p className="map-problem" role="alert">
          Basemap problem: {problem}
        </p>
      )}
    </>
  );
}
