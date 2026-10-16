"""Run one tool against a seeded graph from the command line (Command Prompt, backend venv active):

    python -m app.graph.cli ids                       # find the place / segment / bridge / hospital ids to use
    python -m app.graph.cli apply_closure "{\"segment_id\": \"seg-...\", \"description\": \"test slide\"}"
    python -m app.graph.cli find_cut_off_places "{\"closure_event_id\": \"sim-...\"}"
    python -m app.graph.cli seasonal_risk "{\"month\": 7}"

It prints the full tool envelope as JSON. This is a development aid, not the agent (Phase 3) or the MCP server (Phase 4).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .errors import ToolError
from .store import FalkorStore
from .tools import Toolbox

TOOLS = ["apply_closure", "clear_closure", "find_cut_off_places", "reroute", "find_unreachable_hospitals", "find_weak_links",
         "seasonal_risk", "draft_alerts", "explain_path", "remember_closure", "audit_closure", "probe_capabilities"]


def describe_ids(snap, show_all: bool = False, limit: int = 40) -> list:
    lines = [f"reference places: dimapur={getattr(snap.reference('dimapur'), 'id', None)}  kohima={getattr(snap.reference('kohima'), 'id', None)}",
             f"corridors: {sorted(snap.corridors())}", f"active closures: {snap.active_events()}"]

    def block(title, rows):
        lines.append(f"\n{title} ({len(rows)})")
        for r in (rows if show_all else rows[:limit]):
            lines.append("  " + r)
        if not show_all and len(rows) > limit:
            lines.append(f"  ... {len(rows) - limit} more (use --all)")

    lab = snap.label
    segs = sorted(snap.segments.values(), key=lambda s: (not s.on_nh29, s.id))
    block("segments (NH29 first): id | name | from -> to | minutes | confidence",
          [f"{s.id} | {s.name or s.ref or '-'} | {lab(s.from_place_id)} -> {lab(s.to_place_id)} | {s.travel_time_min} | {s.confidence}" for s in segs])
    block("bridges: id | name | on segments", [f"{b.id} | {b.name or b.road_name or '-'} | {list(b.segment_ids)}" for b in sorted(snap.bridges.values(), key=lambda b: b.id)])
    block("hospitals: id | name | access place", [f"{h.id} | {h.name or '-'} | {h.access_place_id}" for h in sorted(snap.hospitals.values(), key=lambda h: h.id)])
    block("settlements: id | name | type", [f"{p.id} | {p.name} | {p.type}" for p in sorted(snap.places.values(), key=lambda p: p.id) if p.type in ("village", "town")])
    return lines


def main(argv=None, graph=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["ids"] + TOOLS)
    ap.add_argument("args", nargs="?", default="{}", help="tool arguments as a JSON object")
    ap.add_argument("--graph", default=os.environ.get("FALKORDB_GRAPH", "landslide"))
    ap.add_argument("--all", action="store_true", help="with 'ids': print every row")
    a = ap.parse_args(argv)
    if graph is None:
        from falkordb import FalkorDB

        graph = FalkorDB(host=os.environ.get("FALKORDB_HOST", "localhost"), port=int(os.environ.get("FALKORDB_PORT", "6379")),
                         password=os.environ.get("FALKORDB_PASSWORD") or None).select_graph(a.graph)
    store = FalkorStore(graph)
    try:
        if a.command == "ids":
            out("\n".join(describe_ids(store.load_snapshot(), a.all)))
            return 0
        kwargs = json.loads(a.args)
        if not isinstance(kwargs, dict):
            raise ValueError("args must be a JSON object")
        out(json.dumps(getattr(Toolbox(store), a.command)(**kwargs), indent=2, ensure_ascii=False, default=str))
        return 0
    except ToolError as exc:
        out(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, indent=2))
        return 2
    except (ValueError, TypeError) as exc:
        out(json.dumps({"error": {"code": "bad_arguments", "message": str(exc)}}, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
