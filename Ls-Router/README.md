# Landslide Router

Graph-backed landslide-routing prototype for the Dimapur–Kohima corridor (NH29, Nagaland, India). Graph Hacks, Track 1 (FalkorDB).

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

**Current state: Phase 4 (Model Context Protocol server).** Phase 0 scaffold, the Phase 1 OpenStreetMap fetch/normalize/seed pipeline, the Phase 2 eleven graph tools (`backend/app/graph`), the Phase 3 natural-language location resolver + agent loop (`backend/app/agent`), and the Phase 4 MCP server + client handoff bridge (`mcp/` and `backend/app/mcp_bridge`). There is no HTTP tool API yet, and the map still shows no project data (Phase 6).

## Prerequisites (Windows)

- Docker Desktop (running)
- Python 3.11 or newer (`py -3 --version`)
- Node.js 20 LTS or newer (`node --version`)

Low on RAM? Docker Desktop's WSL 2 backend can take a lot. Cap it in `%UserProfile%\.wslconfig` (`[wsl2]` then `memory=2GB`), then run `wsl --shutdown`. The FalkorDB container is also capped at 1 GB (`FALKORDB_MEM_LIMIT`).

## Setup and start (Command Prompt)

Run each block from the repo root, in its own window where noted.

```bat
copy .env.example .env
docker compose up -d
```

Backend (window 2):

```bat
cd backend
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Frontend (window 3):

```bat
cd frontend
npm install
npm run dev
```

## Verify

| Check | Command / action | Expected |
|---|---|---|
| FalkorDB is up | `docker compose exec falkordb redis-cli ping` | `PONG` |
| Graph query works | `docker compose exec falkordb redis-cli GRAPH.QUERY landslide "RETURN 1"` | a result containing `1` |
| API health | `curl http://127.0.0.1:8000/health` | JSON with `"status":"ok"`, `falkordb.query_ok: true` (HTTP 503 and `"degraded"` if FalkorDB is down) |
| API docs | open http://127.0.0.1:8000/docs | Swagger UI |
| Map | open http://localhost:5173 | Basemap framed on Dimapur–Kohima, "No corridor data loaded" notice, green backend status line |
| FalkorDB Browser (optional) | open http://localhost:3000 | Browser UI; connect to host `localhost`, port `6379` |

Backend tests (venv active, from `backend`): `python -m pytest -v`. The `integration` tests need the Docker container and skip themselves if FalkorDB is unreachable.
Frontend type-check and build (from `frontend`): `npm run build`.

## Phase 1: build and seed the graph

Needs the backend venv (`falkordb` package) and FalkorDB running. Details, rules and assumptions: [data/README.md](data/README.md).
Set `OVERPASS_CONTACT` in `.env` first. Run from the repo root:

```bat
backend\.venv\Scripts\activate.bat
python data\fetch_osm.py fetch
python data\fetch_osm.py normalize
python data\seed.py --dry-run
python data\seed.py
python data\seed.py
```

