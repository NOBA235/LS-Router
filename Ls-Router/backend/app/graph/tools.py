"""The eleven Landslide Router tools. Each call: read the graph with Cypher -> compute -> (maybe) write with Cypher ->
return the standard envelope. Nothing is cached between calls and nothing is invented: unknown values stay None."""
from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone

from . import algorithms as A
from .errors import ToolInputError, ToolStateError
from .fingerprint import network_fingerprint, segment_fingerprint
from .snapshot import Snapshot

SETTLEMENTS = ("village", "town")
RISK_RANK = {"high": 3, "medium": 2, "low": 1}
MONTH_NAMES = {n: i for i, n in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ESTIMATE_NOTE = "Travel times are model estimates (assumed or tagged speeds), not measured or live conditions."
FALKORDB_NATIVE_NOTE = ("FalkorDB algo.SPpaths/algo.WCC/algo.betweenness were not used: none can exclude edges whose RoadSegment "
                        "is blocked by an active ClosureEvent, and algo.betweenness scores nodes only.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(v, name, max_len=500) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ToolInputError(f"{name} must be a non-empty string")
    v = v.strip()
    if len(v) > max_len:
        raise ToolInputError(f"{name} is longer than {max_len} characters")
    return v


def _month(v) -> int:
    if isinstance(v, bool):
        raise ToolInputError("month must be 1-12 or a month name")
    if isinstance(v, int):
        m = v
    elif isinstance(v, str) and v.strip().isdigit():
        m = int(v.strip())
    elif isinstance(v, str) and v.strip()[:3].lower() in MONTH_NAMES:
        m = MONTH_NAMES[v.strip()[:3].lower()]
    else:
        raise ToolInputError(f"month {v!r} is not 1-12 or a month name")
    if not 1 <= m <= 12:
        raise ToolInputError(f"month {m} is outside 1-12")
    return m


def _in_season(start, end, m) -> bool:
    if not (isinstance(start, int) and isinstance(end, int)):
        return False
    return start <= m <= end if start <= end else (m >= start or m <= end)


def _event_month(started_at) -> int | None:
    mm = re.match(r"^\d{4}-(\d{2})", started_at or "")
    return int(mm.group(1)) if mm and 1 <= int(mm.group(1)) <= 12 else None


def _event_year(started_at) -> int | None:
    mm = re.match(r"^(\d{4})", started_at or "")
    return int(mm.group(1)) if mm else None


class Toolbox:
    def __init__(self, store, clock=None, id_factory=None):
        self.store = store
        self.clock = clock or _utc_now
        self.new_id = id_factory or (lambda: uuid.uuid4().hex[:8])

    # ------------------------------------------------------------------ plumbing
    def _done(self, tool, inputs, result, explanation, graph, warnings, algorithm) -> dict:
        log = self.store.drain_log()
        seen, uniq = set(), []
        for w in warnings:
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        return {"tool": tool, "result": result, "cypher": [q["query"] for q in log],
                "params": {"inputs": inputs, "query_names": [q["name"] for q in log], "queries": [q["params"] for q in log]},
                "explanation": explanation, "graph": graph, "warnings": uniq, "algorithm": algorithm}

    @staticmethod
    def _alg(name, detail="") -> dict:
        return {"name": name, "implementation": f"NetworkX {A.NX_VERSION} on a graph exported from FalkorDB with Cypher "
                                                  f"(documented fallback). {FALKORDB_NATIVE_NOTE} {detail}".strip()}

    @staticmethod
    def _cypher_only(name) -> dict:
        return {"name": name, "implementation": "FalkorDB Cypher only (no external algorithm)."}

    def _load(self) -> Snapshot:
        self.store.reset_log()
        return self.store.load_snapshot()

    @staticmethod
    def _need_place(snap, pid, name) -> None:
        if not isinstance(pid, str) or pid not in snap.places:
            raise ToolInputError(f"{name}: unknown place id {pid!r}")

    @staticmethod
    def _need_event(snap, eid, name="event_id"):
        if not isinstance(eid, str) or eid not in snap.events:
            raise ToolInputError(f"{name}: unknown closure event {eid!r}")
        return snap.events[eid]

    @staticmethod
    def _origin(snap):
        o = snap.reference("dimapur")
        if o is None:
            raise ToolStateError("The reference place 'dimapur' (reference_key) is not in the graph; seed the data first.")
        return o

    @staticmethod
    def _coverage_warnings(snap, cls=None) -> list:
        w = []
        no_time = sorted(s.id for s in snap.segments.values() if not A.weight_ok(s.travel_time_min))
        if no_time:
            w.append(f"{len(no_time)} segment(s) have a missing/invalid travel time and are excluded from routing "
                     f"(they still count for connectivity): {no_time[:5]}")
        if cls and cls["dangling"]:
            w.append(f"{len(cls['dangling'])} CONNECTED_BY edge(s) name no existing RoadSegment and are never traversable.")
        no_access = sorted(h.id for h in snap.hospitals.values() if not h.access_place_id)
        if no_access:
            w.append(f"{len(no_access)} hospital(s) have no road attachment and cannot be evaluated: {no_access[:5]}")
        return w

    # ---- payload helpers
    @staticmethod
    def _node(snap, pid, role=None) -> dict:
        p = snap.places[pid]
        return {"id": p.id, "label": "Place", "name": p.name, "type": p.type, "lat": p.lat, "lon": p.lon, "role": role,
                "confidence": p.confidence, "data_status": p.data_status, "source_url": p.source_url}

    @staticmethod
    def _edge(snap, sid, status) -> dict:
        s = snap.segments[sid]
        return {"id": s.id, "label": "RoadSegment", "source": s.from_place_id, "target": s.to_place_id, "status": status,
                "name": s.name, "ref": s.ref, "on_nh29": s.on_nh29, "is_bidirectional": s.is_bidirectional,
                "travel_time_min": s.travel_time_min, "confidence": s.confidence, "data_status": s.data_status,
                "source_url": s.source_url}

    def _graph(self, snap, place_roles=None, segment_status=None, hospital_ids=()) -> dict:
        place_roles, segment_status = dict(place_roles or {}), dict(segment_status or {})
        for sid in segment_status:
            s = snap.segments.get(sid)
            if s:
                for pid in (s.from_place_id, s.to_place_id):
                    if pid in snap.places:
                        place_roles.setdefault(pid, None)
        nodes = [self._node(snap, pid, place_roles[pid]) for pid in sorted(place_roles) if pid in snap.places]
        for hid in hospital_ids:
            h = snap.hospitals.get(hid)
            if h:
                nodes.append({"id": h.id, "label": "Hospital", "name": h.name, "lat": h.lat, "lon": h.lon, "beds": h.beds,
                              "trauma_level": h.trauma_level, "access_place_id": h.access_place_id, "confidence": h.confidence,
                              "source_url": h.source_url})
        edges = [self._edge(snap, sid, st) for sid, st in sorted(segment_status.items()) if sid in snap.segments]
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _event_summary(e) -> dict:
        return {"id": e.id, "description": e.description, "status": e.status, "data_status": e.data_status,
                "cause": e.cause, "started_at": e.started_at, "ended_at": e.ended_at, "origin": e.origin,
                "remembered_at": e.remembered_at, "blocks_segments": list(e.blocks_segments),
                "blocks_bridges": list(e.blocks_bridges)}

    def _route_payload(self, snap, r) -> dict | None:
        if r is None:
            return None
        steps = []
        for s in r["steps"]:
            seg = snap.segments[s["segment_id"]]
            steps.append({**s, "segment_name": seg.name, "ref": seg.ref, "length_km": seg.length_km,
                          "travel_time_basis": seg.travel_time_basis, "confidence": seg.confidence,
                          "source_url": seg.source_url, "data_status": seg.data_status,
                          "bridges_on_segment": sorted(b.id for b in snap.bridges.values() if seg.id in b.segment_ids)})
        return {"nodes": r["nodes"], "node_names": [snap.label(n) for n in r["nodes"]], "segments": steps,
                "total_travel_time_min": r["total_travel_time_min"], "total_length_km": A.route_length_km(snap, r),
                "travel_time_is_estimate": True}

    # ------------------------------------------------------------------ shared analysis
    def _cutoff(self, snap, blocked: dict, event_id=None) -> dict:
        """Baseline vs closed reachability from Dimapur (directed), plus Kohima and undirected components."""
        origin = self._origin(snap)
        G0, _ = A.topology_graph(snap, set())
        G1, cls1 = A.topology_graph(snap, set(blocked))
        base, now = A.reach(G0, origin.id), A.reach(G1, origin.id)
        newly = sorted(base - now)
        out = {"origin": origin, "base": base, "now": now, "newly": newly, "G0": G0, "G1": G1, "cls": cls1,
               "already_unreachable": sorted(set(snap.places) - base)}
        # evidence: baseline path to each newly cut-off place + the blocked boundary
        Gr0, _ = A.routing_graph(snap, set())
        _, weighted = A.single_source(Gr0, origin.id)
        evidence = {}
        for pid in newly:
            nodes = weighted.get(pid)
            if nodes:
                r = A.route(Gr0, origin.id, pid)
                evidence[pid] = {"place_id": pid, "path_basis": "travel_time_weighted", "baseline_path_nodes": nodes,
                                 "baseline_path_segments": [s["segment_id"] for s in r["steps"]],
                                 "baseline_travel_time_min": r["total_travel_time_min"]}
            else:
                nodes = A.hop_path(G0, origin.id, pid)
                segs = [G0[u][v]["segment_ids"][0] for u, v in zip(nodes, nodes[1:])] if nodes else []
                evidence[pid] = {"place_id": pid, "path_basis": "fewest_hops (no valid weights; connectivity evidence only)",
                                 "baseline_path_nodes": nodes, "baseline_path_segments": segs, "baseline_travel_time_min": None}
            evidence[pid]["blocked_segments_on_path"] = sorted(s for s in evidence[pid]["baseline_path_segments"] if s in blocked)
        out["evidence"] = evidence
        out["boundary"] = sorted(({"segment_id": c.segment_id, "from": c.from_id, "to": c.to_id, "direction": c.direction,
                                   "blocked_by": blocked.get(c.segment_id, [])}
                                  for c in cls1["blocked"] if c.from_id in now and c.to_id in out["newly"]),
                                 key=lambda d: (d["segment_id"], d["direction"]))
        # attribution: cut off now, but reachable if this event alone were cleared
        out["attributable"], out["other"] = [], list(newly)
        if event_id and snap.events[event_id].status == "active":
            G2, _ = A.topology_graph(snap, set(snap.blocked_map(without_events={event_id})))
            reach2 = A.reach(G2, origin.id)
            out["attributable"] = [p for p in newly if p in reach2]
            out["other"] = [p for p in newly if p not in reach2]
        # Kohima (secondary reference) and undirected components
        kohima = snap.reference("kohima")
        out["kohima"] = None
        if kohima:
            kb, kn = A.reach(G0, kohima.id), A.reach(G1, kohima.id)
            out["kohima"] = {"place_id": kohima.id, "baseline_reachable": len(kb), "reachable_with_closures": len(kn),
                             "newly_unreachable": sorted(kb - kn)}
        c0, c1 = A.weak_components(G0), A.weak_components(G1)
        base_sets = set(c0)
        out["components"] = {"representation": "weakly connected components of the undirected road network (complete decomposition; "
                                               "unlike the Dimapur reachability above, direction is ignored)",
                             "baseline_component_count": len(c0), "component_count_with_closures": len(c1),
                             "new_components": [sorted(c) for c in c1 if c not in base_sets],
                             "new_components_note": "every component that did not exist in the baseline, so BOTH halves of a split appear",
                             "split_off_from_origin_component": [sorted(c) for c in c1 if c not in base_sets and origin.id not in c]}
        return out

    def _settlements(self, snap, ids) -> list:
        return [p for p in ids if snap.places[p].type in SETTLEMENTS]

    # ------------------------------------------------------------------ 1. apply_closure
    def apply_closure(self, segment_id, description, cause="landslide", bridge_id=None) -> dict:
        inputs = {"segment_id": segment_id, "description": description, "cause": cause, "bridge_id": bridge_id}
        seg_id, desc, cause = _text(segment_id, "segment_id", 200), _text(description, "description"), _text(cause, "cause", 50)
        snap = self._load()
        seg = snap.segments.get(seg_id)
        if seg is None:
            raise ToolInputError(f"segment_id: unknown road segment {seg_id!r}")
        if not seg.corridor_id:
            raise ToolInputError(f"segment {seg_id} is not SEGMENT_OF any corridor, so it cannot be closed here")
        if bridge_id is not None:
            bridge = snap.bridges.get(bridge_id)
            if bridge is None:
                raise ToolInputError(f"bridge_id: unknown bridge {bridge_id!r}")
            if seg_id not in bridge.segment_ids:
                raise ToolInputError(f"bridge {bridge_id} is not ON segment {seg_id}")
        want_bridges = {bridge_id} if bridge_id else set()
        existing = next((e for e in snap.events.values() if e.status == "active" and e.data_status == "simulated"
                         and e.description == desc and set(e.blocks_segments) == {seg_id} and set(e.blocks_bridges) == want_bridges), None)
        before, warnings = snap.blocked_map(), []
        if existing:
            created, event_id, snap2 = False, existing.id, snap
            warnings.append(f"An identical active closure ({existing.id}) already exists; nothing was written.")
        else:
            event_id = f"sim-{self.new_id()}"
            if event_id in snap.events:
                raise ToolStateError(f"event id {event_id} already exists")
            self.store.create_closure(event_id, desc, cause, self.clock(), seg_id, bridge_id)
            snap2 = self.store.load_snapshot()  # report the state the graph is actually in
            ev = snap2.events.get(event_id)
            if ev is None or ev.status != "active" or seg_id not in ev.blocks_segments:
                raise ToolStateError(f"closure {event_id} was not found in the graph after creation")
            created = True
        after = snap2.blocked_map()
        targets = snap2.event_segment_ids(event_id)
        shared = {s: [e for e in before[s] if e != event_id] for s in sorted(targets) if s in before and [e for e in before[s] if e != event_id]}
        disabled = sorted(({"from": c.from_id, "to": c.to_id, "direction": c.direction, "segment_id": c.segment_id}
                           for c in snap2.connections if c.segment_id in targets), key=lambda d: (d["segment_id"], d["direction"]))
        if seg.confidence != "verified":
            warnings.append(f"Segment {seg_id} is '{seg.confidence}' (derived from OpenStreetMap, not hand-verified): the closure "
                            "location is a model assumption, not a confirmed event.")
        if shared:
            warnings.append(f"Segment(s) {sorted(shared)} are also blocked by other active closures {sorted({e for v in shared.values() for e in v})}.")
        warnings += self._coverage_warnings(snap2)
        result = {"created": created, "event": self._event_summary(snap2.events[event_id]),
                  "target_segment": {"id": seg.id, "name": seg.name, "ref": seg.ref, "from_place_id": seg.from_place_id,
                                     "to_place_id": seg.to_place_id, "confidence": seg.confidence, "source_url": seg.source_url},
                  "location_confidence": seg.confidence,
                  "state_change": {"blocked_segments_before": sorted(before), "blocked_segments_after": sorted(after),
                                   "newly_blocked_segments": sorted(s for s in targets if s not in before),
                                   "already_blocked_by_other_events": shared, "connections_now_disabled": disabled}}
        expl = (f"{'Created' if created else 'Found'} simulated closure {event_id} blocking {seg_id}; the graph now has "
                f"{len(after)} blocked segment(s) and {len(disabled)} connection(s) of this closure are untraversable. "
                "Simulated closures are demonstration data, distinct from observed events.")
        graph = self._graph(snap2, segment_status={s: "blocked" for s in after})
        return self._done("apply_closure", inputs, result, expl, graph, warnings, self._cypher_only("closure-write"))

    # ------------------------------------------------------------------ 2. clear_closure
    def clear_closure(self, event_id) -> dict:
        inputs = {"event_id": event_id}
        snap = self._load()
        ev = self._need_event(snap, event_id)
        if ev.data_status != "simulated":
            raise ToolStateError(f"{ev.id} is a '{ev.data_status}' event (a historical record), not a simulated closure; it cannot be cleared.")
        before, warnings = snap.blocked_map(), []
        prior_status = ev.status
        if ev.status == "active":
            if not self.store.resolve_closure(ev.id, self.clock()):
                raise ToolStateError(f"closure {ev.id} could not be resolved (it changed state concurrently)")
            snap2 = self.store.load_snapshot()
            if snap2.events[ev.id].status != "resolved":
                raise ToolStateError(f"closure {ev.id} is still not resolved after the update")
            transition = {"from": "active", "to": "resolved", "ended_at": snap2.events[ev.id].ended_at}
        elif ev.status == "resolved":
            snap2, transition = snap, None
            warnings.append(f"{ev.id} was already resolved; nothing was changed.")
        else:
            raise ToolStateError(f"{ev.id} has unexpected status {ev.status!r}")
        after = snap2.blocked_map()
        targets = snap2.event_segment_ids(ev.id)
        still = {s: after[s] for s in sorted(targets) if s in after}
        restored = sorted(s for s in targets if s not in after)
        e2 = snap2.events[ev.id]
        if still:
            warnings.append(f"Segment(s) {sorted(still)} remain blocked by other active closures; they were NOT reopened.")
        result = {"event": self._event_summary(e2), "state_transition": transition, "prior_status": prior_status,
                  "restored_segments": restored, "still_blocked_by_other_closures": still,
                  "blocked_segments_before": sorted(before), "blocked_segments_after": sorted(after),
                  "history_preserved": {"event_node_kept": True, "blocks_links_kept": len(e2.blocks_segments) + len(e2.blocks_bridges),
                                        "failure_records_kept": sum(1 for f in snap2.failed_in if f[1] == ev.id)}}
        expl = (f"{ev.id}: {prior_status} -> {e2.status}. Reopened {len(restored)} segment(s); {len(still)} stayed blocked by other "
                "closures. The event and its BLOCKS links are kept as history.")
        graph = self._graph(snap2, segment_status={**{s: "blocked" for s in after}, **{s: "restored" for s in restored}})
        return self._done("clear_closure", inputs, result, expl, graph, warnings, self._cypher_only("closure-write"))

    # ------------------------------------------------------------------ 3. find_cut_off_places
    def find_cut_off_places(self, closure_event_id) -> dict:
        inputs = {"closure_event_id": closure_event_id}
        snap = self._load()
        ev = self._need_event(snap, closure_event_id, "closure_event_id")
        blocked = snap.blocked_map()
        c = self._cutoff(snap, blocked, ev.id)
        warnings = self._coverage_warnings(snap, c["cls"])
        if ev.status != "active":
            warnings.append(f"{ev.id} is '{ev.status}', not active: it blocks nothing now, so no place is attributed to it. "
                            "Results show the effect of any OTHER active closures.")
        if c["already_unreachable"]:
            warnings.append(f"{len(c['already_unreachable'])} place(s) were already unreachable from Dimapur in the baseline "
                            "(incomplete coverage or one-way access); they are NOT reported as newly cut off.")
        if snap.reference("kohima") and snap.reference("kohima").id not in c["base"]:
            warnings.append("Kohima is not reachable from Dimapur in the baseline network: the extract may be incomplete.")
        warnings.append("Reachability is DIRECTED from the reference origin (Dimapur); the component analysis below ignores direction.")
        items = []
        for pid in c["newly"]:
            p = snap.places[pid]
            items.append({"id": pid, "name": p.name, "type": p.type, "attributable_to_this_event": pid in c["attributable"],
                          "evidence": c["evidence"][pid]})
        by_type = {k: [i for i in items if i["type"] == k] for k in ("village", "town", "junction")}
        by_type["other"] = [i for i in items if i["type"] not in ("village", "town", "junction")]
        hospitals = [{"id": h.id, "name": h.name, "access_place_id": h.access_place_id}
                     for h in snap.hospitals.values() if h.access_place_id in c["newly"]]
        result = {"closure_event": self._event_summary(ev), "blocked_segments": blocked,
                  "reachability": {"kind": "directed reachability from the reference origin (not a component analysis)",
                                   "origin": {"id": c["origin"].id, "name": c["origin"].name},
                                   "baseline_reachable_places": len(c["base"]), "reachable_with_active_closures": len(c["now"])},
                  "newly_unreachable": {"count": len(items), "villages": by_type["village"], "towns": by_type["town"],
                                        "junctions": by_type["junction"], "other": by_type["other"]},
                  "attributable_to_this_event": c["attributable"], "also_cut_by_other_closures": c["other"],
                  "already_unreachable_in_baseline": [{"id": p, "name": snap.places[p].name, "type": snap.places[p].type}
                                                      for p in c["already_unreachable"]],
                  "hospitals_newly_unreachable_from_dimapur": hospitals, "cut_boundary": c["boundary"],
                  "from_kohima": c["kohima"], "component_analysis": c["components"]}
        expl = (f"With {len(blocked)} blocked segment(s), {len(items)} place(s) reachable from {c['origin'].name or c['origin'].id} "
                f"in the baseline are no longer reachable ({len(by_type['village'])} village(s), {len(by_type['town'])} town(s), "
                f"{len(by_type['junction'])} junction(s)).")
        roles = {c["origin"].id: "origin", **{p: "cut_off" for p in c["newly"]}}
        seg_status = {s: "blocked" for s in blocked}
        for i in items:
            for s in i["evidence"]["baseline_path_segments"]:
                seg_status.setdefault(s, "baseline_path")
        graph = self._graph(snap, roles, seg_status, [h["id"] for h in hospitals])
        return self._done("find_cut_off_places", inputs, result, expl, graph, warnings,
                          self._alg("directed reachability + weakly connected components", "Evidence paths: Dijkstra on travel time."))

    # ------------------------------------------------------------------ 4. reroute / 9. explain_path (shared core)
    def _route_request(self, from_place_id, to_place_id, avoid_event_id):
        snap = self._load()
        self._need_place(snap, from_place_id, "from_place_id")
        self._need_place(snap, to_place_id, "to_place_id")
        if avoid_event_id is not None:
            self._need_event(snap, avoid_event_id, "avoid_event_id")
        blocked = snap.blocked_map(extra_events=(avoid_event_id,) if avoid_event_id else ())
        Gc, cls = A.routing_graph(snap, set(blocked))
        Gb, _ = A.routing_graph(snap, set())
        topo, _ = A.topology_graph(snap, set(blocked))
        closed, base = A.route(Gc, from_place_id, to_place_id), A.route(Gb, from_place_id, to_place_id)
        connected = to_place_id in A.reach(topo, from_place_id)
        warnings = self._coverage_warnings(snap, cls) + [ESTIMATE_NOTE]
        if avoid_event_id and snap.events[avoid_event_id].status != "active":
            warnings.append(f"{avoid_event_id} is not active; its segments were excluded anyway because you asked to avoid it.")
        if cls["invalid_weight"]:
            warnings.append(f"{len(cls['invalid_weight'])} usable connection(s) were skipped for a missing/invalid travel time.")
        return snap, blocked, closed, base, connected, cls, warnings

    def reroute(self, from_place_id, to_place_id, avoid_event_id=None) -> dict:
        inputs = {"from_place_id": from_place_id, "to_place_id": to_place_id, "avoid_event_id": avoid_event_id}
        snap, blocked, closed, base, connected, cls, warnings = self._route_request(from_place_id, to_place_id, avoid_event_id)
        if closed is not None:
            status, reason = "route_found", None
        elif connected:
            status, reason = "no_route", "A road path exists but every usable path needs a connection with a missing/invalid travel time."
        else:
            status, reason = "no_route", "No directed path exists once blocked segments are excluded."
        if base is None:
            warnings.append("No baseline route exists between these places (incomplete coverage, direction, or missing weights); "
                            "the additional travel time cannot be computed.")
        extra = (round(closed["total_travel_time_min"] - base["total_travel_time_min"], 6) if closed and base else None)
        result = {"from_place_id": from_place_id, "to_place_id": to_place_id, "avoid_event_id": avoid_event_id,
                  "blocked_segments": blocked, "status": status, "reason": reason,
                  "route": self._route_payload(snap, closed), "baseline_route": self._route_payload(snap, base),
                  "additional_travel_time_min": extra,
                  "objective": "minimum total travel_time_min over usable directed connections (not fewest hops)"}
        expl = (f"Fastest modelled route {snap.label(from_place_id)} -> {snap.label(to_place_id)}: {closed['total_travel_time_min']} min "
                f"(estimate), avoiding {len(blocked)} blocked segment(s)"
                + (f"; baseline {base['total_travel_time_min']} min, so {extra:+g} min." if extra is not None else ".")
                if closed else f"No route: {reason}")
        roles = {from_place_id: "origin", to_place_id: "destination"}
        seg = {s: "blocked" for s in blocked}
        if closed:
            seg.update({s["segment_id"]: "route" for s in closed["steps"]})
        return self._done("reroute", inputs, result, expl, self._graph(snap, roles, seg), warnings,
                          self._alg("dijkstra_path (weight = travel_time_min)", "Directed edges; blocked segments excluded."))

    def explain_path(self, from_place_id, to_place_id, avoid_event_id=None) -> dict:
        inputs = {"from_place_id": from_place_id, "to_place_id": to_place_id, "avoid_event_id": avoid_event_id}
        snap, blocked, closed, base, connected, cls, warnings = self._route_request(from_place_id, to_place_id, avoid_event_id)
        if closed is None:
            result = {"status": "no_route", "selected_path": None,
                      "exclusions": {"blocked_segments": blocked, "excluded_invalid_weight": sorted({c.segment_id for c in cls["invalid_weight"]})},
                      "reason": "Directed path missing" if not connected else "Only paths with invalid weights exist"}
            return self._done("explain_path", inputs, result, "No route exists, so there is no path to explain.",
                              self._graph(snap, {from_place_id: "origin", to_place_id: "destination"}, {s: "blocked" for s in blocked}),
                              warnings, self._alg("dijkstra_path (weight = travel_time_min)"))
        payload = self._route_payload(snap, closed)
        total = payload["total_travel_time_min"]
        contributions = [{"segment_id": s["segment_id"], "name": s["segment_name"], "from": s["from"], "to": s["to"],
                          "direction": s["direction"], "travel_time_min": s["travel_time_min"], "length_km": s["length_km"],
                          "share_of_total": round(s["travel_time_min"] / total, 4) if total else None,
                          "travel_time_basis": s["travel_time_basis"], "confidence": s["confidence"], "source_url": s["source_url"],
                          "data_status": s["data_status"], "bridges_on_segment": s["bridges_on_segment"]} for s in payload["segments"]]
        checks = {"segment_times_sum_to_total": abs(sum(c["travel_time_min"] for c in contributions) - total) < 1e-6,
                  "no_selected_segment_is_blocked": not any(c["segment_id"] in blocked for c in contributions)}
        result = {"status": "route_found", "selected_path": {"nodes": payload["nodes"], "node_names": payload["node_names"]},
                  "contributions": contributions, "total_travel_time_min": total, "total_length_km": payload["total_length_km"],
                  "baseline_total_travel_time_min": base["total_travel_time_min"] if base else None,
                  "exclusions": {"blocked_segments": blocked,
                                 "excluded_invalid_weight": sorted({c.segment_id for c in cls["invalid_weight"]}),
                                 "dangling_connections": len(cls["dangling"])},
                  "checks": checks, "selection_rule": "minimum sum of travel_time_min; ties resolved by NetworkX Dijkstra over "
                                                       "edges inserted in sorted order; parallel segments keep the fastest"}
        steps = " + ".join(f"{c['travel_time_min']:g} ({c['segment_id']})" for c in contributions)
        expl = f"Total {total:g} min (estimate) = {steps}. {len(blocked)} blocked segment(s) were excluded before searching."
        seg = {s: "blocked" for s in blocked}
        seg.update({c["segment_id"]: "route" for c in contributions})
        return self._done("explain_path", inputs, result, expl,
                          self._graph(snap, {from_place_id: "origin", to_place_id: "destination"}, seg), warnings,
                          self._alg("dijkstra_path (weight = travel_time_min)", "Directed edges; blocked segments excluded."))

    # ------------------------------------------------------------------ 5. find_unreachable_hospitals
    def find_unreachable_hospitals(self, closure_event_id, threshold_min=None) -> dict:
        inputs = {"closure_event_id": closure_event_id, "threshold_min": threshold_min}
        if threshold_min is not None and not A.weight_ok(threshold_min):
            raise ToolInputError("threshold_min must be a positive number")
        snap = self._load()
        ev = self._need_event(snap, closure_event_id, "closure_event_id")
        blocked = snap.blocked_map()
        c = self._cutoff(snap, blocked, ev.id)
        affected = self._settlements(snap, c["newly"])
        Gr0, _ = A.routing_graph(snap, set())
        Gr1, _ = A.routing_graph(snap, set(blocked))
        hospitals = [h for h in sorted(snap.hospitals.values(), key=lambda h: h.id) if h.access_place_id in snap.places]
        warnings = self._coverage_warnings(snap, c["cls"]) + [ESTIMATE_NOTE,
                   "Reachability is not suitability: bed counts and trauma capability are unknown unless listed; no capability is assumed."]
        if ev.status != "active":
            warnings.append(f"{ev.id} is '{ev.status}', not active; only other active closures affect the result.")
        per_village, by_hosp = [], {h.id: {"reachable_from": [], "newly_unreachable_from": [], "unreachable_in_baseline_too_from": [],
                                           "over_threshold_from": []} for h in hospitals}
        for v in affected:
            r0, r1 = A.reach(c["G0"], v), A.reach(c["G1"], v)
            rows = []
            for h in hospitals:
                ap = h.access_place_id
                t_now = A.route(Gr1, v, ap) if ap in r1 else None
                t_base = A.route(Gr0, v, ap) if ap in r0 else None
                if ap in r1:
                    st = "reachable"
                    by_hosp[h.id]["reachable_from"].append(v)
                    if ap in r1 and t_now is None:
                        warnings.append(f"Route {v}->{h.id} exists but has no valid travel time.")
                elif ap in r0:
                    st = "newly_unreachable"
                    by_hosp[h.id]["newly_unreachable_from"].append(v)
                else:
                    st = "unreachable_in_baseline_too"
                    by_hosp[h.id]["unreachable_in_baseline_too_from"].append(v)
                over = bool(threshold_min and t_now and t_now["total_travel_time_min"] > threshold_min)
                if over:
                    by_hosp[h.id]["over_threshold_from"].append(v)
                rows.append({"hospital_id": h.id, "hospital_name": h.name, "status": st,
                             "travel_time_min": t_now["total_travel_time_min"] if t_now else None,
                             "baseline_travel_time_min": t_base["total_travel_time_min"] if t_base else None,
                             "over_threshold": over,
                             "route_segments": [s["segment_id"] for s in t_now["steps"]] if t_now else None})
            per_village.append({"village_id": v, "name": snap.places[v].name, "type": snap.places[v].type, "hospitals": rows})
        summary = [{"hospital_id": h.id, "name": h.name, "beds": h.beds, "trauma_level": h.trauma_level,
                    "access_place_id": h.access_place_id, "confidence": h.confidence, "source_url": h.source_url,
                    **by_hosp[h.id]} for h in hospitals]
        no_route = [s["hospital_id"] for s in summary if affected and not s["reachable_from"]]
        result = {"closure_event": self._event_summary(ev), "affected_settlements": affected, "threshold_min": threshold_min,
                  "per_settlement": per_village, "per_hospital": summary,
                  "hospitals_with_no_usable_route_from_any_affected_settlement": no_route,
                  "hospitals_without_road_attachment": sorted(h.id for h in snap.hospitals.values() if not h.access_place_id),
                  "note": "'Affected' = village or town newly unreachable from Dimapur. 'unreachable' and 'over_threshold' are separate lists."}
        expl = (f"{len(affected)} settlement(s) newly cut off from Dimapur; {len(no_route)} of {len(hospitals)} evaluated hospital(s) "
                "have no usable road route from any of them." if affected else "No settlement is newly cut off, so no hospital access changed.")
        roles = {p: "cut_off" for p in affected}
        graph = self._graph(snap, roles, {s: "blocked" for s in blocked}, [h.id for h in hospitals])
        return self._done("find_unreachable_hospitals", inputs, result, expl, graph, warnings,
                          self._alg("directed reachability + dijkstra_path", "Hospitals are reached through their road access Place; "
                                                                             "NEAREST_HOSPITAL is never used."))

    # ------------------------------------------------------------------ 6. find_weak_links
    @staticmethod
    def _restrict(snap, seg_ids):
        import dataclasses
        conns = [c for c in snap.connections if c.segment_id in seg_ids]
        pids = {c.from_id for c in conns} | {c.to_id for c in conns}
        return dataclasses.replace(snap, segments={k: v for k, v in snap.segments.items() if k in seg_ids},
                                   connections=conns, places={k: v for k, v in snap.places.items() if k in pids})

    def find_weak_links(self, corridor_id, top_k=5) -> dict:
        inputs = {"corridor_id": corridor_id, "top_k": top_k}
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 200:
            raise ToolInputError("top_k must be an integer from 1 to 200")
        snap = self._load()
        if corridor_id not in snap.corridors():
            raise ToolInputError(f"corridor_id: unknown corridor {corridor_id!r} (known: {sorted(snap.corridors())})")
        sub = self._restrict(snap, {k for k, s in snap.segments.items() if s.corridor_id == corridor_id})
        G, cls = A.routing_graph(sub, set())
        cen, usage, art = A.centrality(G), A.pair_usage(G), A.articulation_places(G)
        origin = snap.reference("dimapur")
        warnings = self._coverage_warnings(sub, cls) + [
            "Betweenness measures how many modelled shortest paths use a segment, bridge or place. It is NOT a failure probability, "
            "and a high score does not mean the element will fail.",
            "Computed on the BASELINE corridor network (active closures are ignored) with travel_time_min weights; segments without a "
            "valid weight are not scored."]
        if origin is None or origin.id not in sub.places:
            warnings.append("Reference place 'dimapur' is not in this corridor subgraph; per-segment cut-off effects are omitted.")
        seg_scores = {sid: cen["segments"].get(sid, 0.0) for sid in {d["segment_id"] for _, _, d in G.edges(data=True)}}
        ranked = sorted(seg_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        base_components = len(A.weak_components(G))
        segs = []
        for i, (sid, score) in enumerate(ranked, 1):
            seg = snap.segments[sid]
            u = usage.get(sid, {"pairs": 0, "examples": []})
            entry = {"rank": i, "segment_id": sid, "name": seg.name, "ref": seg.ref, "on_nh29": seg.on_nh29,
                     "betweenness": round(score, 6), "ordered_pairs_whose_chosen_shortest_path_uses_it": u["pairs"],
                     "example_paths": [{"from": snap.label(a), "to": snap.label(b), "nodes": n} for a, b, n in u["examples"]],
                     "carries_bridges": sorted(b.id for b in snap.bridges.values() if sid in b.segment_ids),
                     "confidence": seg.confidence, "source_url": seg.source_url, "data_status": seg.data_status}
            if origin is not None and origin.id in sub.places:
                cut = A.cut_off_if_blocked(sub, origin.id, {sid})
                Gx, _ = A.topology_graph(sub, {sid})
                entry["if_closed"] = {"places_cut_off_from_dimapur": sorted(cut), "settlements_cut_off": self._settlements(sub, cut),
                                      "undirected_components_after": len(A.weak_components(Gx)),
                                      "undirected_components_before": base_components}
            segs.append(entry)
        bridge_scores = {}
        for b in snap.bridges.values():
            sc = [seg_scores[s] for s in b.segment_ids if s in seg_scores]
            if sc:
                bridge_scores[b.id] = max(sc)
        bridges = [{"rank": i, "bridge_id": bid, "name": snap.bridges[bid].name, "road_name": snap.bridges[bid].road_name,
                    "score_inherited_from_segments": list(snap.bridges[bid].segment_ids), "betweenness": round(score, 6),
                    "confidence": snap.bridges[bid].confidence, "source_url": snap.bridges[bid].source_url}
                   for i, (bid, score) in enumerate(sorted(bridge_scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k], 1)]
        places = [{"rank": i, "place_id": pid, "name": snap.places[pid].name, "type": snap.places[pid].type,
                   "betweenness": round(sc, 6), "is_articulation_point_undirected": pid in art}
                  for i, (pid, sc) in enumerate(sorted(((p, s) for p, s in cen["nodes"].items() if s > 0), key=lambda kv: (-kv[1], kv[0]))[:top_k], 1)]
        result = {"corridor_id": corridor_id, "top_k": top_k,
                  "representation": "directed graph of CONNECTED_BY edges weighted by travel_time_min; segment score = sum of its "
                                    "directed edges' normalized edge betweenness; a bridge inherits the maximum score of the segments "
                                    "it is ON; parallel segments between the same two places keep only the fastest.",
                  "segments": segs, "bridges": bridges, "places": places,
                  "graph_size": {"places": G.number_of_nodes(), "directed_edges": G.number_of_edges()}}
        expl = (f"Ranked {len(segs)} segment(s), {len(bridges)} bridge(s) and {len(places)} place(s) by shortest-path betweenness on the "
                "corridor network. These are structural weak points, not predictions of failure.")
        seg_status = {s["segment_id"]: "weak_link" for s in segs}
        roles = {p["place_id"]: "high_betweenness" for p in places}
        return self._done("find_weak_links", inputs, result, expl, self._graph(snap, roles, seg_status), warnings,
                          self._alg("betweenness_centrality / edge_betweenness_centrality (weight = travel_time_min), "
                                    "articulation_points, all_pairs_dijkstra_path"))

    # ------------------------------------------------------------------ 7. seasonal_risk
    def seasonal_risk(self, month) -> dict:
        inputs = {"month": month}
        m = _month(month)
        snap = self._load()
        seasons = [s for s in snap.seasons.values() if _in_season(s.start_month, s.end_month, m)]
        warnings = ["Counts of events are evidence to review, not probabilities. A small number of events does not support a failure "
                    "probability, and no future failure is predicted.",
                    "Season-to-segment links in the current data are a blanket modelling assumption (every segment exposed), so "
                    "'applicable' carries no information by itself; ranking comes from risk labels and event history."]
        if not seasons:
            return self._done("seasonal_risk", inputs, {"month": m, "seasons": [], "ranked": [], "ranking_method": None},
                              f"No season in the graph covers month {m}.", self._graph(snap), warnings,
                              self._cypher_only("seasonal-read"))
        seg_ids = sorted({sid for s in seasons for sid in s.segment_ids if sid in snap.segments})
        bridges = {b.id: b for b in snap.bridges.values() if any(s in seg_ids for s in b.segment_ids)}
        windows = [(s.start_month, s.end_month) for s in seasons]

        def ev_row(e):
            return {"id": e.id, "started_at": e.started_at, "cause": e.cause, "status": e.status, "confidence": e.confidence,
                    "source_url": e.source_url, "data_status": e.data_status}

        def split(events):
            obs, sim, other_season, undated = [], [], [], []
            for e in sorted(events, key=lambda e: e.id):
                mo = _event_month(e.started_at)
                if mo is None:
                    undated.append(e.id)
                elif any(_in_season(a, b, mo) for a, b in windows):
                    (obs if e.data_status == "observed" else sim if e.data_status == "simulated" else []).append(ev_row(e))
                else:
                    other_season.append(e.id)
            return obs, sim, other_season, undated

        seg_events: dict = {}
        bridge_events: dict = {}
        for e in snap.events.values():
            for sid in e.blocks_segments:
                seg_events.setdefault(sid, []).append(e)
            for bid in e.blocks_bridges:
                bridge_events.setdefault(bid, []).append(e)
        for bid, eid, _y in snap.failed_in:
            if snap.events.get(eid) and snap.events[eid] not in bridge_events.get(bid, []):
                bridge_events.setdefault(bid, []).append(snap.events[eid])

        def make(kind, ident, name, risk, events, extra):
            obs, sim, oth, und = split(events)
            return {"kind": kind, "id": ident, "name": name, "monsoon_risk": risk, "observed_in_season": obs,
                    "simulated_in_season": sim, "events_outside_season": oth, "undated_events": und,
                    "counts": {"observed": len(obs), "simulated": len(sim)}, **extra}

        rows = [make("segment", sid, snap.segments[sid].name, snap.segments[sid].monsoon_risk, seg_events.get(sid, []),
                     {"carries_bridges": sorted(b for b, br in bridges.items() if sid in br.segment_ids),
                      "confidence": snap.segments[sid].confidence, "source_url": snap.segments[sid].source_url}) for sid in seg_ids]
        rows += [make("bridge", bid, b.name, b.monsoon_risk, bridge_events.get(bid, []),
                      {"segments": list(b.segment_ids), "confidence": b.confidence, "source_url": b.source_url})
                 for bid, b in sorted(bridges.items())]
        signal = [r for r in rows if r["monsoon_risk"] in RISK_RANK or r["counts"]["observed"] or r["counts"]["simulated"]]
        signal.sort(key=lambda r: (-RISK_RANK.get(r["monsoon_risk"], 0), -r["counts"]["observed"], -r["counts"]["simulated"], r["kind"], r["id"]))
        for i, r in enumerate(signal, 1):
            r["rank"] = i
        linked = {e.id for evs in list(seg_events.values()) + list(bridge_events.values()) for e in evs}
        unlinked = [{"id": e.id, "started_at": e.started_at, "near_places": [snap.label(p) for p in e.near_places]}
                    for e in sorted(snap.events.values(), key=lambda e: e.id) if e.data_status == "observed" and e.id not in linked]
        if not any(r["monsoon_risk"] in RISK_RANK for r in rows):
            warnings.append("No segment or bridge carries a monsoon_risk label; ranking uses event history only.")
        if unlinked:
            warnings.append(f"{len(unlinked)} observed event(s) are not linked (BLOCKS/FAILED_IN) to any segment or bridge and therefore "
                            "do not influence any ranking; review and link them before relying on this.")
        if any(r["counts"]["simulated"] for r in signal):
            warnings.append("Simulated closures are demonstration data. They are listed separately and rank below observed history.")
        warnings.append("Observed history is compiled from public reports and is unverified unless its confidence says otherwise.")
        result = {"month": m, "seasons": [{"id": s.id, "name": s.name, "start_month": s.start_month, "end_month": s.end_month}
                                          for s in seasons],
                  "ranking_method": "Lexicographic: (1) monsoon_risk label high>medium>low>unlabelled, (2) number of OBSERVED events "
                                    "in the season window linked to the item, (3) number of SIMULATED events likewise, (4) id. "
                                    "Only items with a label or at least one in-season event are ranked.",
                  "ranked": signal, "applicable_segments": len(seg_ids), "applicable_bridges": len(bridges),
                  "unranked_applicable_items": len(rows) - len(signal), "observed_events_without_links": unlinked}
        expl = (f"Month {m} falls in {', '.join(s.name or s.id for s in seasons)}. {len(signal)} of {len(rows)} applicable segment/bridge "
                "item(s) have a risk label or in-season history and are ranked; the rest are unranked, not low-risk.")
        return self._done("seasonal_risk", inputs, result, expl, self._graph(snap, segment_status={r["id"]: "seasonal_risk" for r in signal if r["kind"] == "segment"}),
                          warnings, self._cypher_only("seasonal-read + in-process ranking"))

    # ------------------------------------------------------------------ 8. draft_alerts
    def _location_text(self, snap, ev) -> str:
        sids = sorted(snap.event_segment_ids(ev.id))
        if not sids:
            return "an unspecified location"
        seg = snap.segments[sids[0]]
        def end(pid):
            p = snap.places.get(pid)
            if p and p.name:
                return p.name
            return f"an unnamed junction ({p.lat:.3f}, {p.lon:.3f})" if p and p.lat is not None else "an unnamed junction"
        road = seg.name or seg.ref or "a road segment"
        extra = f" and {len(sids) - 1} more segment(s)" if len(sids) > 1 else ""
        return f"{road} between {end(seg.from_place_id)} and {end(seg.to_place_id)} (model segment {seg.id}{extra})"

    @staticmethod
    def _alert_text(location, origin, place, hospital_sentence, basis_sentence) -> str:
        def build(h, b):
            parts = [f"SIMULATION \u2014 Road closure reported in the model near {location}.",
                     f"The modeled road network shows no usable road route from {origin} to {place}."]
            parts += [x for x in (h, b) if x]
            parts.append("Verify road conditions and official guidance before travelling.")
            return " ".join(parts)
        text = build(hospital_sentence, basis_sentence)
        if len(text) > 480:
            text = build(hospital_sentence, None)
        if len(text) > 480:
            text = build(None, None)
        return text

    def draft_alerts(self, closure_event_id) -> dict:
        inputs = {"closure_event_id": closure_event_id}
        snap = self._load()
        ev = self._need_event(snap, closure_event_id, "closure_event_id")
        blocked = snap.blocked_map()
        c = self._cutoff(snap, blocked, ev.id)
        affected = self._settlements(snap, c["newly"])
        warnings = self._coverage_warnings(snap, c["cls"]) + [ESTIMATE_NOTE,
                   "Drafts only: nothing is sent and no emergency service is contacted. Affected population is not estimated."]
        if ev.status != "active":
            warnings.append(f"{ev.id} is '{ev.status}', not active; alerts reflect only other active closures.")
        Gr1, _ = A.routing_graph(snap, set(blocked))
        hospitals = [h for h in sorted(snap.hospitals.values(), key=lambda h: h.id) if h.access_place_id in snap.places]
        location = self._location_text(snap, ev)
        origin_name = c["origin"].name or c["origin"].id
        alerts = []
        for pid in affected:
            dist, paths = A.single_source(Gr1, pid)
            best = min(((dist[h.access_place_id], h.id, h) for h in hospitals if h.access_place_id in dist), default=None, key=lambda t: (t[0], t[1]))
            route_segs, hosp_sentence, basis_sentence, hospital_route = [], None, None, None
            if best:
                t, _hid, h = best
                nodes = paths[h.access_place_id]
                r = A.route(Gr1, pid, h.access_place_id)
                route_segs = [s["segment_id"] for s in r["steps"]] if r else []
                hospital_route = {"hospital_id": h.id, "route_nodes": nodes, "route_segments": route_segs, "travel_time_min": round(t, 2)}
                hosp_sentence = (f"The model finds a road route of about {round(t)} min (estimate) to {h.name or 'a recorded hospital'}; "
                                 "its capabilities are unknown.")
                if any(snap.segments[s].travel_time_basis in ("assumed", "mixed") for s in route_segs):
                    basis_sentence = "Times use assumed speeds."
            elif hospitals:
                hosp_sentence = f"The model finds no road route from {snap.label(pid)} to any recorded hospital."
            text = self._alert_text(location, origin_name, snap.label(pid), hosp_sentence, basis_sentence)
            supporting = {"closure_event_id": ev.id, "blocked_segments": blocked, "cut_off_evidence": c["evidence"][pid],
                          "cut_boundary": c["boundary"], "hospital_route": hospital_route,
                          "derived_from": "find_cut_off_places analysis + Dijkstra on the closed network"}
            aid = f"alert-{ev.id}-{pid}"
            self.store.upsert_alert(aid, pid, ev.id, text, json.dumps(supporting, sort_keys=True), self.clock())
            self.store.replace_alert_routes(aid, route_segs)
            alerts.append({"alert_id": aid, "target_place_id": pid, "target_name": snap.places[pid].name, "channel": "sms-draft",
                           "status": "draft", "sent": False, "text": text, "length": len(text), "routed_via_segments": route_segs,
                           "supporting": supporting})
        result = {"closure_event": self._event_summary(ev), "alerts": alerts, "settlements_considered": affected,
                  "disclaimer": "SYNTHETIC DRAFTS. Not sent. Verify with official authorities before any use."}
        expl = (f"Drafted {len(alerts)} alert(s) for settlements newly cut off from Dimapur; each records the graph path that supports it."
                if alerts else "No settlement is newly cut off, so no alert was drafted.")
        graph = self._graph(snap, {p: "alert_target" for p in affected}, {s: "blocked" for s in blocked})
        return self._done("draft_alerts", inputs, result, expl, graph, warnings,
                          self._alg("directed reachability + dijkstra", "Alert nodes are persisted with status 'draft'."))

    # ------------------------------------------------------------------ 10. remember_closure
    def remember_closure(self, event_id) -> dict:
        inputs = {"event_id": event_id}
        snap = self._load()
        ev = self._need_event(snap, event_id)
        warnings, created, existing = [], [], []
        if ev.data_status not in ("simulated", "observed"):
            raise ToolStateError(f"{ev.id} has data_status {ev.data_status!r}; only simulated or observed events can be remembered")
        newly_marked = ev.remembered_at is None
        if newly_marked:
            self.store.mark_remembered(ev.id, self.clock())
        year = _event_year(ev.started_at)
        have = {(b, e) for b, e, _y in snap.failed_in}
        for bid in ev.blocks_bridges:
            if (bid, ev.id) in have:
                existing.append(bid)
            elif year is None:
                warnings.append(f"Event {ev.id} has no usable started_at year, so the failure of {bid} was not recorded.")
            else:
                self.store.record_failure(bid, ev.id, year)
                created.append(bid)
        carried = sorted({b.id for b in snap.bridges.values() if set(b.segment_ids) & set(ev.blocks_segments)} - set(ev.blocks_bridges))
        if carried:
            warnings.append(f"Bridge(s) {carried} sit on segments this closure blocks, but the closure targeted the segment, not the "
                            "bridge, so no bridge failure was recorded (evidence does not support it).")
        if ev.data_status == "simulated":
            warnings.append("This is a SIMULATED event: any failure record links to it and stays distinguishable from observed history.")
        snap2 = self.store.load_snapshot()
        e2 = snap2.events[ev.id]
        result = {"event": self._event_summary(e2), "history_kind": ev.data_status, "already_remembered": not newly_marked,
                  "remembered_at": e2.remembered_at, "failure_records_created": created, "failure_records_already_present": existing,
                  "failure_records_total_for_event": sum(1 for f in snap2.failed_in if f[1] == ev.id),
                  "bridges_on_blocked_segments_not_recorded": carried}
        expl = (f"{ev.id} remembered ({ev.data_status}); {len(created)} new failure record(s), {len(existing)} already present. "
                "Repeating this call changes nothing.")
        return self._done("remember_closure", inputs, result, expl, self._graph(snap2, segment_status={s: "blocked" if ev.status == "active" else "history" for s in snap2.event_segment_ids(ev.id)}),
                          warnings, self._cypher_only("history-write"))

    # ------------------------------------------------------------------ 11. audit_closure
    def audit_closure(self, event_id) -> dict:
        inputs = {"event_id": event_id}
        snap = self._load()
        ev = self._need_event(snap, event_id)
        blocked = snap.blocked_map()
        targets = sorted(snap.event_segment_ids(ev.id))
        others = [e for e in snap.active_events() if e != ev.id]
        problems, checks = [], []

        def check(name, ok, detail, problem_code=None):
            checks.append({"name": name, "ok": ok, "detail": detail})
            if ok is False:
                problems.append({"code": problem_code or name, "message": detail})

        resolved = ev.status == "resolved" and ev.ended_at is not None
        if ev.data_status == "simulated":
            check("event_resolved", resolved, f"status={ev.status}, ended_at={ev.ended_at}", "event_not_resolved")
        else:
            check("event_is_historical_record", True, f"{ev.id} is '{ev.data_status}'; it is not a resolvable closure")
        for sid in targets:
            by = [e for e in blocked.get(sid, [])]
            check(f"segment_{sid}_open", not by, f"blocked by {by}" if by else "not blocked", "segment_still_blocked")
        check("no_other_active_closures", not others, f"other active closures: {others}" if others else "none", "other_active_closures")
        bad_state = [e.id for e in snap.events.values() if e.status == "active" and e.ended_at]
        check("event_states_consistent", not bad_state, f"active events with ended_at set: {bad_state}" if bad_state else "consistent", "inconsistent_event_state")
        # integrity of connections against the segments they claim to follow
        expected = {}
        for s in snap.segments.values():
            if s.from_place_id and s.to_place_id:
                expected[(s.from_place_id, s.to_place_id, s.id, "forward")] = s.travel_time_min
                if s.is_bidirectional:
                    expected[(s.to_place_id, s.from_place_id, s.id, "reverse")] = s.travel_time_min
        actual = {(c.from_id, c.to_id, c.segment_id, c.direction): c.travel_time_min for c in snap.connections}
        order = lambda k: tuple(str(x) for x in k)  # noqa: E731  (keys may contain None for damaged data)
        missing = sorted((k for k in expected if k not in actual), key=order)
        extra = sorted((k for k in actual if k not in expected), key=order)
        differ = sorted((k for k in expected if k in actual and actual[k] != expected[k]), key=order)
        check("connections_match_segments", not (missing or extra or differ),
              f"missing={missing[:3]} unexpected={extra[:3]} weight_mismatch={differ[:3]}" if (missing or extra or differ) else "all connections follow their segments",
              "connection_integrity")
        # baseline fingerprints
        with_fp = [s for s in snap.segments.values() if s.baseline_fp]
        changed = sorted(s.id for s in with_fp if segment_fingerprint(vars(s)) != s.baseline_fp)
        unverifiable = len(snap.segments) - len(with_fp)
        if with_fp:
            check("segments_match_baseline", not changed, f"changed since seeding: {changed[:5]}" if changed else f"{len(with_fp)} segment(s) match their baseline fingerprint", "segment_changed_since_baseline")
        stored = snap.dataset.get("network_fingerprint")
        if stored:
            now_fp = network_fingerprint([vars(s) for s in snap.segments.values()],
                                         [{"segment_id": c.segment_id, "direction": c.direction, "from_place_id": c.from_id,
                                           "to_place_id": c.to_id, "travel_time_min": c.travel_time_min} for c in snap.connections])
            check("network_matches_baseline_fingerprint", now_fp == stored, "network fingerprint equals the one stored at seed time" if now_fp == stored else f"fingerprint {now_fp} != stored {stored}", "network_fingerprint_mismatch")
        verifiable = bool(with_fp) and bool(stored) and unverifiable == 0
        warnings = []
        if not verifiable:
            warnings.append("Baseline fingerprints are missing for part or all of the network (data seeded before Phase 2). Re-run "
                            "'python data\\fetch_osm.py normalize' and 'python data\\seed.py' so a return to baseline can be verified.")
        Gnow, _ = A.topology_graph(snap, set(blocked))
        Gall, _ = A.topology_graph(snap, set())
        origin = snap.reference("dimapur")
        still_cut = sorted(A.reach(Gall, origin.id) - A.reach(Gnow, origin.id)) if origin else []
        if still_cut:
            warnings.append(f"{len(still_cut)} place(s) are still unreachable from Dimapur because of active closures {snap.active_events()}.")
        if problems:
            returned = False
        elif not verifiable:
            returned = None
        else:
            returned = True
        result = {"event": self._event_summary(ev), "resolved": resolved if ev.data_status == "simulated" else None,
                  "returned_to_baseline": returned,
                  "returned_to_baseline_meaning": "True only if the event is resolved, no other closure is active, every connection "
                                                  "follows its segment, and all baseline fingerprints match. None = cannot be verified.",
                  "target_segments": targets, "other_active_closures": others, "checks": checks, "discrepancies": problems,
                  "places_unreachable_from_dimapur_due_to_active_closures": still_cut, "blocked_segments_now": blocked}
        expl = ("The graph is back to its baseline." if returned else
                "The graph is NOT at baseline: " + "; ".join(p["message"] for p in problems) if returned is False else
                "Return to baseline cannot be verified (no baseline fingerprints).")
        return self._done("audit_closure", inputs, result, expl, self._graph(snap, segment_status={s: "blocked" for s in blocked}),
                          warnings, self._cypher_only("audit-read + in-process comparison"))

    # ------------------------------------------------------------------ capability probe
    def probe_capabilities(self) -> dict:
        self.store.reset_log()
        procs = self.store.procedures()
        algo = sorted(n for n, _ in procs if n.startswith("algo."))
        result = {"algo_procedures": algo, "native_weighted_paths": "algo.SPpaths" in algo,
                  "native_betweenness": "algo.betweenness" in algo, "native_wcc": "algo.WCC" in algo,
                  "decision": "NetworkX on an exported graph is used for every algorithm; see algorithm.implementation."}
        return self._done("probe_capabilities", {}, result, f"{len(algo)} algo.* procedure(s) installed.", {"nodes": [], "edges": []},
                          [], self._cypher_only("dbms.procedures"))
