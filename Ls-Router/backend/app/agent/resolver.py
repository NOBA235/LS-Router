"""Natural-language event parser and corridor location resolver.

Resolves user descriptions against the actual seeded graph in FalkorDB (read via `FalkorStore`).
Enforces the Section 10 non-negotiable rule:
- Nearest-neighbor search may use geographic coordinates, but proximity alone is not sufficient
  to establish that an event occurred on a particular road.
- Never silently snap an ambiguous event (multiple matching segments, coordinate-only proximity,
  or unverified kilometre/chainage markers) to an arbitrary segment.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from ..graph import algorithms as A
from ..graph.errors import ToolInputError
from ..graph.snapshot import Bridge, Place, Segment, Snapshot
from ..graph.tools import FALKORDB_NATIVE_NOTE, MONTH_NAMES, _text


def _fold(text: str | None) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(a)))


def _point_to_segment_m(
    plat: float, plon: float, alat: float, alon: float, blat: float, blon: float
) -> float:
    """Approximate planar projection distance in metres from point P to segment AB."""
    mid_lat = math.radians((plat + alat + blat) / 3.0)
    scale_x = 111_320.0 * math.cos(mid_lat)
    scale_y = 110_574.0
    px, py = (plon - alon) * scale_x, (plat - alat) * scale_y
    bx, by = (blon - alon) * scale_x, (blat - alat) * scale_y
    denom = bx * bx + by * by
    if denom <= 1e-9:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * bx + py * by) / denom))
    return math.hypot(px - t * bx, py - t * by)


_KM_PATTERNS = (
    re.compile(
        r"\b(?:chainage(?:\s+km)?|kilomet(?:re|er)s?|kms?|milestone)\s*(?:no\.?|number|post|marker)?\s*[:=#-]?\s*"
        r"(\d+(?:\.\d+)?(?:\+\d+)?)\b",
        re.I,
    ),
    re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:kilomet(?:re|er)s?|kms?)\b", re.I),
)

_COORD_PATTERN = re.compile(
    r"(?:lat(?:itude)?\s*[:=]?\s*)?(-?\d{1,2}\.\d{2,})\s*[,;/\s]\s*(?:lon(?:gitude)?\s*[:=]?\s*)?(-?\d{1,3}\.\d{2,})",
    re.I,
)

_NH_PATTERN = re.compile(r"\b(?:nh|national\s+highway)[\s-]*(\d+)\b", re.I)
_SH_PATTERN = re.compile(r"\b(?:sh|state\s+highway)[\s-]*(\d+)\b", re.I)

_BETWEEN_PATTERNS = (
    re.compile(r"\bbetween\s+([a-z0-9\u0080-\uffff _-]+?)\s+and\s+([a-z0-9\u0080-\uffff _.-]+?)(?:$|[,.?;!]|(?:\s+on\b|\s+near\b|\s+at\b|\s+in\b))", re.I),
    re.compile(r"\bfrom\s+([a-z0-9\u0080-\uffff _-]+?)\s+to\s+([a-z0-9\u0080-\uffff _.-]+?)(?:$|[,.?;!]|(?:\s+on\b|\s+near\b|\s+at\b|\s+avoiding\b))", re.I),
)

_EVENT_ID_PATTERN = re.compile(r"\b((?:sim|obs)-[a-z0-9-]+)\b", re.I)


@dataclass(frozen=True)
class ParsedEvent:
    raw_text: str
    cause: str
    intent: str
    road_refs: list[str] = field(default_factory=list)
    km_marker: float | None = None
    coordinates: dict[str, float] | None = None
    bridge_mentioned: bool = False
    between_pair: list[str] | None = None
    month: int | None = None
    explicit_event_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_km(text: str) -> float | None:
    for pat in _KM_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(1)
            if "+" in raw:
                km_part, m_part = raw.split("+", 1)
                return round(float(km_part) + float(m_part) / 1000.0, 3)
            return float(raw)
    return None


def _parse_month(text: str) -> int | None:
    low = _fold(text)
    for token in re.findall(r"[a-z]+", low):
        if len(token) >= 3 and token[:3] in MONTH_NAMES:
            # Avoid matching common English word "may" unless capitalized or adjacent to month/risk/in
            if token == "may" and not re.search(r"\b(?:in\s+may|may\s+monsoon|month\s+(?:of\s+)?may)\b", low):
                continue
            return MONTH_NAMES[token[:3]]
    m = re.search(r"\bmonth\s*[:=]?\s*(\d{1,2})\b", low)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(1))
    return None


def parse_event(description: str, lat: float | None = None, lon: float | None = None) -> ParsedEvent:
    """Deterministically parse a natural-language event description or query."""
    text = _text(description, "description", max_len=1000)
    low = _fold(text)

    # Cause classification
    if any(w in low for w in ("flood", "washout", "washed away", "washed out", "submerged", "inundat")):
        cause = "flood"
    elif any(w in low for w in ("landslide", "mudslide", "rockfall", "rockslide", "earthslip", "debris", "slip", "slide")):
        cause = "landslide"
    elif any(w in low for w in ("bridge collapse", "bridge failure", "bridge damaged", "bridge out", "bridge washed")):
        cause = "bridge_failure"
    else:
        cause = "closure"

    # Intent classification
    event_ids = [m.group(1) for m in _EVENT_ID_PATTERN.finditer(text)]
    if any(w in low for w in ("clear closure", "resolve closure", "reopen", "clear event", "resolve event")) or (
        event_ids and any(w in low for w in ("clear", "resolve"))
    ):
        intent = "clear_closure"
    elif any(w in low for w in ("audit closure", "audit event", "verify baseline", "returned to baseline")) or (
        event_ids and "audit" in low
    ):
        intent = "audit_closure"
    elif any(w in low for w in ("remember closure", "remember event", "record failure", "persist closure")) or (
        event_ids and "remember" in low
    ):
        intent = "remember_closure"
    elif (
        any(w in low for w in ("weak link", "weak links", "weak point", "betweenness", "centrality", "bottleneck"))
        and not any(w in low for w in ("landslide", "mudslide", "rockfall", "flood", "washout", "closure", "blocked", "closed"))
    ):
        intent = "weak_links"
    elif (
        any(w in low for w in ("seasonal risk", "monsoon risk", "seasonal ranking", "elevated monsoon risk"))
        and not any(w in low for w in ("landslide", "mudslide", "rockfall", "closure", "blocked", "closed"))
    ):
        intent = "seasonal_risk"
    elif (
        any(w in low for w in ("reroute", "explain path", "fastest route", "fastest path", "alternate route from", "route from"))
        and not any(w in low for w in ("landslide", "mudslide", "rockfall", "flood", "washout", "block"))
    ):
        intent = "reroute"
    else:
        intent = "closure_impact"

    # Road references
    road_refs: list[str] = []
    for m in _NH_PATTERN.finditer(text):
        ref = f"NH {m.group(1)}"
        if ref not in road_refs:
            road_refs.append(ref)
    for m in _SH_PATTERN.finditer(text):
        ref = f"SH {m.group(1)}"
        if ref not in road_refs:
            road_refs.append(ref)

    # Kilometre / chainage marker
    km_marker = _parse_km(text)

    # Coordinates
    coords: dict[str, float] | None = None
    if lat is not None or lon is not None:
        if (
            lat is None
            or lon is None
            or isinstance(lat, bool)
            or isinstance(lon, bool)
            or not isinstance(lat, (int, float))
            or not isinstance(lon, (int, float))
            or not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180)
        ):
            raise ToolInputError("lat and lon must both be finite numbers within [-90, 90] and [-180, 180]")
        coords = {"lat": float(lat), "lon": float(lon)}
    else:
        cm = _COORD_PATTERN.search(text)
        if cm:
            clat, clon = float(cm.group(1)), float(cm.group(2))
            if -90 <= clat <= 90 and -180 <= clon <= 180:
                coords = {"lat": clat, "lon": clon}

    bridge_mentioned = bool(re.search(r"\b(?:bridge|bridges|culvert|viaduct|suspension\s+bridge)\b", low))

    between_pair: list[str] | None = None
    for pat in _BETWEEN_PATTERNS:
        bm = pat.search(text)
        if bm:
            between_pair = [bm.group(1).strip(), bm.group(2).strip()]
            break

    month = _parse_month(text)

    return ParsedEvent(
        raw_text=text,
        cause=cause,
        intent=intent,
        road_refs=road_refs,
        km_marker=km_marker,
        coordinates=coords,
        bridge_mentioned=bridge_mentioned,
        between_pair=between_pair,
        month=month,
        explicit_event_ids=event_ids,
    )


class LocationResolver:
    """Resolves a natural-language event against a `FalkorStore` snapshot."""

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _place_summary(p: Place) -> dict:
        return {
            "id": p.id,
            "name": p.name,
            "type": p.type,
            "lat": p.lat,
            "lon": p.lon,
            "confidence": p.confidence,
            "data_status": p.data_status,
            "source_url": p.source_url,
        }

    @staticmethod
    def _segment_candidate(
        snap: Snapshot,
        seg: Segment,
        reasons: list[str],
        *,
        rank: int = 1,
        cumulative_km: tuple[float, float] | None = None,
        distance_m: float | None = None,
    ) -> dict:
        fp = snap.places.get(seg.from_place_id)
        tp = snap.places.get(seg.to_place_id)
        bridges = sorted(b.id for b in snap.bridges.values() if seg.id in b.segment_ids)
        out = {
            "rank": rank,
            "segment_id": seg.id,
            "name": seg.name,
            "ref": seg.ref,
            "highway": seg.highway,
            "on_nh29": seg.on_nh29,
            "from_place_id": seg.from_place_id,
            "from_place_name": fp.name if fp else None,
            "to_place_id": seg.to_place_id,
            "to_place_name": tp.name if tp else None,
            "length_km": seg.length_km,
            "travel_time_min": seg.travel_time_min,
            "travel_time_basis": seg.travel_time_basis,
            "is_bidirectional": seg.is_bidirectional,
            "carries_bridges": bridges,
            "confidence": seg.confidence,
            "data_status": seg.data_status,
            "source_url": seg.source_url,
            "match_reasons": reasons,
        }
        if cumulative_km is not None:
            out["cumulative_km_from_dimapur"] = [round(cumulative_km[0], 3), round(cumulative_km[1], 3)]
        if distance_m is not None:
            out["distance_m"] = round(distance_m, 1)
        return out

    @staticmethod
    def _bridge_candidate(
        br: Bridge,
        reasons: list[str],
        *,
        rank: int = 1,
        distance_m: float | None = None,
    ) -> dict:
        out = {
            "rank": rank,
            "bridge_id": br.id,
            "name": br.name,
            "road_name": br.road_name,
            "lat": br.lat,
            "lon": br.lon,
            "segment_ids": list(br.segment_ids),
            "monsoon_risk": br.monsoon_risk,
            "confidence": br.confidence,
            "source_url": br.source_url,
            "match_reasons": reasons,
        }
        if distance_m is not None:
            out["distance_m"] = round(distance_m, 1)
        return out

    @staticmethod
    def _match_place_in_fragment(snap: Snapshot, fragment: str) -> Place | None:
        frag_fold = _fold(fragment)
        if not frag_fold:
            return None
        # 1. Exact ID or exact folded name match
        for p in sorted(snap.places.values(), key=lambda x: x.id):
            if _fold(p.id) == frag_fold or (p.name and _fold(p.name) == frag_fold):
                return p
        # 2. Substring match on multi-character name or ID inside fragment
        for p in sorted(snap.places.values(), key=lambda x: -(len(x.name or x.id))):
            if p.name and len(_fold(p.name)) >= 3 and re.search(rf"\b{re.escape(_fold(p.name))}\b", frag_fold):
                return p
            if len(p.id) >= 2 and re.search(rf"\b{re.escape(_fold(p.id))}\b", frag_fold):
                return p
        # 3. Single-letter place IDs (e.g. D, K, A, B, C, E in synthetic fixtures) when fragment is just that token
        m = re.match(r"^(?:place|village|town|node|junction)?\s*([a-z0-9])$", frag_fold)
        if m:
            tok = m.group(1)
            for p in sorted(snap.places.values(), key=lambda x: x.id):
                if _fold(p.id) == tok:
                    return p
        return None

    def _find_mentioned_places(self, snap: Snapshot, text: str, parsed: ParsedEvent) -> list[Place]:
        """Find Place nodes mentioned by name or ID in `text`."""
        found: dict[str, Place] = {}
        low = _fold(text)

        # First check explicit between/from-to pair if present
        if parsed.between_pair:
            for part in parsed.between_pair:
                p = self._match_place_in_fragment(snap, part)
                if p:
                    found[p.id] = p

        # Match full place names (longest first)
        for p in sorted(snap.places.values(), key=lambda x: (-(len(x.name or "")), x.id)):
            if not p.name:
                continue
            pname = _fold(p.name)
            if len(pname) >= 2 and re.search(rf"\b{re.escape(pname)}\b", low):
                found[p.id] = p

        # Match place IDs
        for p in sorted(snap.places.values(), key=lambda x: (-len(x.id), x.id)):
            pid_low = _fold(p.id)
            if len(pid_low) >= 2:
                if re.search(rf"\b{re.escape(pid_low)}\b", low):
                    found[p.id] = p
            elif len(pid_low) == 1:
                # 1-char IDs (like A, B, C, D, E, K): match only after prepositions/labels or uppercase token not at index 0
                pat = rf"(?:\b(?:at|near|to|from|between|and|place|village|town|node|junction)\s+{re.escape(p.id)}\b)"
                if re.search(pat, text):
                    found[p.id] = p

        return list(found.values())

    @staticmethod
    def _cumulative_km_map(snap: Snapshot, nh29_only: bool = False) -> tuple[dict[str, tuple[float, float]], float]:
        """Return (segment_id -> (start_km, end_km) from Dimapur, max_corridor_km)."""
        origin = snap.reference("dimapur")
        if origin is None:
            return {}, 0.0

        # Build length-weighted graph from Dimapur; prefer NH29 segments for the trunk distance spine
        import networkx as nx

        G = nx.DiGraph()
        for pid in sorted(snap.places):
            G.add_node(pid)
        for c in snap.connections:
            seg = snap.segments.get(c.segment_id)
            if seg is None or not A.weight_ok(seg.length_km):
                continue
            # Slight penalty for non-NH29 edges so shortest-distance tree follows NH29 along the main spine
            penalty = 1.0 if seg.on_nh29 else 1.001
            w = float(seg.length_km) * penalty
            cur = G.get_edge_data(c.from_id, c.to_id)
            if cur is None or w < cur["w"]:
                G.add_edge(c.from_id, c.to_id, w=w, raw_km=float(seg.length_km), segment_id=seg.id)

        if origin.id not in G:
            return {}, 0.0
        _, paths = nx.single_source_dijkstra(G, origin.id, weight="w")
        place_km: dict[str, float] = {origin.id: 0.0}
        for pid, nodes in paths.items():
            place_km[pid] = sum(G[u][v]["raw_km"] for u, v in zip(nodes, nodes[1:]))

        seg_intervals: dict[str, tuple[float, float]] = {}
        max_km = 0.0
        for seg in sorted(snap.segments.values(), key=lambda s: s.id):
            if nh29_only and not seg.on_nh29:
                continue
            if not A.weight_ok(seg.length_km):
                continue
            du = place_km.get(seg.from_place_id)
            dv = place_km.get(seg.to_place_id)
            if du is None and dv is None:
                continue
            start = min(x for x in (du, dv) if x is not None)
            end = start + float(seg.length_km)
            seg_intervals[seg.id] = (start, end)
            if seg.on_nh29 and end > max_km:
                max_km = end
        if max_km == 0.0 and seg_intervals:
            max_km = max(end for _, end in seg_intervals.values())
        return seg_intervals, round(max_km, 3)

    @staticmethod
    def _interval_distance(km: float, interval: tuple[float, float]) -> float:
        a, b = interval
        if a <= km <= b:
            return 0.0
        return min(abs(km - a), abs(km - b))

    @staticmethod
    def _segment_distance_m(snap: Snapshot, seg: Segment, lat: float, lon: float) -> float | None:
        pa = snap.places.get(seg.from_place_id)
        pb = snap.places.get(seg.to_place_id)
        if pa and pb and pa.lat is not None and pa.lon is not None and pb.lat is not None and pb.lon is not None:
            return _point_to_segment_m(lat, lon, pa.lat, pa.lon, pb.lat, pb.lon)
        if pa and pa.lat is not None and pa.lon is not None:
            return _haversine_m(lat, lon, pa.lat, pa.lon)
        if pb and pb.lat is not None and pb.lon is not None:
            return _haversine_m(lat, lon, pb.lat, pb.lon)
        return None

    # ------------------------------------------------------------------ main entry
    def resolve_location(
        self,
        description: str,
        lat: float | None = None,
        lon: float | None = None,
        target_segment_id: str | None = None,
        target_bridge_id: str | None = None,
        max_candidates: int = 5,
    ) -> dict:
        """Resolve a natural-language event description against the seeded corridor graph.

        Returns the standard tool envelope `{tool, result, cypher, params, explanation, graph, warnings, algorithm}`.
        """
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 50:
            raise ToolInputError("max_candidates must be an integer between 1 and 50")

        parsed = parse_event(description, lat=lat, lon=lon)
        inputs = {
            "description": description,
            "lat": lat,
            "lon": lon,
            "target_segment_id": target_segment_id,
            "target_bridge_id": target_bridge_id,
            "max_candidates": max_candidates,
        }

        self.store.reset_log()
        snap = self.store.load_snapshot()
        warnings: list[str] = []
        low = _fold(parsed.raw_text)
        want_nh29 = "NH 29" in parsed.road_refs

        # Check related historical observed events for context
        matched_places = self._find_mentioned_places(snap, parsed.raw_text, parsed)
        matched_place_ids = {p.id for p in matched_places}
        related_history = []
        for ev in sorted(snap.events.values(), key=lambda e: e.id):
            if ev.data_status != "observed":
                continue
            loc_fold = _fold(ev.location_text or "")
            if (set(ev.near_places) & matched_place_ids) or any(
                p.name and _fold(p.name) in loc_fold for p in matched_places
            ):
                related_history.append({
                    "id": ev.id,
                    "started_at": ev.started_at,
                    "cause": ev.cause,
                    "location_text": ev.location_text,
                    "confidence": ev.confidence,
                    "source_url": ev.source_url,
                    "data_status": ev.data_status,
                })

        # ------------------------------------------------------------------
        # 0. Caller explicitly confirmed target_segment_id and/or target_bridge_id
        # ------------------------------------------------------------------
        if target_segment_id is not None or target_bridge_id is not None:
            t_seg_id = _text(target_segment_id, "target_segment_id", 200) if target_segment_id is not None else None
            t_br_id = _text(target_bridge_id, "target_bridge_id", 200) if target_bridge_id is not None else None

            if t_br_id is not None and t_br_id not in snap.bridges:
                raise ToolInputError(f"target_bridge_id: unknown bridge {t_br_id!r}")
            if t_seg_id is not None and t_seg_id not in snap.segments:
                raise ToolInputError(f"target_segment_id: unknown road segment {t_seg_id!r}")

            if t_br_id is not None and t_seg_id is None:
                br = snap.bridges[t_br_id]
                if len(br.segment_ids) == 1:
                    t_seg_id = br.segment_ids[0]
                elif len(br.segment_ids) > 1:
                    c_segs = [
                        self._segment_candidate(
                            snap,
                            snap.segments[sid],
                            [f"carries bridge {br.id} ({br.name or 'unnamed'})"],
                            rank=i,
                        )
                        for i, sid in enumerate(br.segment_ids[:max_candidates], 1)
                        if sid in snap.segments
                    ]
                    c_brs = [self._bridge_candidate(br, ["explicit target_bridge_id"])]
                    return self._finish(
                        snap,
                        inputs,
                        parsed,
                        status="ambiguous",
                        selected=None,
                        candidate_segments=c_segs,
                        candidate_bridges=c_brs,
                        matched_places=matched_places,
                        related_history=related_history,
                        warnings=warnings + [f"Bridge {br.id} sits on {len(br.segment_ids)} segments; specify target_segment_id."],
                        clarification=f"Bridge {br.id} spans multiple segments ({list(br.segment_ids)}). Which segment should be closed?",
                        explanation=f"Bridge {br.id} sits on {len(br.segment_ids)} road segments; clarification is required.",
                    )
                else:
                    raise ToolInputError(f"bridge {t_br_id} is not ON any road segment")

            assert t_seg_id is not None
            seg = snap.segments[t_seg_id]
            if t_br_id is not None and t_seg_id not in snap.bridges[t_br_id].segment_ids:
                raise ToolInputError(f"bridge {t_br_id} is not ON segment {t_seg_id}")

            reasons = ["explicitly confirmed target_segment_id"]
            if t_br_id:
                reasons.append(f"explicitly confirmed target_bridge_id={t_br_id}")
            if seg.confidence != "verified":
                warnings.append(
                    f"Segment {seg.id} is '{seg.confidence}' (derived from OpenStreetMap, not hand-verified)."
                )
            c_seg = self._segment_candidate(snap, seg, reasons, rank=1)
            c_brs = (
                [self._bridge_candidate(snap.bridges[t_br_id], ["explicitly confirmed target_bridge_id"])]
                if t_br_id
                else [
                    self._bridge_candidate(snap.bridges[bid], [f"sits ON segment {seg.id}"], rank=i)
                    for i, bid in enumerate(c_seg["carries_bridges"], 1)
                ]
            )
            selected = {
                "segment_id": seg.id,
                "bridge_id": t_br_id,
                "segment_name": seg.name,
                "ref": seg.ref,
                "from_place_id": seg.from_place_id,
                "to_place_id": seg.to_place_id,
                "confidence": seg.confidence,
                "source_url": seg.source_url,
                "match_reasons": reasons,
            }
            return self._finish(
                snap,
                inputs,
                parsed,
                status="resolved",
                selected=selected,
                candidate_segments=[c_seg],
                candidate_bridges=c_brs,
                matched_places=matched_places,
                related_history=related_history,
                warnings=warnings,
                clarification=None,
                explanation=f"Target segment {seg.id} ({snap.label(seg.from_place_id)} -> {snap.label(seg.to_place_id)}) confirmed.",
            )

        # ------------------------------------------------------------------
        # 1. Explicit segment ID or bridge ID/name mentioned in text
        # ------------------------------------------------------------------
        explicit_segs = [
            s for s in sorted(snap.segments.values(), key=lambda x: x.id)
            if re.search(rf"\b{re.escape(_fold(s.id))}\b", low)
        ]
        explicit_bridges = [
            b for b in sorted(snap.bridges.values(), key=lambda x: x.id)
            if re.search(rf"\b{re.escape(_fold(b.id))}\b", low)
            or (b.name and len(_fold(b.name)) >= 3 and re.search(rf"\b{re.escape(_fold(b.name))}\b", low))
        ]

        if explicit_segs or explicit_bridges:
            seg_reasons: dict[str, list[str]] = {}
            for s in explicit_segs:
                seg_reasons.setdefault(s.id, []).append(f"segment id '{s.id}' named in event description")
            for b in explicit_bridges:
                label = f"bridge '{b.name}' ({b.id})" if b.name else f"bridge id '{b.id}'"
                for sid in b.segment_ids:
                    if sid in snap.segments:
                        seg_reasons.setdefault(sid, []).append(f"carries {label} named in event description")

            c_segs = [
                self._segment_candidate(snap, snap.segments[sid], r, rank=i)
                for i, (sid, r) in enumerate(sorted(seg_reasons.items())[:max_candidates], 1)
            ]
            c_brs = [
                self._bridge_candidate(b, [f"bridge '{b.name or b.id}' matched in text"], rank=i)
                for i, b in enumerate(explicit_bridges[:max_candidates], 1)
            ]
            if len(seg_reasons) == 1:
                sid = next(iter(seg_reasons))
                seg = snap.segments[sid]
                chosen_bridge = explicit_bridges[0].id if len(explicit_bridges) == 1 and sid in explicit_bridges[0].segment_ids else None
                if seg.confidence != "verified":
                    warnings.append(f"Segment {seg.id} is '{seg.confidence}' (not hand-verified).")
                selected = {
                    "segment_id": seg.id,
                    "bridge_id": chosen_bridge,
                    "segment_name": seg.name,
                    "ref": seg.ref,
                    "from_place_id": seg.from_place_id,
                    "to_place_id": seg.to_place_id,
                    "confidence": seg.confidence,
                    "source_url": seg.source_url,
                    "match_reasons": seg_reasons[sid],
                }
                return self._finish(
                    snap,
                    inputs,
                    parsed,
                    status="resolved",
                    selected=selected,
                    candidate_segments=c_segs,
                    candidate_bridges=c_brs,
                    matched_places=matched_places,
                    related_history=related_history,
                    warnings=warnings,
                    clarification=None,
                    explanation=f"Resolved unambiguously to segment {seg.id}" + (f" and bridge {chosen_bridge}." if chosen_bridge else "."),
                )
            return self._finish(
                snap,
                inputs,
                parsed,
                status="ambiguous",
                selected=None,
                candidate_segments=c_segs,
                candidate_bridges=c_brs,
                matched_places=matched_places,
                related_history=related_history,
                warnings=warnings,
                clarification=f"Multiple segments matched ({[c['segment_id'] for c in c_segs]}). Specify which segment_id to close.",
                explanation=f"Matched {len(c_segs)} candidate segments by explicit identifier/bridge name; clarification required.",
            )

        # ------------------------------------------------------------------
        # 2. Two endpoints matched ("between X and Y" or two places in text)
        # ------------------------------------------------------------------
        if len(matched_places) >= 2:
            p1, p2 = matched_places[0], matched_places[1]
            direct = [
                s for s in sorted(snap.segments.values(), key=lambda x: x.id)
                if {s.from_place_id, s.to_place_id} == {p1.id, p2.id}
            ]
            if want_nh29 and any(s.on_nh29 for s in direct):
                direct = [s for s in direct if s.on_nh29]
            if parsed.bridge_mentioned and any(
                any(s.id in b.segment_ids for b in snap.bridges.values()) for s in direct
            ):
                direct = [s for s in direct if any(s.id in b.segment_ids for b in snap.bridges.values())]

            if direct:
                c_segs = [
                    self._segment_candidate(
                        snap,
                        s,
                        [f"directly connects {snap.label(p1.id)} ({p1.id}) and {snap.label(p2.id)} ({p2.id})"]
                        + (["on NH 29"] if want_nh29 and s.on_nh29 else []),
                        rank=i,
                    )
                    for i, s in enumerate(direct[:max_candidates], 1)
                ]
                c_brs = [
                    self._bridge_candidate(b, [f"sits ON segment between {snap.label(p1.id)} and {snap.label(p2.id)}"], rank=i)
                    for i, b in enumerate(
                        [b for b in sorted(snap.bridges.values(), key=lambda x: x.id) if any(s.id in b.segment_ids for s in direct)][:max_candidates],
                        1,
                    )
                ]
                if len(direct) == 1 and parsed.km_marker is None:
                    seg = direct[0]
                    chosen_bridge = c_brs[0]["bridge_id"] if parsed.bridge_mentioned and len(c_brs) == 1 else None
                    if seg.confidence != "verified":
                        warnings.append(f"Segment {seg.id} is '{seg.confidence}' (not hand-verified).")
                    selected = {
                        "segment_id": seg.id,
                        "bridge_id": chosen_bridge,
                        "segment_name": seg.name,
                        "ref": seg.ref,
                        "from_place_id": seg.from_place_id,
                        "to_place_id": seg.to_place_id,
                        "confidence": seg.confidence,
                        "source_url": seg.source_url,
                        "match_reasons": c_segs[0]["match_reasons"],
                    }
                    return self._finish(
                        snap,
                        inputs,
                        parsed,
                        status="resolved",
                        selected=selected,
                        candidate_segments=c_segs,
                        candidate_bridges=c_brs,
                        matched_places=matched_places,
                        related_history=related_history,
                        warnings=warnings,
                        clarification=None,
                        explanation=f"Resolved to segment {seg.id} directly connecting {snap.label(p1.id)} and {snap.label(p2.id)}.",
                    )
                return self._finish(
                    snap,
                    inputs,
                    parsed,
                    status="ambiguous",
                    selected=None,
                    candidate_segments=c_segs,
                    candidate_bridges=c_brs,
                    matched_places=matched_places,
                    related_history=related_history,
                    warnings=warnings,
                    clarification=f"Multiple parallel segments connect {snap.label(p1.id)} and {snap.label(p2.id)}: {[c['segment_id'] for c in c_segs]}. Please confirm which segment_id is blocked.",
                    explanation=f"Found {len(c_segs)} parallel segments between {snap.label(p1.id)} and {snap.label(p2.id)}; clarification required.",
                )

            # Not directly adjacent: check baseline path between p1 and p2
            Gr, _ = A.routing_graph(snap, set())
            path_r = A.route(Gr, p1.id, p2.id) or A.route(Gr, p2.id, p1.id)
            path_segs: list[Segment] = []
            if path_r:
                path_segs = [snap.segments[st["segment_id"]] for st in path_r["steps"] if st["segment_id"] in snap.segments]
            else:
                Gt, _ = A.topology_graph(snap, set())
                hp = A.hop_path(Gt, p1.id, p2.id) or A.hop_path(Gt, p2.id, p1.id)
                if hp:
                    for u, v in zip(hp, hp[1:]):
                        for sid in Gt[u][v]["segment_ids"]:
                            if sid in snap.segments:
                                path_segs.append(snap.segments[sid])

            if want_nh29 and any(s.on_nh29 for s in path_segs):
                path_segs = [s for s in path_segs if s.on_nh29]
            if parsed.bridge_mentioned and any(
                any(s.id in b.segment_ids for b in snap.bridges.values()) for s in path_segs
            ):
                path_segs = [s for s in path_segs if any(s.id in b.segment_ids for b in snap.bridges.values())]

            if path_segs:
                c_segs = [
                    self._segment_candidate(
                        snap,
                        s,
                        [f"on corridor path between {snap.label(p1.id)} ({p1.id}) and {snap.label(p2.id)} ({p2.id})"]
                        + (["carries a bridge"] if any(s.id in b.segment_ids for b in snap.bridges.values()) else []),
                        rank=i,
                    )
                    for i, s in enumerate(path_segs[:max_candidates], 1)
                ]
                c_brs = [
                    self._bridge_candidate(b, [f"on corridor path between {snap.label(p1.id)} and {snap.label(p2.id)}"], rank=i)
                    for i, b in enumerate(
                        [b for b in sorted(snap.bridges.values(), key=lambda x: x.id) if any(s.id in b.segment_ids for s in path_segs)][:max_candidates],
                        1,
                    )
                ]
                if len(path_segs) == 1 and parsed.km_marker is None:
                    seg = path_segs[0]
                    chosen_bridge = c_brs[0]["bridge_id"] if parsed.bridge_mentioned and len(c_brs) == 1 else None
                    if seg.confidence != "verified":
                        warnings.append(f"Segment {seg.id} is '{seg.confidence}' (not hand-verified).")
                    selected = {
                        "segment_id": seg.id,
                        "bridge_id": chosen_bridge,
                        "segment_name": seg.name,
                        "ref": seg.ref,
                        "from_place_id": seg.from_place_id,
                        "to_place_id": seg.to_place_id,
                        "confidence": seg.confidence,
                        "source_url": seg.source_url,
                        "match_reasons": c_segs[0]["match_reasons"],
                    }
                    return self._finish(
                        snap,
                        inputs,
                        parsed,
                        status="resolved",
                        selected=selected,
                        candidate_segments=c_segs,
                        candidate_bridges=c_brs,
                        matched_places=matched_places,
                        related_history=related_history,
                        warnings=warnings,
                        clarification=None,
                        explanation=f"Resolved to the single matching segment {seg.id} between {snap.label(p1.id)} and {snap.label(p2.id)}.",
                    )
                return self._finish(
                    snap,
                    inputs,
                    parsed,
                    status="ambiguous",
                    selected=None,
                    candidate_segments=c_segs,
                    candidate_bridges=c_brs,
                    matched_places=matched_places,
                    related_history=related_history,
                    warnings=warnings,
                    clarification=(
                        f"The stretch between {snap.label(p1.id)} and {snap.label(p2.id)} spans {len(path_segs)} road segments "
                        f"({[c['segment_id'] for c in c_segs]}). Please specify which segment_id is blocked."
                    ),
                    explanation=f"Found {len(path_segs)} segments along the path between {snap.label(p1.id)} and {snap.label(p2.id)}; clarification required.",
                )

        # ------------------------------------------------------------------
        # 3. Specific road name in text (e.g. "Synthetic Spur", "Synthetic Alt Road")
        # ------------------------------------------------------------------
        named_road_segs = [
            s for s in sorted(snap.segments.values(), key=lambda x: x.id)
            if s.name and len(_fold(s.name)) >= 4 and re.search(rf"\b{re.escape(_fold(s.name))}\b", low)
        ]
        if named_road_segs and parsed.km_marker is None:
            if len(matched_places) == 1:
                p = matched_places[0]
                incident = [s for s in named_road_segs if p.id in (s.from_place_id, s.to_place_id)]
                if incident:
                    named_road_segs = incident
            if parsed.bridge_mentioned:
                with_br = [s for s in named_road_segs if any(s.id in b.segment_ids for b in snap.bridges.values())]
                if with_br:
                    named_road_segs = with_br

            c_segs = [
                self._segment_candidate(snap, s, [f"road name '{s.name}' matched in event description"], rank=i)
                for i, s in enumerate(named_road_segs[:max_candidates], 1)
            ]
            c_brs = [
                self._bridge_candidate(b, [f"sits on matched road '{named_road_segs[0].name}'"], rank=i)
                for i, b in enumerate(
                    [b for b in sorted(snap.bridges.values(), key=lambda x: x.id) if any(s.id in b.segment_ids for s in named_road_segs)][:max_candidates],
                    1,
                )
            ]
            if len(named_road_segs) == 1:
                seg = named_road_segs[0]
                chosen_bridge = c_brs[0]["bridge_id"] if parsed.bridge_mentioned and len(c_brs) == 1 else None
                if seg.confidence != "verified":
                    warnings.append(f"Segment {seg.id} is '{seg.confidence}' (not hand-verified).")
                selected = {
                    "segment_id": seg.id,
                    "bridge_id": chosen_bridge,
                    "segment_name": seg.name,
                    "ref": seg.ref,
                    "from_place_id": seg.from_place_id,
                    "to_place_id": seg.to_place_id,
                    "confidence": seg.confidence,
                    "source_url": seg.source_url,
                    "match_reasons": c_segs[0]["match_reasons"],
                }
                return self._finish(
                    snap,
                    inputs,
                    parsed,
                    status="resolved",
                    selected=selected,
                    candidate_segments=c_segs,
                    candidate_bridges=c_brs,
                    matched_places=matched_places,
                    related_history=related_history,
                    warnings=warnings,
                    clarification=None,
                    explanation=f"Resolved to segment {seg.id} by road name '{seg.name}'.",
                )
            return self._finish(
                snap,
                inputs,
                parsed,
                status="ambiguous",
                selected=None,
                candidate_segments=c_segs,
                candidate_bridges=c_brs,
                matched_places=matched_places,
                related_history=related_history,
                warnings=warnings,
                clarification=f"Road '{named_road_segs[0].name}' has {len(named_road_segs)} segments ({[c['segment_id'] for c in c_segs]}). Please specify which segment_id is blocked.",
                explanation=f"Road '{named_road_segs[0].name}' spans {len(named_road_segs)} segments; clarification required.",
            )

        # ------------------------------------------------------------------
        # 4. Single place mentioned ("near Alpha", "at Bravo", "near J2", "at Pherima")
        # ------------------------------------------------------------------
        if len(matched_places) == 1 and parsed.km_marker is None:
            p = matched_places[0]
            incident = [
                s for s in sorted(snap.segments.values(), key=lambda x: x.id)
                if p.id in (s.from_place_id, s.to_place_id)
            ]
            if want_nh29 and any(s.on_nh29 for s in incident):
                incident = [s for s in incident if s.on_nh29]
            if parsed.bridge_mentioned and any(
                any(s.id in b.segment_ids for b in snap.bridges.values()) for s in incident
            ):
                incident = [s for s in incident if any(s.id in b.segment_ids for b in snap.bridges.values())]

            if incident:
                c_segs = [
                    self._segment_candidate(
                        snap,
                        s,
                        [f"incident to {snap.label(p.id)} ({p.id}): {snap.label(s.from_place_id)} -> {snap.label(s.to_place_id)}"]
                        + (["on NH 29"] if s.on_nh29 else [])
                        + (["carries bridge"] if any(s.id in b.segment_ids for b in snap.bridges.values()) else []),
                        rank=i,
                    )
                    for i, s in enumerate(incident[:max_candidates], 1)
                ]
                c_brs = [
                    self._bridge_candidate(b, [f"on segment incident to {snap.label(p.id)} ({p.id})"], rank=i)
                    for i, b in enumerate(
                        [b for b in sorted(snap.bridges.values(), key=lambda x: x.id) if any(s.id in b.segment_ids for s in incident)][:max_candidates],
                        1,
                    )
                ]
                if len(incident) == 1 and parsed.coordinates is None:
                    seg = incident[0]
                    chosen_bridge = c_brs[0]["bridge_id"] if parsed.bridge_mentioned and len(c_brs) == 1 else None
                    if seg.confidence != "verified":
                        warnings.append(f"Segment {seg.id} is '{seg.confidence}' (not hand-verified).")
                    selected = {
                        "segment_id": seg.id,
                        "bridge_id": chosen_bridge,
                        "segment_name": seg.name,
                        "ref": seg.ref,
                        "from_place_id": seg.from_place_id,
                        "to_place_id": seg.to_place_id,
                        "confidence": seg.confidence,
                        "source_url": seg.source_url,
                        "match_reasons": c_segs[0]["match_reasons"],
                    }
                    return self._finish(
                        snap,
                        inputs,
                        parsed,
                        status="resolved",
                        selected=selected,
                        candidate_segments=c_segs,
                        candidate_bridges=c_brs,
                        matched_places=matched_places,
                        related_history=related_history,
                        warnings=warnings,
                        clarification=None,
                        explanation=f"Resolved to segment {seg.id}, the only matching road segment connected to {snap.label(p.id)}.",
                    )
                return self._finish(
                    snap,
                    inputs,
                    parsed,
                    status="ambiguous",
                    selected=None,
                    candidate_segments=c_segs,
                    candidate_bridges=c_brs,
                    matched_places=matched_places,
                    related_history=related_history,
                    warnings=warnings,
                    clarification=(
                        f"{len(incident)} road segments meet at {snap.label(p.id)} ({p.id}): "
                        + ", ".join(
                            f"{c['segment_id']} ({snap.label(c['from_place_id'])} -> {snap.label(c['to_place_id'])})"
                            for c in c_segs
                        )
                        + ". Please clarify which segment_id is blocked."
                    ),
                    explanation=f"{snap.label(p.id)} has {len(incident)} matching incident road segments; refusing to snap to an arbitrary segment.",
                )

        # ------------------------------------------------------------------
        # 5. Kilometre / Chainage marker ("Landslide near NH29 kilometre 42.")
        # ------------------------------------------------------------------
        if parsed.km_marker is not None:
            km = parsed.km_marker
            intervals, max_km = self._cumulative_km_map(snap, nh29_only=want_nh29)
            if not intervals and want_nh29:
                intervals, max_km = self._cumulative_km_map(snap, nh29_only=False)

            warnings.append(
                "The graph does not contain verified highway milestone/chainage posts. Candidate segments are ranked by "
                "cumulative modelled road distance (length_km) from the Dimapur reference node, which may differ from "
                "official highway chainage numbers."
            )
            if max_km > 0 and km > max_km:
                warnings.append(
                    f"Requested kilometre marker ({km:g} km) exceeds the total modelled corridor length from Dimapur "
                    f"({max_km:g} km) in this dataset; returning the closest corridor segments by cumulative distance."
                )

            scored = sorted(
                intervals.items(),
                key=lambda kv: (
                    self._interval_distance(km, kv[1]),
                    not snap.segments[kv[0]].on_nh29,
                    kv[0],
                ),
            )
            if parsed.bridge_mentioned:
                with_br = [kv for kv in scored if any(kv[0] in b.segment_ids for b in snap.bridges.values())]
                if with_br:
                    scored = with_br

            c_segs = []
            for i, (sid, iv) in enumerate(scored[:max_candidates], 1):
                seg = snap.segments[sid]
                dist_km = self._interval_distance(km, iv)
                reason = (
                    f"spans cumulative distance {iv[0]:.2f}-{iv[1]:.2f} km from Dimapur (contains km {km:g})"
                    if dist_km == 0.0
                    else f"cumulative distance {iv[0]:.2f}-{iv[1]:.2f} km from Dimapur ({dist_km:.2f} km from requested km {km:g})"
                )
                reasons = [reason]
                if seg.on_nh29:
                    reasons.append("on NH 29")
                c_segs.append(self._segment_candidate(snap, seg, reasons, rank=i, cumulative_km=iv))

            c_brs = [
                self._bridge_candidate(b, [f"sits on candidate segment near modelled km {km:g}"], rank=i)
                for i, b in enumerate(
                    [
                        b for b in sorted(snap.bridges.values(), key=lambda x: x.id)
                        if any(c["segment_id"] in b.segment_ids for c in c_segs)
                    ][:max_candidates],
                    1,
                )
            ]
            if not c_segs:
                return self._finish(
                    snap,
                    inputs,
                    parsed,
                    status="no_match",
                    selected=None,
                    candidate_segments=[],
                    candidate_bridges=[],
                    matched_places=matched_places,
                    related_history=related_history,
                    warnings=warnings + ["No road segments with valid length_km were found from Dimapur."],
                    clarification=None,
                    explanation=f"Could not resolve km {km:g}: no segments with cumulative distance from Dimapur.",
                )

            return self._finish(
                snap,
                inputs,
                parsed,
                status="ambiguous",
                selected=None,
                candidate_segments=c_segs,
                candidate_bridges=c_brs,
                matched_places=matched_places,
                related_history=related_history,
                warnings=warnings,
                clarification=(
                    f"Kilometre marker 'km {km:g}' is matched using cumulative OSM segment length from Dimapur (not verified "
                    f"milestone posts). Candidate segments: "
                    + ", ".join(
                        f"{c['segment_id']} ({snap.label(c['from_place_id'])} -> {snap.label(c['to_place_id'])}, "
                        f"{c['cumulative_km_from_dimapur'][0]}-{c['cumulative_km_from_dimapur'][1]} km)"
                        for c in c_segs
                    )
                    + ". Confirm target_segment_id before applying a closure."
                ),
                explanation=(
                    f"Ranked {len(c_segs)} candidate segment(s) near km {km:g} along the corridor from Dimapur. "
                    "Clarification is required because the graph has no official chainage milestones."
                ),
            )

        # ------------------------------------------------------------------
        # 6. Geographic coordinates (proximity alone never silently snaps)
        # ------------------------------------------------------------------
        if parsed.coordinates is not None:
            clat, clon = parsed.coordinates["lat"], parsed.coordinates["lon"]
            warnings.append(
                "Geographic coordinate proximity alone is not sufficient to establish that an event occurred on a "
                "particular road. Confirm the intended segment_id from the nearest candidates."
            )
            pool = [
                s for s in snap.segments.values()
                if (not want_nh29 or s.on_nh29)
            ] or list(snap.segments.values())
            scored_segs = []
            for s in pool:
                dm = self._segment_distance_m(snap, s, clat, clon)
                if dm is not None:
                    scored_segs.append((dm, not s.on_nh29, s.id, s))
            scored_segs.sort(key=lambda t: (t[0], t[1], t[2]))

            c_segs = [
                self._segment_candidate(
                    snap,
                    s,
                    [f"{dm:.1f} m from coordinates ({clat}, {clon})"] + (["on NH 29"] if s.on_nh29 else []),
                    rank=i,
                    distance_m=dm,
                )
                for i, (dm, _, _, s) in enumerate(scored_segs[:max_candidates], 1)
            ]
            scored_brs = []
            for b in snap.bridges.values():
                if b.lat is not None and b.lon is not None:
                    scored_brs.append((_haversine_m(clat, clon, b.lat, b.lon), b.id, b))
            scored_brs.sort(key=lambda t: (t[0], t[1]))
            c_brs = [
                self._bridge_candidate(b, [f"{dm:.1f} m from coordinates ({clat}, {clon})"], rank=i, distance_m=dm)
                for i, (dm, _, b) in enumerate(scored_brs[:max_candidates], 1)
            ]
            return self._finish(
                snap,
                inputs,
                parsed,
                status="ambiguous",
                selected=None,
                candidate_segments=c_segs,
                candidate_bridges=c_brs,
                matched_places=matched_places,
                related_history=related_history,
                warnings=warnings,
                clarification=(
                    f"Coordinate search around ({clat}, {clon}) found {len(c_segs)} nearby segment(s): "
                    + ", ".join(f"{c['segment_id']} ({c['distance_m']} m)" for c in c_segs)
                    + ". Proximity alone does not identify the affected road; please confirm target_segment_id."
                ),
                explanation=f"Ranked {len(c_segs)} nearest segment(s) to ({clat}, {clon}); confirmation required.",
            )

        # ------------------------------------------------------------------
        # 7. Bridge mentioned without a specific place/id, or NH29-only mention
        # ------------------------------------------------------------------
        if parsed.bridge_mentioned and snap.bridges:
            br_list = sorted(snap.bridges.values(), key=lambda b: b.id)
            if want_nh29:
                nh_br = [b for b in br_list if any(snap.segments.get(sid) and snap.segments[sid].on_nh29 for sid in b.segment_ids)]
                if nh_br:
                    br_list = nh_br
            seg_ids = []
            for b in br_list:
                for sid in b.segment_ids:
                    if sid in snap.segments and sid not in seg_ids:
                        seg_ids.append(sid)
            c_brs = [
                self._bridge_candidate(b, ["bridge mentioned in description"], rank=i)
                for i, b in enumerate(br_list[:max_candidates], 1)
            ]
            c_segs = [
                self._segment_candidate(snap, snap.segments[sid], ["carries a candidate bridge"], rank=i)
                for i, sid in enumerate(seg_ids[:max_candidates], 1)
            ]
            if len(br_list) == 1 and len(seg_ids) == 1:
                # Even if there's only 1 bridge in a tiny test fixture, check if an unknown place name was in the text!
                if not self._has_unmatched_location_words(parsed.raw_text):
                    seg = snap.segments[seg_ids[0]]
                    br = br_list[0]
                    if seg.confidence != "verified":
                        warnings.append(f"Segment {seg.id} is '{seg.confidence}' (not hand-verified).")
                    selected = {
                        "segment_id": seg.id,
                        "bridge_id": br.id,
                        "segment_name": seg.name,
                        "ref": seg.ref,
                        "from_place_id": seg.from_place_id,
                        "to_place_id": seg.to_place_id,
                        "confidence": seg.confidence,
                        "source_url": seg.source_url,
                        "match_reasons": [f"only bridge in corridor ({br.id}) sits on segment {seg.id}"],
                    }
                    return self._finish(
                        snap,
                        inputs,
                        parsed,
                        status="resolved",
                        selected=selected,
                        candidate_segments=c_segs,
                        candidate_bridges=c_brs,
                        matched_places=matched_places,
                        related_history=related_history,
                        warnings=warnings,
                        clarification=None,
                        explanation=f"Resolved to bridge {br.id} on segment {seg.id}.",
                    )
            return self._finish(
                snap,
                inputs,
                parsed,
                status="ambiguous",
                selected=None,
                candidate_segments=c_segs,
                candidate_bridges=c_brs,
                matched_places=matched_places,
                related_history=related_history,
                warnings=warnings,
                clarification=f"Multiple or unspecified bridge candidates ({[b['bridge_id'] for b in c_brs]}). Please specify target_bridge_id or target_segment_id.",
                explanation=f"Found {len(c_brs)} candidate bridge(s) on {len(c_segs)} segment(s); clarification required.",
            )

        if want_nh29 and not self._has_unmatched_location_words(parsed.raw_text):
            nh_segs = [s for s in sorted(snap.segments.values(), key=lambda x: x.id) if s.on_nh29]
            intervals, _ = self._cumulative_km_map(snap, nh29_only=True)
            c_segs = [
                self._segment_candidate(
                    snap,
                    s,
                    ["on NH 29 (no specific settlement, junction, or bridge was specified)"],
                    rank=i,
                    cumulative_km=intervals.get(s.id),
                )
                for i, s in enumerate(nh_segs[:max_candidates], 1)
            ]
            return self._finish(
                snap,
                inputs,
                parsed,
                status="ambiguous",
                selected=None,
                candidate_segments=c_segs,
                candidate_bridges=[],
                matched_places=matched_places,
                related_history=related_history,
                warnings=warnings,
                clarification=(
                    f"NH 29 has {len(nh_segs)} road segments in the corridor graph (showing top {len(c_segs)}: "
                    f"{[c['segment_id'] for c in c_segs]}). Please specify the settlement, junction pair, or segment_id."
                ),
                explanation=f"Event mentions NH 29 without a specific segment or endpoint; returning {len(c_segs)} of {len(nh_segs)} NH 29 segments.",
            )

        # ------------------------------------------------------------------
        # 8. No match in corridor data
        # ------------------------------------------------------------------
        warnings.append(
            "No place, bridge, or road segment in the seeded corridor graph matched the event description. "
            "The named location may be outside the Dimapur–Kohima NH29 corridor or unattached in OpenStreetMap."
        )
        return self._finish(
            snap,
            inputs,
            parsed,
            status="no_match",
            selected=None,
            candidate_segments=[],
            candidate_bridges=[],
            matched_places=matched_places,
            related_history=related_history,
            warnings=warnings,
            clarification="No matching location was found in the corridor graph. Use 'ids' to inspect available places, bridges, and segments.",
            explanation="No road segment or bridge in the seeded corridor graph matched the description.",
        )

    @staticmethod
    def _has_unmatched_location_words(text: str) -> bool:
        """Detect if a preposition like 'near X' or 'at X' or 'between X and Y' named an unknown location."""
        m = re.search(r"\b(?:near|at|in|around|towards|between)\s+([a-z0-9\u0080-\uffff_-]{3,})", _fold(text))
        if not m:
            return False
        word = m.group(1)
        return word not in {
            "nh", "nh29", "highway", "national", "road", "bridge", "corridor", "kilometre", "kilometer",
            "km", "chainage", "monsoon", "model", "the", "a", "an", "segment", "closure", "landslide",
            "mudslide", "flood", "rockfall", "washout",
        } and word not in MONTH_NAMES

    def _finish(
        self,
        snap: Snapshot,
        inputs: dict,
        parsed: ParsedEvent,
        *,
        status: str,
        selected: dict | None,
        candidate_segments: list[dict],
        candidate_bridges: list[dict],
        matched_places: list[Place],
        related_history: list[dict],
        warnings: list[str],
        clarification: str | None,
        explanation: str,
    ) -> dict:
        log = self.store.drain_log()
        seen_w: set[str] = set()
        uniq_warnings: list[str] = []
        for w in warnings:
            if w not in seen_w:
                seen_w.add(w)
                uniq_warnings.append(w)

        # Build supporting subgraph of matched places and candidate segments
        place_roles = {p.id: "matched_place" for p in matched_places}
        seg_status = {}
        for c in candidate_segments:
            sid = c["segment_id"]
            seg_status[sid] = "selected_target" if (selected and selected["segment_id"] == sid) else "candidate"
            s = snap.segments.get(sid)
            if s:
                for pid in (s.from_place_id, s.to_place_id):
                    if pid in snap.places:
                        place_roles.setdefault(pid, "candidate_endpoint")

        nodes = [
            {
                "id": pid,
                "label": "Place",
                "name": snap.places[pid].name,
                "type": snap.places[pid].type,
                "lat": snap.places[pid].lat,
                "lon": snap.places[pid].lon,
                "role": role,
                "confidence": snap.places[pid].confidence,
                "data_status": snap.places[pid].data_status,
                "source_url": snap.places[pid].source_url,
            }
            for pid, role in sorted(place_roles.items())
            if pid in snap.places
        ]
        edges = [
            {
                "id": sid,
                "label": "RoadSegment",
                "source": snap.segments[sid].from_place_id,
                "target": snap.segments[sid].to_place_id,
                "status": st,
                "name": snap.segments[sid].name,
                "ref": snap.segments[sid].ref,
                "on_nh29": snap.segments[sid].on_nh29,
                "is_bidirectional": snap.segments[sid].is_bidirectional,
                "travel_time_min": snap.segments[sid].travel_time_min,
                "confidence": snap.segments[sid].confidence,
                "data_status": snap.segments[sid].data_status,
                "source_url": snap.segments[sid].source_url,
            }
            for sid, st in sorted(seg_status.items())
            if sid in snap.segments
        ]

        result = {
            "status": status,
            "requires_clarification": status == "ambiguous",
            "clarification_prompt": clarification,
            "parsed_event": parsed.to_dict(),
            "selected_target": selected,
            "matched_places": [self._place_summary(p) for p in matched_places],
            "candidate_segments": candidate_segments,
            "candidate_bridges": candidate_bridges,
            "related_observed_events": related_history,
        }
        algorithm = {
            "name": "corridor location resolution (Cypher snapshot + NetworkX cumulative distance / shortest-path matching)",
            "implementation": (
                f"NetworkX {A.NX_VERSION} on a graph exported from FalkorDB with Cypher. "
                f"{FALKORDB_NATIVE_NOTE} Ambiguous, chainage-based, and coordinate-only matches require explicit confirmation."
            ),
        }
        return {
            "tool": "resolve_location",
            "result": result,
            "cypher": [q["query"] for q in log],
            "params": {
                "inputs": inputs,
                "query_names": [q["name"] for q in log],
                "queries": [q["params"] for q in log],
            },
            "explanation": explanation,
            "graph": {"nodes": nodes, "edges": edges},
            "warnings": uniq_warnings,
            "algorithm": algorithm,
        }