- `fetch` talks to the Overpass API once and caches raw JSON in `data\cache\raw` (re-running uses the cache). It prints the `ref` values it found; check that the NH29 road is among them.
- `normalize` is offline. Read `data\cache\report.md`: it lists what was skipped, what could not be attached to the road network, what is unknown, and any validation errors.
- `seed` loads the graph and then checks it. Run it twice: the second run must report `0 node(s) and 0 relationship(s)` created.
- Inspect in FalkorDB Browser (http://localhost:3000) with the queries in `data\queries.cypher`.
- To rebuild from scratch: `python data\seed.py --reset --yes` (deletes everything in the graph).

Tests (no database or network needed, except `integration` tests that skip themselves without FalkorDB):

```bat
cd backend
python -m pytest -v
```

## Phase 2: the graph tools

Design, semantics and limits: [docs/PHASE2.md](docs/PHASE2.md). **After updating from Phase 1, re-run `normalize` and `seed`** (they now store baseline fingerprints that `audit_closure` needs), then:

```bat
backend\.venv\Scripts\activate.bat
cd backend
pip install -r requirements.txt
python -m app.graph.cli ids
python -m app.graph.cli apply_closure "{\"segment_id\": \"<a segment id from 'ids'>\", \"description\": \"test landslide\"}"
python -m app.graph.cli find_cut_off_places "{\"closure_event_id\": \"<sim-... from the previous output>\"}"
python -m app.graph.cli clear_closure "{\"event_id\": \"<same id>\"}"
python -m app.graph.cli audit_closure "{\"event_id\": \"<same id>\"}"
```

`python -m app.graph.cli probe_capabilities` lists the `algo.*` procedures your FalkorDB build actually has. Tests: `python -m pytest -v` (the `integration`-marked ones need FalkorDB and skip themselves without it).

## Phase 3: the location resolver and graph-backed agent

Design, resolution ladder, ambiguity gating, and provenance schema: [docs/PHASE3.md](docs/PHASE3.md).

Run from `backend\` with the virtualenv active:

```bat
cd backend
.venv\Scripts\activate.bat
python -m app.agent.cli --fixture --demo
python -m app.agent.cli --fixture "Landslide near NH29 kilometre 42"
python -m app.agent.cli --fixture --confirm-id seg:s2 "Landslide near NH29 kilometre 42"
python -m app.agent.cli --fixture --json "Rockfall on Chathe Bridge"
```

## Phase 4: Model Context Protocol (MCP) server

Architecture, tool schemas, and handoff logging: [docs/PHASE4.md](docs/PHASE4.md) and [mcp/README.md](mcp/README.md).

Run from `backend\` with the virtualenv active:

```bat
cd backend
.venv\Scripts\activate.bat

:: 1. Discover all 12 MCP tools over stdio child process:
python -m app.mcp_bridge.cli --fixture --transport stdio list-tools

:: 2. Invoke a single tool through MCP ClientSession:
python -m app.mcp_bridge.cli --fixture --transport stdio call find_weak_links "{\"corridor_id\": \"corr-synthetic\", \"top_k\": 3}"

:: 3. Run the end-to-end multi-step demonstration through MCP:
python -m app.mcp_bridge.cli --fixture --transport stdio demo
```

When FalkorDB is running and seeded (or using `--dataset ..\data\cache\normalized_corridor.json` offline):

```bat
python -m app.mcp_bridge.cli demo
python -m app.agent.cli --mcp-transport stdio "Landslide near NH29 kilometre 42"
```

## Stop

Press Ctrl+C in the backend and frontend windows, then from the repo root:

```bat
docker compose down
```

Graph data lives in the `falkordb_data` volume and survives `down`. `docker compose down -v` deletes it.

## Basemap and attribution

The map uses the keyless [OpenFreeMap](https://openfreemap.org) "liberty" vector style, which is built from OpenStreetMap data. MapLibre shows the required attribution automatically; do not hide it. The public OpenFreeMap service is best-effort with no availability guarantee. To switch, set `VITE_MAP_STYLE_URL` in `.env` to any MapLibre style URL you are licensed to use (self-hosted, or a provider with an API key; never commit keys). MapLibre GL JS itself supplies no tiles.

The basemap is general context. It is **not** the project's road graph and must not be read as verified NH29 data.

## Layout

```
backend/    FastAPI app (app/), tests/, requirements.txt
frontend/   Vite + React + TypeScript + MapLibre GL JS
data/       Phase 1: fetch_osm.py, osm_normalize.py, seed.py, queries.cypher, curated/ (see data/README.md)
mcp/        reserved for Phase 4
docker-compose.yml   FalkorDB (falkordb/falkordb, includes Browser UI)
```

## Phase 0 notes

- `/api/*` is proxied by Vite to `127.0.0.1:8000`, so no CORS setup is needed in development.
- No password is set on FalkorDB; both ports are bound to `127.0.0.1` only.
- `falkordb/falkordb:latest` is unpinned. Record the tested version (`docker image inspect falkordb/falkordb:latest`) and pin it in `.env` before the final demo.
- Python and npm dependencies use lower-bound versions; pin after a working install.
- The health check's `RETURN 1` creates an empty graph named `landslide` if it does not exist.
