# Phase 7 — Historical Memory and Seasonal Risk

Phase 7 implements persistent closure memory across application restarts, duplicate-request protection for historical failure counts, visible `SIMULATED` vs `OBSERVED` history labeling, and an explicit seasonal risk scoring formula with non-predictive advisories.

---

## 1. Persistent Closure Memory Across Restarts

- **Live FalkorDB mode**: `ClosureEvent`, `BLOCKS`, `FAILED_IN`, `Alert`, `TARGETS`, and `ROUTED_VIA` records persist natively inside the FalkorDB Docker container (`falkordb_data` volume), and `seed.py` preserves runtime nodes unless `--reset` is passed.
- **Offline fallback mode (`InMemoryGraph`)**: [`InMemoryGraph`](../backend/app/graph/in_memory.py) now supports an optional `history_path` file (configured via `LANDSLIDE_HISTORY_FILE`, defaulting to `data/cache/runtime_history.json` when unset in [`db.py`](../backend/app/db.py)).
  - Every write operation (`_create`, `_resolve`, `_remember`, `_record_failure`, `_upsert_alert`, `_clear_routes`, `_link_route`) atomically snapshots runtime `ClosureEvent`, `Alert`, and `FAILED_IN` state via `save_history_file()`.
  - When a fresh `InMemoryGraph` boots and runs `seed_graph(...)`, `load_history_file()` restores all persisted closures, resolved timestamps, remembered markers, bridge failure edges, and draft alerts.

---

## 2. Duplicate Request Protection (Idempotency)

- **Closure creation (`Toolbox.apply_closure`)**:
  - Before creating a new `ClosureEvent`, `apply_closure` checks whether an existing event on the same target already matches `(cause, description, data_status, started_at[:10])` (or is currently `active` with the same cause and description when `started_at` is omitted).
  - If a duplicate request is detected, `apply_closure` returns the existing `event_id` with `deduplicated: True` instead of inflating historical failure counts.
- **Remembering closures (`Toolbox.remember_closure`)**:
  - `remember_closure(event_id, year=...)` uses Cypher `MERGE (b)-[f:FAILED_IN {event_id: $event_id}]->(c)` (and an equivalent `(bridge_id, event_id)` uniqueness check in `InMemoryGraph._record_failure`).
  - Calling `remember_closure` repeatedly for the same `event_id` updates metadata in place (`created_new_edge=0`) without creating duplicate `FAILED_IN` relationships.

---

## 3. Visible Labeling of Simulated vs Observed History

- Every historical closure and bridge failure record carries both `data_status` (`simulated` | `observed`) and a human-readable `history_label`:
  - `"SIMULATED (demonstration scenario)"` when `data_status == "simulated"`
  - `"OBSERVED (historical report)"` when `data_status == "observed"`
- Exposed across:
  - `Toolbox.seasonal_risk(month)` (`items[].history_label`, `bridge_failures_in_season[].history_label`)
  - `GET /graph` (`closures[].history_label`, `bridges[].failures[].history_label`)
  - `GET /history` (`closures` and `bridge_failures`)
  - Frontend **Seasonal Risk** table and **Feature Inspector** badges (`SIMULATED` vs `OBSERVED`).

---

## 4. Seasonal Risk Scoring Rule & Non-Predictive Language

`Toolbox.seasonal_risk(month)` ranks every `RoadSegment` and `Bridge` during the active `Season` (`monsoon`, months 6–9) or `off_monsoon` using a documented lexicographic priority and composite score:

$$\text{composite\_score} = 100 \cdot \text{risk\_tier} + 25 \cdot \text{obs} + 10 \cdot \text{sim} + 5 \cdot \text{rem} + 10 \cdot \text{obs\_bf} + 5 \cdot \text{sim\_bf}$$

Where:
- `risk_tier`: `high = 3`, `medium = 2`, `low = 1`, `unknown = 0` (halved outside monsoon months).
- `obs`: count of distinct `observed` `ClosureEvent`s in the target season blocking the road segment or bridge.
- `sim`: count of distinct `simulated` `ClosureEvent`s in the target season blocking the road segment or bridge.
- `rem`: count of distinct in-season `ClosureEvent`s explicitly archived via `remember_closure`.
- `obs_bf` / `sim_bf`: count of distinct in-season `FAILED_IN` bridge relationships (`observed` vs `simulated`).

### Non-Predictive Advisory Rule
All tool outputs, agent claims (`seasonal_risk_evidence`), and UI summaries follow strict non-predictive language:
- Uses phrasing such as *"Elevated modeled risk during monsoon; historical pattern warrants review"* or *"Baseline modeled risk; monitor conditions during heavy rainfall."*
- Never claims that a bridge or road segment **will fail** in a future month.

---

## 5. Verification

Run from `backend/`:

```cmd
.\.venv\Scripts\pytest.exe -q
```

And from `frontend/`:

```cmd
npm.cmd run build
```

