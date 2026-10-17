# Phase 5 — FastAPI HTTP Backend

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

## 1. Overview & Architecture

Phase 5 exposes the authoritative HTTP API for **Landslide Router** in [`backend/app/main.py`](../backend/app/main.py). Every endpoint executes live graph queries through [`FalkorStore`](../backend/app/graph/store.py) against FalkorDB (or an explicitly configured offline normalized dataset), runs the Phase 2–3 graph algorithms and location resolver, and returns structured JSON payloads containing the executed Cypher statements, parameters, provenance metadata, warnings, and the safety disclaimer.

```
+-------------------------------------------------------------------------------+
|                        Frontend (Vite / React / MapLibre)                     |
|                       or HTTP Client / OpenAPI (/docs)                        |
+-------------------------------------------------------------------------------+
                                       |
                        HTTP JSON (CORS enabled for local dev)
                                       v
+-------------------------------------------------------------------------------+
|                       FastAPI App (backend/app/main.py)                       |
|   - Pydantic request/query validation & domain exception handlers             |
|   - Automatic personal/sensitive token redaction (`backend/app/privacy.py`)   |
|   - Authoritative endpoints: /health, /graph, /closure, /cutoff/{event_id},   |
|     /reroute, /weak-links, /seasonal-risk, /alerts/{event_id},                |
|     /resolve/{event_id}                                                       |
+-------------------------------------------------------------------------------+
                                       |
                                       v
+-------------------------------------------------------------------------------+
|         LocationResolver (`app/agent/resolver.py`) + Toolbox (`app/graph/`)   |
|   - Ambiguity gating on `POST /closure` (never silently snaps candidates)     |
|   - Directed reachability, Dijkstra routing, betweenness centrality,          |
|     seasonal risk ranking, synthetic draft alerts, and baseline audit         |
+-------------------------------------------------------------------------------+
                                       |
                                       v
+-------------------------------------------------------------------------------+
|                    FalkorStore (`backend/app/graph/store.py`)                 |
|   - Logs every Cypher statement and parameter map executed per HTTP request   |
+-------------------------------------------------------------------------------+
```

---

## 2. HTTP Endpoints

| Method | Path | Summary | Key Parameters / Body |
|---|---|---|---|
| `GET` | `/health` | Check API and FalkorDB connectivity | None (`200 OK` when connected, `503 Service Unavailable` when degraded) |
| `GET` | `/graph` | Return corridor graph with places, road segments, hospitals, bridges, connections, closures, seasons, provenance, and GeoJSON geometry | Query: `include_geometry: bool = true` |
| `POST` | `/closure` | Resolve a natural-language incident description (and optional coordinates) and apply a simulated closure when unambiguous | Body: `description`, optional `lat`, `lon`, `target_segment_id`, `target_bridge_id`, `cause`, `max_candidates`, `remember_in_history` |
| `GET` | `/cutoff/{event_id}` | Return newly cut-off settlements from Dimapur, component splits, and hospital reachability impact | Path: `event_id`; Query: optional `threshold_min: float` |
| `GET` | `/reroute` | Compute fastest valid detour route (`reroute`) and per-segment weight explanation (`explain_path`) | Query: `from` (or `from_place_id`), `to` (or `to_place_id`), optional `avoid_event_id` |
| `GET` | `/weak-links` | Rank top corridor weak links (segments, bridges, and places) by shortest-path betweenness centrality | Query: optional `corridor_id`, `top_k: int = 5` |
| `GET` | `/seasonal-risk` | Return documented monsoon seasonal risk ranking for a given month | Query: `month` (`1`–`12` or month name such as `july`) |
| `POST` | `/alerts/{event_id}` | Generate synthetic SMS draft alerts (`status="draft"`, `sent=false`) for cut-off settlements | Path: `event_id` |
| `POST` | `/resolve/{event_id}` | Resolve a simulated closure (`clear_closure`) and verify return to baseline (`audit_closure`) | Path: `event_id`; Query/Body: optional `remember_in_history: bool = false` |

---

## 3. Ambiguity Gating on `POST /closure`

In accordance with Section 10 of the specification:
- `POST /closure` always runs `LocationResolver.resolve_location` first.
- If the incident description uses an unverified highway chainage/kilometre marker (e.g. `"Landslide near NH29 kilometre 42"`), coordinate-only proximity (`lat`/`lon` without a single confirmed road segment), or names a junction where multiple segments meet, `POST /closure` **does not mutate the graph**. It returns:
  - `status: "clarification_required"`
  - `requires_clarification: true`
  - `created: false`
  - `resolution.candidate_segments` and `resolution.clarification_prompt`
- Once the caller supplies `target_segment_id` (or `target_bridge_id`) or provides an unambiguous description that matches a single corridor segment, `POST /closure` executes `Toolbox.apply_closure` and returns `status: "applied"`, `created: true`, `event_id`, `closure`, `resolution`, `cypher`, and `graph`.

---

## 4. Error Status Codes & Envelope

All error responses use a consistent JSON envelope with `error`, `detail`, and `disclaimer`:

| HTTP Status | Error Code | Exception Type | Cause |
|---|---|---|---|
| `404 Not Found` | `not_found` | `ToolNotFoundError` | Unknown `place_id`, `segment_id`, `bridge_id`, `event_id`, or `corridor_id` |
| `422 Unprocessable Entity` | `validation_error` / `invalid_input` | `RequestValidationError` / `ToolInputError` | Invalid request body, missing required query parameter (`from`/`to`), unpaired `lat`/`lon`, or out-of-range `month`/`top_k` |
| `409 Conflict` | `invalid_state` | `ToolStateError` | Attempting to clear/resolve a historical `observed` event (`obs-...`) or graph state conflict |
| `503 Service Unavailable` | `store_error` | `StoreError` | FalkorDB is unreachable or a Cypher query failed |

---

## 5. Windows Command Prompt Verification

From `backend\` with `.venv` active:

```bat
cd backend
.venv\Scripts\activate.bat

:: Run the FastAPI server (against live FalkorDB, or set LANDSLIDE_USE_FIXTURE=1 for offline testing):
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

:: Run the API unit test suite:
python -m pytest tests/test_api.py -v
```

