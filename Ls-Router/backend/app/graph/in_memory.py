"""In-memory graph engine that answers the exact Cypher statements in `app.graph.queries` from a Phase 1 dataset dict.

Used by offline CLI modes (`--dataset`, `--fixture`) and tests when a live FalkorDB container is not running.
Every tool still goes through `FalkorStore` and executes the same Cypher strings and parameter bindings.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import queries as Q


class _Result:
    def __init__(self, rows: list):
        self.result_set = rows


class InMemoryGraph:
    def __init__(self, ds: dict, procedures=(), history_path: str | Path | None = None):
        self.log: list = []
        self.procedures = list(procedures)
        self.history_path: Path | None = Path(history_path).expanduser().resolve() if history_path else None
        self.corridor = ds["corridor"]["id"]
        self.no_corridor: set = set()
        self.places = {p["id"]: dict(p) for p in ds["places"]}
        self.segments = {s["id"]: dict(s) for s in ds["segments"]}
        self.connections = [dict(c) for c in ds["connections"]]
        self.hospitals = {h["id"]: dict(h) for h in ds["hospitals"]}
        self.located = {r["hospital_id"]: dict(r) for r in ds["hospital_located_at"]}
        self.bridges = {b["id"]: dict(b) for b in ds["bridges"]}
        self.bridge_on = {(r["bridge_id"], r["segment_id"]) for r in ds["bridge_on"]}
        self.events = {e["id"]: dict(e) for e in ds["observed_events"]}
        self.blocks = {(r["event_id"], r["target_id"], "seg") for r in ds["event_blocks"]}
        self.near = {(r["event_id"], r["place_id"]) for r in ds["event_near_place"]}
        self.failed = {(r["bridge_id"], r["event_id"]): r["year"] for r in ds["event_failed_bridges"]}
        self.seasons = {s["id"]: dict(s) for s in ds["seasons"]}
        self.affects = {(r["season_id"], r["segment_id"]) for r in ds["season_affects"]}
        self.dataset = dict(ds["datasets"][0]) if ds.get("datasets") else {}
        self.alerts, self.alert_targets, self.alert_routes = {}, {}, set()
        if self.history_path is not None and self.history_path.exists():
            self.load_history_file(self.history_path)
        self.handlers = {
            "RETURN 1": lambda _: [[1]],
            "RETURN 1 AS ok": lambda _: [[1]],
            Q.Q_PLACES: self._places,
            Q.Q_SEGMENTS: self._segments,
            Q.Q_CONNECTIONS: self._connections,
            Q.Q_HOSPITALS: self._hospitals,
            Q.Q_BRIDGES: self._bridges,
            Q.Q_EVENTS: self._events,
            Q.Q_BLOCKS_SEGMENT: self._blocks_seg,
            Q.Q_BLOCKS_BRIDGE: self._blocks_bridge,
            Q.Q_FAILED_IN: self._failed_in,
            Q.Q_NEAR_PLACE: self._near,
            Q.Q_SEASONS: self._seasons,
            Q.Q_DATASET: self._dataset,
            Q.Q_PLACE_DETAIL: self._place_detail,
            Q.Q_HOSPITAL_DETAIL: self._hospital_detail,
            Q.Q_BRIDGE_DETAIL: self._bridge_detail,
            Q.Q_SEGMENT_DETAIL: self._segment_detail,
            Q.Q_PROCEDURES: lambda p: [[n, m] for n, m in self.procedures],
            Q.W_CREATE_CLOSURE: self._create,
            Q.W_CREATE_CLOSURE_WITH_BRIDGE: self._create,
            Q.W_RESOLVE_CLOSURE: self._resolve,
            Q.W_MARK_REMEMBERED: self._remember,
            Q.W_RECORD_FAILURE: self._record_failure,
            Q.W_UPSERT_ALERT: self._upsert_alert,
            Q.W_CLEAR_ALERT_ROUTES: self._clear_routes,
            Q.W_LINK_ALERT_ROUTE: self._link_route,
        }

    def export_history_state(self) -> dict:
        """Export all mutable closure, bridge-failure, and alert history state as a JSON-serializable dict."""
        return {
            "events": [dict(e) for _, e in sorted(self.events.items())],
            "blocks": [[e, t, k] for e, t, k in sorted(self.blocks)],
            "near": [[e, p] for e, p in sorted(self.near)],
            "failed": [[b, e, y] for (b, e), y in sorted(self.failed.items())],
            "alerts": [dict(a) for _, a in sorted(self.alerts.items())],
            "alert_targets": dict(sorted(self.alert_targets.items())),
            "alert_routes": [[a, s] for a, s in sorted(self.alert_routes)],
        }

    def load_history_state(self, state: dict) -> None:
        """Restore persisted closure events, BLOCKS/FAILED_IN links, and draft alerts into this graph."""
        if not isinstance(state, dict):
            return
        for e in state.get("events", []):
            if isinstance(e, dict) and e.get("id"):
                self.events[e["id"]] = dict(e)
        for row in state.get("blocks", []):
            if isinstance(row, (list, tuple)) and len(row) == 3:
                self.blocks.add((str(row[0]), str(row[1]), str(row[2])))
        for row in state.get("near", []):
            if isinstance(row, (list, tuple)) and len(row) == 2:
                self.near.add((str(row[0]), str(row[1])))
        for row in state.get("failed", []):
            if isinstance(row, (list, tuple)) and len(row) == 3:
                self.failed[(str(row[0]), str(row[1]))] = row[2]
        for a in state.get("alerts", []):
            if isinstance(a, dict) and a.get("id"):
                self.alerts[a["id"]] = dict(a)
        for k, v in (state.get("alert_targets") or {}).items():
            self.alert_targets[str(k)] = str(v)
        for row in state.get("alert_routes", []):
            if isinstance(row, (list, tuple)) and len(row) == 2:
                self.alert_routes.add((str(row[0]), str(row[1])))

    def save_history_file(self, path: str | Path | None = None) -> Path | None:
        target = Path(path).expanduser().resolve() if path else self.history_path
        if target is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.export_history_state(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
        return target

    def load_history_file(self, path: str | Path | None = None) -> bool:
        target = Path(path).expanduser().resolve() if path else self.history_path
        if target is None or not target.exists():
            return False
        raw = json.loads(target.read_text(encoding="utf-8"))
        self.load_history_state(raw)
        return True

    def _persist_if_configured(self) -> None:
        if self.history_path is not None:
            self.save_history_file(self.history_path)

    def query(self, q: str, params: dict | None = None) -> _Result:
        self.log.append((q, dict(params or {})))
        handler = self.handlers.get(q)
        if handler is None:
            raise RuntimeError("InMemoryGraph has no handler for this Cypher text: " + q[:80])
        return _Result(handler(dict(params or {})))

    def _places(self, _):
        return [
            [p["id"], p.get("name"), p.get("type"), p.get("lat"), p.get("lon"), p.get("reference_key"),
             p.get("confidence"), p.get("source_url"), p.get("data_status")]
            for _, p in sorted(self.places.items())
        ]

    def _segments(self, _):
        g = lambda s, k: s.get(k)  # noqa: E731
        return [
            [s["id"], g(s, "name"), g(s, "ref"), g(s, "highway"), g(s, "length_km"), g(s, "travel_time_min"),
             g(s, "travel_time_basis"), g(s, "is_bidirectional"), g(s, "on_nh29"), g(s, "monsoon_risk"),
             g(s, "confidence"), g(s, "source_url"), g(s, "data_status"), g(s, "from_place_id"),
             g(s, "to_place_id"), g(s, "baseline_fp"), None if s["id"] in self.no_corridor else self.corridor]
            for _, s in sorted(self.segments.items())
        ]

    def _connections(self, _):
        return [
            [c["from_place_id"], c["to_place_id"], c.get("segment_id"), c.get("travel_time_min"), c.get("direction")]
            for c in sorted(
                self.connections,
                key=lambda c: (str(c.get("segment_id")), str(c.get("direction")), c["from_place_id"]),
            )
        ]

    def _hospitals(self, _):
        return [
            [h["id"], h.get("name"), h.get("lat"), h.get("lon"), h.get("beds"), h.get("trauma_level"),
             h.get("confidence"), h.get("source_url"), h.get("data_status"),
             self.located.get(h["id"], {}).get("place_id"), self.located.get(h["id"], {}).get("snap_distance_m")]
            for _, h in sorted(self.hospitals.items())
        ]

    def _bridges(self, _):
        rows = []
        for bid, b in sorted(self.bridges.items()):
            head = [bid, b.get("name"), b.get("road_name"), b.get("lat"), b.get("lon"), b.get("monsoon_risk"),
                    b.get("confidence"), b.get("source_url")]
            segs = sorted(s for (x, s) in self.bridge_on if x == bid)
            rows += [head + [s] for s in segs] or [head + [None]]
        return rows

    def _events(self, _):
        return [
            [e["id"], e.get("description"), e.get("status"), e.get("data_status"), e.get("started_at"),
             e.get("ended_at"), e.get("cause"), e.get("origin"), e.get("confidence"), e.get("remembered_at"),
             e.get("source_url"), e.get("location_text")]
            for _, e in sorted(self.events.items())
        ]

    def _blocks_seg(self, _):
        return [[e, t] for e, t, k in sorted(self.blocks) if k == "seg"]

    def _blocks_bridge(self, _):
        return [[e, t] for e, t, k in sorted(self.blocks) if k == "bridge"]

    def _failed_in(self, _):
        return [[b, e, y] for (b, e), y in sorted(self.failed.items())]

    def _near(self, _):
        return [[e, p] for e, p in sorted(self.near)]

    def _seasons(self, _):
        rows = []
        for sid, s in sorted(self.seasons.items()):
            head = [sid, s.get("name"), s.get("start_month"), s.get("end_month")]
            segs = sorted(x for (se, x) in self.affects if se == sid)
            rows += [head + [x] for x in segs] or [head + [None]]
        return rows

    def _dataset(self, _):
        d = self.dataset
        if not d:
            return []
        return [[d.get("id"), d.get("kind"), d.get("network_fingerprint"), d.get("retrieved_at"), d.get("attribution")]]

    def _place_detail(self, _):
        return [[p["id"], p.get("source_id"), p.get("retrieved_at"), p.get("last_verified"), p.get("population")]
                for _, p in sorted(self.places.items())]

    def _hospital_detail(self, _):
        return [[h["id"], h.get("source_id"), h.get("retrieved_at"), h.get("last_verified")]
                for _, h in sorted(self.hospitals.items())]

    def _bridge_detail(self, _):
        return [[b["id"], b.get("source_id"), b.get("retrieved_at"), b.get("last_verified")]
                for _, b in sorted(self.bridges.items())]

    def _segment_detail(self, _):
        return [[s["id"], s.get("source_id"), s.get("retrieved_at"), s.get("last_verified"), s.get("surface"), s.get("geometry")]
                for _, s in sorted(self.segments.items())]

    def _create(self, p):
        if p["segment_id"] not in self.segments or ("bridge_id" in p and p["bridge_id"] not in self.bridges):
            return []
        self.events[p["event_id"]] = {
            "id": p["event_id"],
            "description": p["description"],
            "cause": p["cause"],
            "status": "active",
            "data_status": "simulated",
            "origin": "simulation",
            "started_at": p["now"],
        }
        self.blocks.add((p["event_id"], p["segment_id"], "seg"))
        if "bridge_id" in p:
            self.blocks.add((p["event_id"], p["bridge_id"], "bridge"))
        self._persist_if_configured()
        return [[p["event_id"]]]

    def _resolve(self, p):
        e = self.events.get(p["event_id"])
        if e and e.get("status") == "active" and e.get("data_status") == "simulated":
            e["status"], e["ended_at"] = "resolved", p["now"]
            self._persist_if_configured()
            return [[e["id"]]]
        return []

    def _remember(self, p):
        e = self.events.get(p["event_id"])
        if not e:
            return []
        e["remembered"] = True
        e["remembered_at"] = e.get("remembered_at") or p["now"]
        self._persist_if_configured()
        return [[e["remembered_at"]]]

    def _record_failure(self, p):
        if p["bridge_id"] in self.bridges and p["event_id"] in self.events:
            self.failed[(p["bridge_id"], p["event_id"])] = p["year"]
            self._persist_if_configured()
            return [[p["bridge_id"]]]
        return []

    def _upsert_alert(self, p):
        if p["place_id"] not in self.places:
            return []
        a = self.alerts.setdefault(p["alert_id"], {"id": p["alert_id"]})
        a.update({
            "text": p["text"],
            "channel": "sms-draft",
            "target_place_id": p["place_id"],
            "status": "draft",
            "simulation": True,
            "closure_event_id": p["event_id"],
            "supporting_json": p["supporting"],
            "updated_at": p["now"],
        })
        a.setdefault("created_at", p["now"])
        self.alert_targets[p["alert_id"]] = p["place_id"]
        self._persist_if_configured()
        return [[p["alert_id"]]]

    def _clear_routes(self, p):
        self.alert_routes = {(a, s) for a, s in self.alert_routes if a != p["alert_id"]}
        self._persist_if_configured()
        return []

    def _link_route(self, p):
        if p["alert_id"] in self.alerts and p["segment_id"] in self.segments:
            self.alert_routes.add((p["alert_id"], p["segment_id"]))
            self._persist_if_configured()
            return [[p["segment_id"]]]
        return []

