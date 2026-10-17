# Judge's Guide — Landslide Router (Dimapur–Kohima NH29 Corridor)

**Track**: Graph Hacks — Track 1: Agents That Act on Connected Data (FalkorDB)

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

---

## 1. What Makes This Project Real (Zero Hardcoded Demo Answers)

Every number you see in **Landslide Router**—whether `3` isolated settlements (`Kezo Town`, `Kikruma`, `Chakhabama`), a `+90.03 min` detour (`125.2 min` baseline → `215.23 min` detour), or `0.458767` shortest-path betweenness centrality—is computed live from a **2,019-node, 4,177-relationship OpenStreetMap + historical Nagaland property graph** stored in **FalkorDB**.

- **Real Corridor Data (`data/cache/normalized.json` & FalkorDB `landslide` graph)**:
  - `814` `Place` nodes (`76` villages, `4` towns including Dimapur & Kohima, `734` road junctions)
  - `1,045` `RoadSegment` nodes (`100` NH29 trunk segments covering `189.5 km`)
  - `1,935` directed `:CONNECTED_BY` relationships weighted by `travel_time_min`
  - `126` `Bridge` nodes linked via `(:Bridge)-[:ON]->(:RoadSegment)`
  - `24` `Hospital` nodes linked via `(:Hospital)-[:LOCATED_AT]->(:Place)`
  - `7` `ClosureEvent` nodes from curated historical reports (`data_status="observed"`) + runtime simulated closures (`data_status="simulated"`)
- **Real MCP Tool Execution (`mcp/server.py` & `backend/app/mcp_bridge/`)**:
  - All 12 graph tools are exposed and invoked over the official Python Model Context Protocol (`mcp`) SDK (`tools/list` and `tools/call`).
- **Cryptographic Baseline Auditing (`audit_closure`)**:
  - Every `RoadSegment` and the `Dataset` node carry SHA-256 baseline fingerprints (`baseline_fp` and `network_fingerprint`) so resolving a closure is mathematically verified to restore the baseline graph.

---

## 2. Three Ways to Run & Verify in Under 3 Minutes (Windows Command Prompt)

### Quickest Verification: Run the Automated 12-Step Demo Script (~21 seconds)
From the repo root with `backend\.venv` active:

```bat
backend\.venv\Scripts\activate.bat

:: Run against live FalkorDB (Cloud or local Docker configured in .env):
python scripts\run_demo.py

:: Or run with zero Docker / zero cloud setup against the cached 2,019-node OSM NH29 extract:
python scripts\run_demo.py --dataset data\cache\normalized.json
```

### Full Interactive Three-Pane MapLibre Workbench
Open two Windows Command Prompt windows:

**Window 1 — FastAPI Backend**:
```bat
cd backend
.venv\Scripts\activate.bat
:: If not using FalkorDB Cloud/Docker, uncomment the next line to use the cached 2,019-node OSM dataset:
:: set LANDSLIDE_DATASET_PATH=..\data\cache\normalized.json
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

**Window 2 — React + MapLibre Frontend**:
```bat
cd frontend
npm run dev
```

Open **`http://localhost:5173`** in your browser.

---

## 3. Guided 3-Minute Interactive Demo Walkthrough

1. **Inspect Sourced Corridor Network (0:00–0:20)**:
   - Observe the NH29 trunk (slate), secondary hill connectors, bridges, hospitals, and settlements between **Dimapur** and **Kohima**.
   - Click any road segment or settlement on the map to inspect its OpenStreetMap `source_url`, `source_id`, `confidence` badge (`verified` vs `unverified`), and `data_status` (`observed` vs `derived`) in the right-hand **"Why?"** panel.
2. **Test Ambiguity Gating (0:20–0:50)**:
   - In the left pane, enter `"Landslide at NH29 near kilometre 42."` and click **Report / Simulate Closure** (or click the **Simulate Landslide (NH29 km 42)** quick button).
   - Notice that the system **refuses to guess**: it returns `clarification_required` and lists the top candidate NH29 segments ranked by cumulative distance from Dimapur.
3. **Confirm Trunk Closure & Inspect Detour Comparison (0:50–1:30)**:
   - Confirm candidate segment `seg-569069510-569069527-1353575333` (or click **Confirm & Block Segment**).
   - The blocked segment turns red on the map, the avoided baseline route renders as a dashed line, and the active detour draws in blue.
   - In the **"Why?"** panel, inspect the **Alternative-Route Comparison** card: baseline `125.2 min` (`70.101 km`) vs detour `215.23 min` (`100.511 km`, **`+90.03 min`**).
4. **Simulate Settlement Cut-Off & Export Draft Alerts (1:30–2:10)**:
   - Enter `"Landslide on NH29 between Kezo Town and Chakhabama"` and confirm segment `seg-2469699767-6286548534-239157848`.
   - Observe that **`3` settlements** (`Kezo Town`, `Kikruma`, `Chakhabama`) are newly cut off from Dimapur, hospital reachability is recalculated over the road graph, and **`3` synthetic SMS draft alerts** (`status="draft"`, `sent=False`) are generated.
   - Click **Download JSON** or **Download .TXT** to export the draft alert sheet.
5. **Inspect Weak Links, July Monsoon Risk & Timeline Replay (2:10–2:40)**:
   - Click **Show Weak Links** to highlight the top 5 betweenness-centrality bottlenecks (`seg-569069510-569069527-1353575333`, `betweenness = 0.458767`).
   - Click **July (Monsoon)** to inspect seasonal risk rankings and `OBSERVED` vs `SIMULATED` history badges.
   - Click any entry in the **Closure Event Timeline** (`◀ Prev` / `Next ▶`) to non-destructively replay a historical event.
6. **Resolve Closure & Cryptographic Baseline Audit (2:40–3:00)**:
   - Click **Resolve** (or run `python scripts\reset_demo.py`) and verify in the **"Why?"** panel that `returned_to_baseline: true` with `0` fingerprint discrepancies.

---

## 4. Automated Test Suite

Run the full backend test suite (`120+` unit and API tests) and frontend production build:

```bat
cd backend
.\.venv\Scripts\pytest.exe -q

cd ..\frontend
npm.cmd run build
```

For full architecture diagrams, verified claims checklist, blog post draft, and pitch deck outline, see:
- [`docs/PHASE9.md`](PHASE9.md)
- [`docs/BLOG_POST_DRAFT.md`](BLOG_POST_DRAFT.md)
- [`docs/PITCH_DECK_OUTLINE.md`](PITCH_DECK_OUTLINE.md)
