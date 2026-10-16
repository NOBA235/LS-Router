"""Graph algorithms on a graph exported from FalkorDB (the documented NetworkX fallback). The only module importing networkx.

Why a fallback: FalkorDB ships algo.SPpaths (weighted paths), algo.WCC and algo.betweenness (nodes only), but none of them can
exclude an edge because the RoadSegment it depends on is blocked by an active ClosureEvent, and the brief needs
segment-level centrality. FalkorDB stays the source of truth: topology and closure state are read with Cypher on every call
and the closure filter is applied while building the NetworkX graph.
"""
from __future__ import annotations

import math
from collections import defaultdict

import networkx as nx

from .snapshot import Snapshot

NX_VERSION = nx.__version__


def weight_ok(w) -> bool:
    return isinstance(w, (int, float)) and not isinstance(w, bool) and math.isfinite(w) and w > 0


def classify_connections(snap: Snapshot, blocked) -> dict:
    """A connection is traversable only if it names an existing segment and that segment is not blocked."""
    out = {"usable": [], "blocked": [], "dangling": []}
    for c in snap.connections:
        if c.segment_id is None or c.segment_id not in snap.segments:
            out["dangling"].append(c)
        elif c.segment_id in blocked:
            out["blocked"].append(c)
        else:
            out["usable"].append(c)
    return out


def _skeleton(snap: Snapshot) -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_nodes_from(sorted(snap.places))
    return G


def topology_graph(snap: Snapshot, blocked) -> tuple[nx.DiGraph, dict]:
    """Directed graph of usable connections, ignoring weights: answers 'can I get there at all'."""
    cls = classify_connections(snap, blocked)
    G = _skeleton(snap)
    for c in sorted(cls["usable"], key=lambda c: (c.from_id, c.to_id, c.segment_id)):
        if c.from_id in G and c.to_id in G:
            if G.has_edge(c.from_id, c.to_id):
                G[c.from_id][c.to_id]["segment_ids"].append(c.segment_id)
            else:
                G.add_edge(c.from_id, c.to_id, segment_ids=[c.segment_id])
    return G, cls


def routing_graph(snap: Snapshot, blocked) -> tuple[nx.DiGraph, dict]:
    """Directed graph for minimum-travel-time routing. Connections with a missing/invalid weight are excluded (never
    treated as zero). Parallel connections keep the fastest one, ties broken by segment id."""
    cls = classify_connections(snap, blocked)
    G = _skeleton(snap)
    invalid = []
    for c in sorted(cls["usable"], key=lambda c: (c.from_id, c.to_id, c.segment_id)):
        if not weight_ok(c.travel_time_min):
            invalid.append(c)
            continue
        if c.from_id not in G or c.to_id not in G:
            continue
        cur = G.get_edge_data(c.from_id, c.to_id)
        if cur is None or c.travel_time_min < cur["w"]:
            G.add_edge(c.from_id, c.to_id, w=float(c.travel_time_min), segment_id=c.segment_id, direction=c.direction)
    cls["invalid_weight"] = invalid
    return G, cls


def reach(G: nx.DiGraph, source: str) -> set:
    """Directed reachability from `source` (including itself). NOT a component analysis."""
    return {source} | nx.descendants(G, source) if source in G else set()


def reach_to(G: nx.DiGraph, target: str) -> set:
    return {target} | nx.ancestors(G, target) if target in G else set()


def weak_components(G: nx.DiGraph) -> list:
    """Connected components of the UNDIRECTED representation, largest first."""
    return sorted((frozenset(c) for c in nx.weakly_connected_components(G)), key=lambda c: (-len(c), sorted(c)[0]))


def hop_path(G: nx.DiGraph, a: str, b: str) -> list | None:
    """Fewest-hop path. Used only as evidence of prior connectivity, never as a travel-time claim."""
    try:
        return nx.shortest_path(G, a, b)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def route(G: nx.DiGraph, a: str, b: str) -> dict | None:
    """Minimum total travel time (Dijkstra on w=travel_time_min). None if no route."""
    if a not in G or b not in G:
        return None
    try:
        nodes = nx.dijkstra_path(G, a, b, weight="w")
    except nx.NetworkXNoPath:
        return None
    steps = []
    for u, v in zip(nodes, nodes[1:]):
        d = G[u][v]
        steps.append({"from": u, "to": v, "segment_id": d["segment_id"], "direction": d["direction"], "travel_time_min": d["w"]})
    total = math.fsum(s["travel_time_min"] for s in steps)
    assert abs(total - nx.dijkstra_path_length(G, a, b, weight="w")) < 1e-9, "route sum disagrees with Dijkstra length"
    return {"nodes": nodes, "steps": steps, "total_travel_time_min": round(total, 6)}


def single_source(G: nx.DiGraph, source: str) -> tuple[dict, dict]:
    """(distances, paths) by travel time from `source` to everything reachable; ({}, {}) if source is unknown."""
    if source not in G:
        return {}, {}
    return nx.single_source_dijkstra(G, source, weight="w")


def route_length_km(snap: Snapshot, r: dict) -> float | None:
    """Sum of segment lengths, or None when any is missing (never guessed)."""
    vals = [snap.segments[s["segment_id"]].length_km for s in r["steps"]]
    if any(not weight_ok(v) for v in vals):
        return None
    return round(math.fsum(vals), 3)


def centrality(G: nx.DiGraph) -> dict:
    """Shortest-path (Brandes) betweenness on the travel-time-weighted directed graph, normalized by NetworkX.
    Segment score = sum over its directed edges (both directions of a two-way segment)."""
    nodes = nx.betweenness_centrality(G, weight="w", normalized=True)
    edges = nx.edge_betweenness_centrality(G, weight="w", normalized=True)
    seg: dict = defaultdict(float)
    for (u, v), score in edges.items():
        seg[G[u][v]["segment_id"]] += score
    return {"nodes": nodes, "segments": dict(seg)}


def pair_usage(G: nx.DiGraph) -> dict:
    """segment_id -> {'pairs': n, 'examples': [(origin, destination, path nodes)]} over all ordered pairs, one shortest
    path per pair. A count of single chosen paths: related to, but not the same number as, betweenness (which splits ties)."""
    usage: dict = defaultdict(lambda: {"pairs": 0, "examples": []})
    for s, paths in sorted(nx.all_pairs_dijkstra_path(G, weight="w")):
        for t, nodes in sorted(paths.items()):
            if s == t:
                continue
            for u, v in zip(nodes, nodes[1:]):
                u_ = usage[G[u][v]["segment_id"]]
                u_["pairs"] += 1
                if len(u_["examples"]) < 3:
                    u_["examples"].append((s, t, nodes))
    return dict(usage)


def articulation_places(G: nx.DiGraph) -> set:
    """Places whose removal disconnects the undirected network."""
    return set(nx.articulation_points(nx.Graph(G.to_undirected())))


def cut_off_if_blocked(snap: Snapshot, origin: str, segment_ids, base_blocked=None) -> set:
    """Places reachable from `origin` today but not once `segment_ids` are also blocked."""
    blocked = set(base_blocked or ()) | set(segment_ids)
    before, _ = topology_graph(snap, set(base_blocked or ()))
    after, _ = topology_graph(snap, blocked)
    return reach(before, origin) - reach(after, origin)
