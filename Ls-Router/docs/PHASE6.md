# Phase 6 — Three-Pane Interactive Frontend

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

## 1. Overview & Three-Pane Layout

Phase 6 implements the interactive React + TypeScript + MapLibre GL JS interface in [`frontend/src/`](../frontend/src/). Every user interaction invokes the authoritative Phase 5 FastAPI backend (`/api/*`)—the frontend never fabricates closures, routes, reachability counts, or risk scores in local state.

```
+---------------------------------------------------------------------------------------------------+
| Header: Landslide Router | Corridor Entity Counts | Reload Graph                                  |
| Persistent Safety Disclaimer Banner                                                               |
+---------------------------+-------------------------------------------+---------------------------+
| LEFT PANE                 | CENTER PANE                               | RIGHT PANE                |
| Event & Routing Controls  | Interactive MapLibre GL JS Map            | "Why?" Evidence & Details |
|                           |                                           |                           |
| 1. Simulate Landslide     | - Road segments (solid OSM geometry vs    | - Selected Feature        |
|    (POST /api/closure)    |   dashed schematic CONNECTED_BY edges)    |   Provenance & Coords     |
| 2. Active Closures        | - Villages, towns, junctions              | - Closure Reachability &  |
|    - Clear & Audit        | - Hospitals & attachment links            |   Hospital Access Table   |
|    - Draft SMS Alerts     | - Bridges & active closure markers        | - Route & Travel-Time     |
| 3. Inspect Alternate      | - Blocked segments (red), alternate       |   Contributions           |
|    Routes (/api/reroute)  |   routes (blue), weak links (amber),      | - Weak Links & Seasonal   |
| 4. Weak Links, July/Aug   |   seasonal risk (purple)                  |   Risk Rankings           |
|    Seasonal Risk, Source  | - Collapsible Corridor Graph Legend       | - Dataset Confidence &    |
| 5. Agent & Tool Event Log | - Fit to Graph & Topology Overlay toggles |   Executed Cypher Queries |
+---------------------------+-------------------------------------------+---------------------------+
| Footer: Live Backend & FalkorDB Health Status (/api/health)                                       |
+---------------------------------------------------------------------------------------------------+
```

---

## 2. Map Rendering & Geometry Honesty

- **Surveyed Road Geometry vs. Schematic Topology**:
  - Road segments with stored GeoJSON coordinates (`segment.geometry !== null`) render on the `segments-geom-line` layer as solid lines.
  - Road segments without stored road geometry (`segment.geometry === null`), or when the user enables **Show Graph Topology**, render on the `segments-topo-line` layer as **dashed lines** (`line-dasharray: [2, 2]`) so a schematic `CONNECTED_BY` edge is never misrepresented as surveyed road geometry.
- **Closure & Routing Highlights**:
  - Applying a closure highlights the blocked segment in red (`#c62828`), places an active closure marker on the segment, colors newly unreachable settlements in red (`#d32f2f`) while keeping baseline-unreachable islands distinct (`#78909c`), marks unreachable hospitals in crimson (`#b71c1c`), and draws the calculated minimum-travel-time alternate route in blue (`#1565c0`).

---

## 3. Interactive Feature & Provenance Inspection

Clicking any road segment, settlement, junction, hospital, bridge, or closure marker on the map (or in any right-pane table) populates the **Selected Feature Inspector** with:
- Name, ID, and node/relationship label (`Place`, `RoadSegment`, `Hospital`, `Bridge`, `ClosureEvent`)
- Coordinates (`lat, lon`) or endpoints (`from_place_id → to_place_id`)
- Full provenance (`source_url`, `source_id`, `retrieved_at`, `last_verified`, `confidence`, `data_status`)
- Active closure status, cut-off baseline evidence path, route travel-time contribution, betweenness centrality score, and seasonal risk rank
- Exact executed Cypher statements and algorithm implementation notes (`NetworkX` fallback rationale)

---

## 4. Windows Command Prompt Verification

```bat
:: Window 1 — Start FastAPI backend (with live FalkorDB or offline fixture):
cd Ls-Router\backend
.venv\Scripts\activate.bat
set LANDSLIDE_USE_FIXTURE=1
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

:: Window 2 — Type-check, build, and start Vite frontend:
cd Ls-Router\frontend
npm run build
npm run dev
```

Open `http://localhost:5173` in a browser to verify the three-pane UI.
