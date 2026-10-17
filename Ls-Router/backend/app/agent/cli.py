"""Command-line interface for the Phase 3 Landslide Router Agent (Command Prompt, backend venv active):

    # against a seeded FalkorDB instance:
    python -m app.agent.cli "Landslide near NH29 kilometre 42."
    python -m app.agent.cli "Landslide near NH29 kilometre 42." --target-segment <segment_id>
    python -m app.agent.cli --demo

    # against a cached normalized dataset JSON (offline, no Docker required):
    python -m app.agent.cli --dataset ..\\data\\cache\\normalized.json "Landslide near NH29 kilometre 42."

    # against the deterministic synthetic test fixture (offline, no Docker or cache required):
    python -m app.agent.cli --fixture --demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..graph.errors import ToolError
from ..graph.fingerprint import network_fingerprint, segment_fingerprint
from ..graph.in_memory import InMemoryGraph
from ..graph.store import FalkorStore
from .llm import llm_client_from_env
from .orchestrator import LandslideAgent


def build_synthetic_fixture_dataset() -> dict:
    """Deterministic synthetic corridor dataset (matches tests/scenarios.py scenario_a) for offline CLI verification."""
    prov = {"confidence": "unverified", "retrieved_at": "2000-01-01T00:00:00Z", "last_verified": None}
    raw_places = [
        ("D", "town", "Dimapur", "dimapur", 10.00, 10.00),
        ("J1", "junction", None, None, 10.00, 10.02),
        ("J2", "junction", None, None, 10.00, 10.04),
        ("K", "town", "Kohima", "kohima", 10.00, 10.06),
        ("A", "village", "Alpha", None, 9.99, 10.04),
        ("B", "village", "Bravo", None, 10.01, 10.03),
        ("C", "village", "Charlie", None, 10.00, 10.07),
        ("E", "village", "Echo", None, 10.00, 10.08),
        ("X", "village", "Xray", None, 10.15, 10.15),
        ("Y", "village", "Yankee", None, 10.15, 10.16),
    ]
    places = [
        {
            **prov,
            "id": pid,
            "name": name,
            "type": ptype,
            "lat": lat,
            "lon": lon,
            "population": None,
            "source_url": f"https://example.test/place/{pid}",
            "source_id": f"place/{pid}",
            "data_status": "observed",
            "reference_key": ref,
            "origin": "synthetic",
        }
        for pid, ptype, name, ref, lat, lon in raw_places
    ]
    raw_segs = [
        ("s1", "D", "J1", 10.0, 5.0, True, True, "Synthetic Highway"),
        ("s2", "J1", "J2", 20.0, 10.0, True, True, "Synthetic Highway"),
        ("s3", "J2", "K", 10.0, 5.0, True, True, "Synthetic Highway"),
        ("s4", "J1", "B", 15.0, 8.0, True, False, "Synthetic Alt Road"),
        ("s5", "B", "J2", 25.0, 12.0, True, False, "Synthetic Alt Road"),
        ("s6", "J2", "A", 5.0, 2.0, True, False, "Synthetic Spur"),
        ("s7", "K", "C", 8.0, 4.0, True, False, "Charlie Connector"),
        ("s8", "C", "E", 4.0, 2.0, False, False, "Echo One-Way"),
        ("s9", "X", "Y", 3.0, 1.0, True, False, "Island Road"),
    ]
    segs, conns = [], []
    for sid, a, b, mins, km, two_way, nh29, name in raw_segs:
        rec = {
            **prov,
            "id": sid,
            "name": name,
            "ref": "NH 29" if nh29 else None,
            "highway": "trunk" if nh29 else "secondary",
            "length_km": km,
            "travel_time_min": mins,
            "travel_time_basis": "assumed",
            "surface": None,
            "monsoon_risk": None,
            "source_url": f"https://example.test/seg/{sid}",
            "source_id": f"seg/{sid}",
            "data_status": "derived",
            "is_bidirectional": two_way,
            "on_nh29": nh29,
            "way_ids": [],
            "from_place_id": a,
            "to_place_id": b,
            "geometry": "[]",
        }
        conns.append({"from_place_id": a, "to_place_id": b, "segment_id": sid, "travel_time_min": mins, "direction": "forward"})
        if two_way:
            conns.append({"from_place_id": b, "to_place_id": a, "segment_id": sid, "travel_time_min": mins, "direction": "reverse"})
        rec["baseline_fp"] = segment_fingerprint(rec)
        segs.append(rec)

    hospitals = [
        {**prov, "id": hid, "name": hname, "lat": 10.0, "lon": 10.0, "beds": beds, "trauma_level": None,
         "source_url": f"https://example.test/hosp/{hid}", "source_id": f"hosp/{hid}", "data_status": "observed", "origin": "synthetic"}
        for hid, hname, _, beds in [("HJ", "Hosp J", "J1", 100), ("HK", "Hosp K", "K", None), ("HE", "Hosp E", "E", None),
                                    ("HY", "Hosp Y", "Y", None), ("HN", "Hosp N", None, None)]
    ]
    located = [
        {"hospital_id": hid, "place_id": pid, "snap_distance_m": 10.0, "method": "nearest_road_vertex", "data_status": "derived"}
        for hid, pid in [("HJ", "J1"), ("HK", "K"), ("HE", "E"), ("HY", "Y")]
    ]
    bridges = [
        {**prov, "id": "BR1", "name": "Bridge One", "road_name": "Synthetic Highway", "lat": 10.0, "lon": 10.035,
         "monsoon_risk": None, "source_url": "https://example.test/bridge/BR1", "source_id": "bridge/BR1", "data_status": "observed"}
    ]
    bridge_on = [{"bridge_id": "BR1", "segment_id": "s2"}]
    seasons = [{"id": "monsoon", "name": "Monsoon", "start_month": 6, "end_month": 9, "data_status": "derived", "confidence": "unverified"}]
    season_affects = [{"season_id": "monsoon", "segment_id": s["id"], "basis": "assumed_all_segments_exposed"} for s in segs]
    return {
        "schema_version": 1,
        "meta": {"kind": "synthetic-test", "source": {"retrieved_at": prov["retrieved_at"]}, "bbox": None},
        "corridor": {"id": "corr-synthetic", "name": "Synthetic test corridor"},
        "datasets": [{
            "id": "dataset-synthetic",
            "kind": "synthetic-test",
            "name": "synthetic",
            "retrieved_at": prov["retrieved_at"],
            "network_fingerprint": network_fingerprint(segs, conns),
            "attribution": "SYNTHETIC TEST FIXTURE",
        }],
        "seasons": seasons,
        "places": places,
        "hospitals": hospitals,
        "bridges": bridges,
        "segments": segs,
        "connections": conns,
        "bridge_on": bridge_on,
        "hospital_located_at": located,
        "season_affects": season_affects,
        "observed_events": [],
        "event_near_place": [],
        "event_blocks": [],
        "event_failed_bridges": [],
        "reference_places": {"dimapur": "D", "kohima": "K"},
    }


def _run_demo(agent: LandslideAgent, fmt: str, out) -> int:
    """Run a reproducible multi-step demonstration of the Phase 3 agent loop."""
    steps = []

    # 1. Ambiguous natural-language event ("Landslide near NH29 kilometre 42.")
    r1 = agent.run("Landslide near NH29 kilometre 42.")
    cands = (r1.get("resolution") or {}).get("candidate_segments") or []
    if not cands:
        out(json.dumps({"error": {"code": "empty_graph", "message": "No candidate segments found; seed the graph first."}}, indent=2))
        return 2
    steps.append({"step": 1, "description": "Ambiguous location resolution (requires clarification)", "report": r1})

    # 2. If there is a second candidate (e.g. s2 with a detour via Bravo), show alternate route calculation first
    if len(cands) >= 2:
        detour_seg = cands[1]["segment_id"]
        r_detour = agent.run("Landslide near NH29 kilometre 42.", target_segment_id=detour_seg, month=7)
        ev_detour = r_detour["closure"]["event"]["id"]
        r_detour_clear = agent.run(f"Clear closure {ev_detour} and audit")
        steps.append({"step": len(steps) + 1, "description": f"Confirmed closure on {detour_seg} (alternate route & detour analysis)", "report": r_detour})
        steps.append({"step": len(steps) + 1, "description": f"Resolve closure {ev_detour} and audit baseline restoration", "report": r_detour_clear})

    # 3. Confirm primary candidate segment (e.g. s3 causing cut-off settlements, hospital impact, and draft alerts)
    first_seg = cands[0]["segment_id"]
    r2 = agent.run("Landslide near NH29 kilometre 42.", target_segment_id=first_seg, month=8)
    ev_id = r2["closure"]["event"]["id"]
    steps.append({"step": len(steps) + 1, "description": f"Confirmed closure on {first_seg} (cut-off settlements, hospitals & alerts)", "report": r2})

    # 4. Resolve the closure and audit baseline restoration so the graph returns to baseline cleanly
    r3 = agent.run(f"Clear closure {ev_id} and audit")
    steps.append({"step": len(steps) + 1, "description": f"Resolve closure {ev_id} and audit baseline restoration", "report": r3})

    if fmt == "json":
        out(
            json.dumps(
                {
                    "demo": "Landslide Router Phase 3 Reproducible CLI Demo",
                    "steps": steps,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    for i, st in enumerate(steps):
        if i > 0:
            out("")
        out("=" * 78)
        out(f"STEP {st['step']}: {st['description']}")
        out("=" * 78)
        out(st["report"]["markdown_report"].replace("\u2014", "--").replace("\u2013", "-"))
    return 0


def main(argv=None, graph=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", nargs="?", help="Natural-language event description or query")
    ap.add_argument("--demo", action="store_true", help="Run the reproducible multi-step demonstration")
    ap.add_argument("--target-segment", default=None, help="Explicitly confirmed RoadSegment ID")
    ap.add_argument("--target-bridge", default=None, help="Explicitly confirmed Bridge ID")
    ap.add_argument("--confirm-id", default=None, help="Alias for --target-segment or --target-bridge")
    ap.add_argument("--clear-after", action="store_true", help="Resolve and audit the created closure after reporting")
    ap.add_argument("--lat", type=float, default=None, help="Optional latitude")
    ap.add_argument("--lon", type=float, default=None, help="Optional longitude")
    ap.add_argument("--month", default=None, help="Optional month (1-12 or name) for seasonal risk")
    ap.add_argument("--top-k", type=int, default=5, help="Top-K weak links to rank (default: 5)")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format")
    ap.add_argument("--json", action="store_true", help="Shorthand for --format json")
    ap.add_argument("--graph", default=os.environ.get("FALKORDB_GRAPH", "landslide"), help="FalkorDB graph name")
    ap.add_argument("--dataset", type=Path, default=None, help="Run against a normalized.json dataset file in memory")
    ap.add_argument("--fixture", action="store_true", help="Run against the deterministic synthetic test fixture in memory")
    ap.add_argument("--planner", choices=["deterministic", "auto"], default="deterministic", help="Planner mode")
    ap.add_argument(
        "--mcp-transport",
        choices=["memory", "stdio", "direct"],
        default="memory",
        help="Invoke tools through MCP in-memory streams ('memory'), MCP stdio subprocess ('stdio'), or direct Python ('direct')",
    )
    a = ap.parse_args(argv)
    fmt = "json" if a.json else a.format

    if not a.demo and not (a.prompt and a.prompt.strip()):
        ap.error("Provide a natural-language prompt or pass --demo.")

    if graph is None:
        if a.fixture:
            graph = InMemoryGraph(build_synthetic_fixture_dataset())
        elif a.dataset is not None:
            if not a.dataset.exists():
                out(json.dumps({"error": {"code": "file_not_found", "message": f"Dataset {a.dataset} not found."}}, indent=2))
                return 2
            ds = json.loads(a.dataset.read_text(encoding="utf-8"))
            graph = InMemoryGraph(ds)
        else:
            from falkordb import FalkorDB
            from ..config import get_settings

            s = get_settings()
            graph = FalkorDB(
                host=s.falkordb_host,
                port=s.falkordb_port,
                username=s.falkordb_username or None,
                password=s.falkordb_password or None,
                ssl=s.falkordb_ssl,
            ).select_graph(a.graph or s.falkordb_graph)

    store = FalkorStore(graph)
    llm_client = llm_client_from_env() if a.planner == "auto" else None
    mcp_client = None
    use_mcp = a.mcp_transport != "direct"
    if a.mcp_transport == "stdio":
        from ..mcp_bridge.client import MCPToolClient

        stdio_args: list[str] = []
        if a.fixture:
            stdio_args.append("--fixture")
        elif a.dataset is not None:
            stdio_args.extend(["--dataset", str(a.dataset.resolve())])
        else:
            stdio_args.extend(["--graph", a.graph])
        mcp_client = MCPToolClient(store, transport="stdio", stdio_args=stdio_args)

    agent = LandslideAgent(
        store,
        llm_client=llm_client,
        use_mcp=use_mcp,
        mcp_client=mcp_client,
        mcp_transport="memory" if a.mcp_transport == "direct" else a.mcp_transport,
    )

    target_seg = a.target_segment
    target_br = a.target_bridge
    if a.confirm_id and not (target_seg or target_br):
        if a.confirm_id.startswith("bridge:") or a.confirm_id.startswith("BR"):
            target_br = a.confirm_id
        else:
            target_seg = a.confirm_id

    try:
        if a.demo:
            return _run_demo(agent, fmt, out)

        report = agent.run(
            a.prompt,
            lat=a.lat,
            lon=a.lon,
            target_segment_id=target_seg,
            target_bridge_id=target_br,
            month=a.month,
            top_k=a.top_k,
        )
        if a.clear_after and report.get("closure") and report["closure"].get("event"):
            ev_id = report["closure"]["event"]["id"]
            cleanup = agent.run(f"Clear closure {ev_id} and audit")
            report["closure_resolution"] = cleanup.get("closure_resolution")
            report["closure_audit"] = cleanup.get("closure_audit")
            report["markdown_report"] = agent.format_markdown(report)

        if fmt == "json":
            out(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        else:
            out(report["markdown_report"].replace("\u2014", "--").replace("\u2013", "-"))
        return 0
    except ToolError as exc:
        out(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, indent=2))
        return 2
    except (ValueError, TypeError) as exc:
        out(json.dumps({"error": {"code": "bad_arguments", "message": str(exc)}}, indent=2))
        return 2
    finally:
        if mcp_client is not None:
            mcp_client.close()


if __name__ == "__main__":
    sys.exit(main())

