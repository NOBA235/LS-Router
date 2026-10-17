# Phase 8 — Polish (Confidence Badges, Data-Source Inspection, Timeline Replay, Alert Export & Route Comparison)

Phase 8 polishes the **Landslide Router** (Dimapur–Kohima NH29 corridor, Nagaland, India) operator workbench with explicit provenance/confidence badges across every panel, a searchable data-source inspector, a chronological closure timeline with non-destructive event replay, draft alert-plan exports (JSON and plain text), side-by-side alternative-route comparison, responsive mobile pane navigation, and accessibility enhancements.

---

## 1. Confidence & Provenance Badges

Every feature inspector, route comparison card, segment contribution table, cut-off settlement row, hospital row, timeline entry, and draft alert displays explicit badges:

- **Verification status**:
  - `verified` — backed by curated historical press/NSDMA/BRO reports or corridor survey data.
  - `unverified` — schematic geometry, operator-reported, or simulated scenario requiring field confirmation.
- **Provenance & lifecycle status**:
  - `observed` — historical closure event recorded in the seed dataset.
  - `derived` — computed deterministically by the graph reasoning engine (`ExplainPath`, `FindCutOffPlaces`, `FindUnreachableHospitals`).
  - `simulated` — scenario closure created interactively by the operator.
  - `draft` — operator-review alert (`status="draft"`, `sent=False`).

---

## 2. Data-Source & Provenance Inspection

The **Data-Source & Provenance Inspector** in [`WhyPanel.tsx`](../frontend/src/WhyPanel.tsx) indexes all corridor entities (`Place`, `RoadSegment`, `Junction`, `Bridge`, `Hospital`, `ClosureEvent`) directly from the loaded snapshot and active/replayed events:

- **Searchable & filterable**: Filter by free-text search (`id`, `name`, `source`), entity type (`place`, `road_segment`, `junction`, `bridge`, `hospital`, `closure_event`), and verification status (`verified` vs `unverified`).
- **Click-to-focus**: Selecting any row highlights the entity in the inspector and displays its exact `source`, `verified` flag, and `geometry_precision` (`curated_polyline` vs `schematic`).

---

## 3. Closure Event Timeline & Non-Destructive Replay

- **`GET /history`**: Returns both the raw `events` list and a chronologically ordered `timeline` array (1-based `index`, `segment_details`, `bridge_details`, `failed_bridges`) built from real stored `ClosureEvent` records in the graph.
- **`GET /replay/{event_id}` & `POST /replay/{event_id}`**: Replays any stored closure event (`observed` historical events or `resolved`/`active` simulated events) using `Snapshot.blocked_map(extra_events=(event_id,))` and `replay=True` in [`Toolbox`](../backend/app/graph/tools.py).
  - Computes full reachability (`cut_off_places`), hospital access (`hospital_access`), alternative route (`route_explanation` with `baseline_comparison`), and `draft_alerts` for that event's exact blocked segments and failed bridges **without mutating live active closures**.
- **Interactive Timeline Controls**: Operators can step backward/forward (`◀ Prev`, `Next ▶`), click any event in the timeline to enter Replay Mode, and click **Exit Replay** to return to the live corridor state.

---

## 4. Draft Alert-Plan Export (JSON & Plain Text)

- **`GET /alerts/{event_id}/export?format=json|text&replay=true`**:
  - Generates the event's alert plan and enforces `status == "draft"` and `sent == False` on every alert (`all_drafts: True`, `any_sent: False`).
  - Supports structured JSON (`AlertPlanExportResponse`) or downloadable plain-text (`Content-Disposition: attachment; filename="draft-alerts-{event_id}.txt"`).
- **UI Export Controls**: Operators can download `.json` or `.txt` files, copy the formatted plain-text alert plan to the clipboard, or preview the plain-text sheet inside a native `<details>` disclosure.

---

## 5. Alternative-Route Comparison

- **`Toolbox.explain_path` (`baseline_comparison`)**:
  - Computes both the active/replayed route and the unblocked baseline route for the same `(origin, destination, season, prefer_reliable)` query.
  - Reports `baseline_total_travel_time_min`, `baseline_total_length_km`, `detour_travel_time_delta_min`, `detour_length_delta_km`, `same_as_baseline`, `segments_avoided_from_baseline`, and `segments_added_in_detour`.
- **Map & WhyPanel Visualization**:
  - [`MapView.tsx`](../frontend/src/MapView.tsx) renders the avoided baseline route as a dashed slate overlay (`baseline-route-line`) alongside the solid blue active/detour route (`route-line`).
  - [`WhyPanel.tsx`](../frontend/src/WhyPanel.tsx) renders a side-by-side **Baseline Route** vs **Active / Detour Route** comparison card with avoided and added segment lists.

---

## 6. Loading/Error States, Responsive Layout & Accessibility

- **Loading & Replay Banners**: Top-level `aria-live="polite"` loading indicator with spinner during async API operations, plus a prominent Replay Mode banner when inspecting historical/stored events.
- **Responsive Mobile / Narrow-Viewport Switcher**: At `<= 1080px`, a mobile pane tab bar switches cleanly between **1. Controls & Timeline**, **2. Corridor Map**, and **3. Why / Evidence**.
- **Accessibility**: Keyboard skip link (`Skip to Why / Evidence Panel`), `aria-busy` on `<main>`, semantic `<details>` disclosures for searchable hidden content, and persistent safety banner + footer disclaimer.

