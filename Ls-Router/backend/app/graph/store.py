"""The only module that talks to FalkorDB. Every statement is logged so a tool response can quote what ran."""
from __future__ import annotations

from . import queries as Q
from .errors import StoreError, ToolStateError
from .snapshot import Bridge, Connection, Event, Hospital, Place, Season, Segment, Snapshot


class FalkorStore:
    def __init__(self, graph):
        self.graph = graph  # falkordb Graph: .query(cypher, params) -> result with .result_set
        self._log: list = []

    # ---- query log
    def reset_log(self) -> None:
        self._log = []

    def drain_log(self) -> list:
        out, self._log = self._log, []
        return out

    def _run(self, name: str, query: str, params: dict | None = None):
        params = params or {}
        self._log.append({"name": name, "query": query, "params": params})
        try:
            return self.graph.query(query, params)
        except Exception as exc:
            raise StoreError(f"Cypher statement '{name}' failed: {exc}") from exc

    def _rows(self, name: str, query: str, params: dict | None = None) -> list:
        return list(self._run(name, query, params).result_set or [])

    # ---- export
    def load_snapshot(self) -> Snapshot:
        snap = Snapshot()
        for r in self._rows("places", Q.Q_PLACES):
            snap.places[r[0]] = Place(*r)
        for r in self._rows("segments", Q.Q_SEGMENTS):
            snap.segments[r[0]] = Segment(*r[:16], corridor_id=r[16])
        for r in self._rows("connections", Q.Q_CONNECTIONS):
            snap.connections.append(Connection(r[0], r[1], r[2], r[3], r[4]))
        for r in self._rows("hospitals", Q.Q_HOSPITALS):
            if r[0] not in snap.hospitals:
                snap.hospitals[r[0]] = Hospital(*r[:9], access_place_id=r[9], snap_distance_m=r[10])
        bridge_rows: dict = {}
        for r in self._rows("bridges", Q.Q_BRIDGES):
            row = bridge_rows.setdefault(r[0], {"head": r[:8], "segs": []})
            if r[8] is not None:
                row["segs"].append(r[8])
        for bid, row in bridge_rows.items():
            snap.bridges[bid] = Bridge(*row["head"], segment_ids=tuple(sorted(row["segs"])))
        events = {r[0]: list(r) for r in self._rows("events", Q.Q_EVENTS)}
        bs, bb, near = {}, {}, {}
        for eid, sid in self._rows("blocks_segment", Q.Q_BLOCKS_SEGMENT):
            bs.setdefault(eid, []).append(sid)
        for eid, bid in self._rows("blocks_bridge", Q.Q_BLOCKS_BRIDGE):
            bb.setdefault(eid, []).append(bid)
        for eid, pid in self._rows("near_place", Q.Q_NEAR_PLACE):
            near.setdefault(eid, []).append(pid)
        for eid, r in events.items():
            snap.events[eid] = Event(*r, blocks_segments=tuple(sorted(bs.get(eid, []))),
                                     blocks_bridges=tuple(sorted(bb.get(eid, []))), near_places=tuple(sorted(near.get(eid, []))))
        snap.failed_in = [(b, e, y) for b, e, y in self._rows("failed_in", Q.Q_FAILED_IN)]
        season_rows: dict = {}
        for r in self._rows("seasons", Q.Q_SEASONS):
            row = season_rows.setdefault(r[0], {"head": r[:4], "segs": []})
            if r[4] is not None:
                row["segs"].append(r[4])
        for sid, row in season_rows.items():
            snap.seasons[sid] = Season(*row["head"], segment_ids=tuple(sorted(row["segs"])))
        ds = self._rows("dataset", Q.Q_DATASET)
        if ds:
            snap.dataset = dict(zip(("id", "kind", "network_fingerprint", "retrieved_at", "attribution"), ds[0]))
        return snap

    def load_details(self) -> dict:
        """Extra read-only fields for GET /graph, keyed by entity id: {places|hospitals|bridges|segments: {id: {...}}}.

        Unknown values stay None (a property that was never stored is NULL); nothing is guessed or defaulted.
        """
        out: dict = {"places": {}, "hospitals": {}, "bridges": {}, "segments": {}}
        for r in self._rows("place_detail", Q.Q_PLACE_DETAIL):
            out["places"][r[0]] = {"source_id": r[1], "retrieved_at": r[2], "last_verified": r[3], "population": r[4]}
        for r in self._rows("hospital_detail", Q.Q_HOSPITAL_DETAIL):
            out["hospitals"][r[0]] = {"source_id": r[1], "retrieved_at": r[2], "last_verified": r[3]}
        for r in self._rows("bridge_detail", Q.Q_BRIDGE_DETAIL):
            out["bridges"][r[0]] = {"source_id": r[1], "retrieved_at": r[2], "last_verified": r[3]}
        for r in self._rows("segment_detail", Q.Q_SEGMENT_DETAIL):
            out["segments"][r[0]] = {"source_id": r[1], "retrieved_at": r[2], "last_verified": r[3], "surface": r[4],
                                     "geometry": r[5]}
        return out

    # ---- writes
    def create_closure(self, event_id, description, cause, now, segment_id, bridge_id=None) -> None:
        params = {"event_id": event_id, "description": description, "cause": cause, "now": now, "segment_id": segment_id}
        if bridge_id:
            params["bridge_id"] = bridge_id
        rows = self._rows("create_closure", Q.W_CREATE_CLOSURE_WITH_BRIDGE if bridge_id else Q.W_CREATE_CLOSURE, params)
        if not rows:
            raise ToolStateError("The closure was not created: the segment (or bridge) no longer exists in the graph.")

    def resolve_closure(self, event_id, now) -> bool:
        return bool(self._rows("resolve_closure", Q.W_RESOLVE_CLOSURE, {"event_id": event_id, "now": now}))

    def mark_remembered(self, event_id, now):
        rows = self._rows("mark_remembered", Q.W_MARK_REMEMBERED, {"event_id": event_id, "now": now})
        return rows[0][0] if rows else None

    def record_failure(self, bridge_id, event_id, year) -> None:
        self._rows("record_failure", Q.W_RECORD_FAILURE, {"bridge_id": bridge_id, "event_id": event_id, "year": year})

    def upsert_alert(self, alert_id, place_id, event_id, text, supporting_json, now) -> None:
        rows = self._rows("upsert_alert", Q.W_UPSERT_ALERT, {"alert_id": alert_id, "place_id": place_id, "event_id": event_id,
                                                              "text": text, "supporting": supporting_json, "now": now})
        if not rows:
            raise ToolStateError(f"Alert not saved: place {place_id} not found.")

    def replace_alert_routes(self, alert_id, segment_ids) -> None:
        self._rows("clear_alert_routes", Q.W_CLEAR_ALERT_ROUTES, {"alert_id": alert_id})
        for sid in segment_ids:
            self._rows("link_alert_route", Q.W_LINK_ALERT_ROUTE, {"alert_id": alert_id, "segment_id": sid})

    def procedures(self) -> list:
        return [(r[0], r[1]) for r in self._rows("procedures", Q.Q_PROCEDURES)]
