"""In-memory copy of the graph state the tools reason over. Built only from Cypher results (see store.py)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Place:
    id: str
    name: str | None
    type: str | None
    lat: float | None
    lon: float | None
    reference_key: str | None
    confidence: str | None
    source_url: str | None
    data_status: str | None


@dataclass(frozen=True)
class Segment:
    id: str
    name: str | None
    ref: str | None
    highway: str | None
    length_km: float | None
    travel_time_min: float | None
    travel_time_basis: str | None
    is_bidirectional: bool | None
    on_nh29: bool | None
    monsoon_risk: str | None
    confidence: str | None
    source_url: str | None
    data_status: str | None
    from_place_id: str | None
    to_place_id: str | None
    baseline_fp: str | None
    corridor_id: str | None


@dataclass(frozen=True)
class Connection:
    from_id: str
    to_id: str
    segment_id: str | None
    travel_time_min: float | None
    direction: str | None


@dataclass(frozen=True)
class Hospital:
    id: str
    name: str | None
    lat: float | None
    lon: float | None
    beds: int | None
    trauma_level: object
    confidence: str | None
    source_url: str | None
    data_status: str | None
    access_place_id: str | None
    snap_distance_m: float | None


@dataclass(frozen=True)
class Bridge:
    id: str
    name: str | None
    road_name: str | None
    lat: float | None
    lon: float | None
    monsoon_risk: str | None
    confidence: str | None
    source_url: str | None
    segment_ids: tuple = ()


@dataclass(frozen=True)
class Event:
    id: str
    description: str | None
    status: str | None
    data_status: str | None
    started_at: str | None
    ended_at: str | None
    cause: str | None
    origin: str | None
    confidence: str | None
    remembered_at: str | None
    source_url: str | None
    location_text: str | None
    blocks_segments: tuple = ()
    blocks_bridges: tuple = ()
    near_places: tuple = ()


@dataclass(frozen=True)
class Season:
    id: str
    name: str | None
    start_month: int | None
    end_month: int | None
    segment_ids: tuple = ()


@dataclass
class Snapshot:
    places: dict = field(default_factory=dict)
    segments: dict = field(default_factory=dict)
    connections: list = field(default_factory=list)
    hospitals: dict = field(default_factory=dict)
    bridges: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    failed_in: list = field(default_factory=list)      # (bridge_id, event_id, year)
    seasons: dict = field(default_factory=dict)
    dataset: dict = field(default_factory=dict)        # id, kind, network_fingerprint, ...

    # ---- reference places
    def reference(self, key: str) -> Place | None:
        return next((p for p in self.places.values() if p.reference_key == key), None)

    def corridors(self) -> set:
        return {s.corridor_id for s in self.segments.values() if s.corridor_id}

    # ---- closure state
    def event_segment_ids(self, event_id: str) -> set:
        """Segments an event blocks: directly, and via the segments every blocked bridge sits on."""
        e = self.events[event_id]
        out = set(e.blocks_segments)
        for bid in e.blocks_bridges:
            if bid in self.bridges:
                out |= set(self.bridges[bid].segment_ids)
        return out

    def blocked_map(self, extra_events=(), without_events=()) -> dict:
        """segment_id -> sorted ids of the ACTIVE closures blocking it (plus `extra_events` forced in)."""
        out: dict = {}
        for e in self.events.values():
            if e.id in without_events:
                continue
            if e.status == "active" or e.id in extra_events:
                for sid in self.event_segment_ids(e.id):
                    out.setdefault(sid, []).append(e.id)
        return {k: sorted(v) for k, v in out.items()}

    def active_events(self) -> list:
        return sorted(e.id for e in self.events.values() if e.status == "active")

    def label(self, place_id: str) -> str:
        p = self.places.get(place_id)
        return (p.name if p and p.name else place_id)
