"""Command-line interface for Phase 4 MCP discovery, tool invocation, and end-to-end MCP agent demo.

Run from `backend\\` with `.venv` active:

    # 1. Discover exposed MCP tools via ClientSession.list_tools():
    python -m app.mcp_bridge.cli --fixture list-tools
    python -m app.mcp_bridge.cli --fixture --transport stdio list-tools

    # 2. Invoke an individual tool through MCP ClientSession.call_tool():
    python -m app.mcp_bridge.cli --fixture call resolve_location "{\\"description\\": \\"Landslide near NH29 kilometre 42.\\"}"

    # 3. Run the end-to-end CLI demonstration through MCP:
    python -m app.mcp_bridge.cli --fixture demo
    python -m app.mcp_bridge.cli --fixture --transport stdio demo
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..agent.cli import build_synthetic_fixture_dataset
from ..agent.llm import llm_client_from_env
from ..agent.orchestrator import LandslideAgent
from ..graph.errors import ToolError
from ..graph.in_memory import InMemoryGraph
from ..graph.store import FalkorStore
from .client import MCPToolClient


def _run_mcp_demo(agent: LandslideAgent, mcp_client: MCPToolClient, fmt: str, out) -> int:
    """Run the Section 11 end-to-end CLI demonstration through MCP."""
    tools = mcp_client.list_tools()

    # Step 1: Ambiguous event resolved through MCP (gated before apply_closure)
    r_ambig = agent.run("Landslide near NH29 kilometre 42.")
    cands = (r_ambig.get("resolution") or {}).get("candidate_segments") or []
    if not cands:
        out(json.dumps({"error": {"code": "empty_graph", "message": "No candidate segments found; seed the graph first."}}, indent=2))
        return 2

    # Step 2: Confirmed closure on detour-capable segment (s2) -> full MCP chain
    detour_seg = cands[1]["segment_id"] if len(cands) >= 2 else cands[0]["segment_id"]
    r_detour = agent.run("Landslide near NH29 kilometre 42.", target_segment_id=detour_seg, month=7)
    ev_detour = r_detour["closure"]["event"]["id"]
    r_detour_clear = agent.run(f"Clear closure {ev_detour} and audit")

    # Step 3: Confirmed closure on cut-off segment (s3) -> full MCP chain including draft_alerts
    cutoff_seg = cands[0]["segment_id"]
    r_cutoff = agent.run("Landslide near NH29 kilometre 42.", target_segment_id=cutoff_seg, month=8)
    ev_cutoff = r_cutoff["closure"]["event"]["id"]
    r_cutoff_clear = agent.run(f"Clear closure {ev_cutoff} and audit")

    steps = [
        {"step": 1, "title": "Ambiguous location resolution through MCP (requires clarification)", "report": r_ambig},
        {"step": 2, "title": f"Confirmed closure on {detour_seg} through MCP (alternate route & path explanation)", "report": r_detour},
        {"step": 3, "title": f"Clear & audit closure {ev_detour} through MCP", "report": r_detour_clear},
        {"step": 4, "title": f"Confirmed closure on {cutoff_seg} through MCP (cut-off settlements, hospitals & alerts)", "report": r_cutoff},
        {"step": 5, "title": f"Clear & audit closure {ev_cutoff} through MCP", "report": r_cutoff_clear},
    ]

    payload = {
        "demo": "Landslide Router Phase 4 End-to-End MCP Demonstration",
        "transport": mcp_client.transport_label,
        "server_info": mcp_client.server_info,
        "discovered_tool_count": len(tools),
        "discovered_tools": [t["name"] for t in tools],
        "total_mcp_handoffs": len(mcp_client.handoffs),
        "steps": steps,
    }

    if fmt == "json":
        out(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    out("=" * 78)
    out(f"MCP SERVER DISCOVERY ({mcp_client.transport_label}): {mcp_client.server_info['name']} v{mcp_client.server_info['version']}")
    out(f"Discovered {len(tools)} MCP tools: {', '.join(t['name'] for t in tools)}")
    out("=" * 78)
    for st in steps:
        out("")
        out("=" * 78)
        out(f"STEP {st['step']}: {st['title']}")
        out("=" * 78)
        out(st["report"]["markdown_report"].replace("\u2014", "--").replace("\u2013", "-"))
    return 0


def main(argv: list[str] | None = None, graph=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "command",
        nargs="?",
        default="demo",
        choices=["list-tools", "call", "demo", "agent"],
        help="MCP action to perform (default: demo)",
    )
    ap.add_argument("target", nargs="?", default=None, help="Tool name (for 'call') or natural-language prompt (for 'agent')")
    ap.add_argument("json_args", nargs="?", default="{}", help="JSON arguments object when command='call'")
    ap.add_argument("--transport", choices=["memory", "stdio"], default="memory", help="MCP transport ('memory' or 'stdio')")
    ap.add_argument("--timeout", type=float, default=15.0, help="Per-call MCP timeout in seconds (default: 15.0)")
    ap.add_argument("--target-segment", default=None, help="Confirmed RoadSegment ID for 'agent'")
    ap.add_argument("--target-bridge", default=None, help="Confirmed Bridge ID for 'agent'")
    ap.add_argument("--month", default=None, help="Optional month for seasonal risk")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format for demo/agent")
    ap.add_argument("--json", action="store_true", help="Shorthand for --format json")
    ap.add_argument("--graph", default=os.environ.get("FALKORDB_GRAPH", "landslide"), help="FalkorDB graph name")
    ap.add_argument("--dataset", type=Path, default=None, help="Run against a normalized.json dataset file")
    ap.add_argument("--fixture", action="store_true", help="Run against the deterministic synthetic test fixture")
    ap.add_argument("--planner", choices=["deterministic", "auto"], default="deterministic", help="Agent planner mode")
    a = ap.parse_args(argv)
    fmt = "json" if a.json else a.format

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

            graph = FalkorDB(
                host=os.environ.get("FALKORDB_HOST", "localhost"),
                port=int(os.environ.get("FALKORDB_PORT", "6379")),
                password=os.environ.get("FALKORDB_PASSWORD") or None,
            ).select_graph(a.graph)

    store = FalkorStore(graph)
    stdio_args: list[str] = []
    if a.fixture:
        stdio_args.append("--fixture")
    elif a.dataset is not None:
        stdio_args.extend(["--dataset", str(a.dataset.resolve())])
    else:
        stdio_args.extend(["--graph", a.graph])

    try:
        with MCPToolClient(store, transport=a.transport, timeout_s=a.timeout, stdio_args=stdio_args) as mcp_client:
            if a.command == "list-tools":
                tools = mcp_client.list_tools()
                out(
                    json.dumps(
                        {
                            "transport": mcp_client.transport_label,
                            "server_info": mcp_client.server_info,
                            "tool_count": len(tools),
                            "tools": tools,
                            "handoff": mcp_client.handoffs[-1],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 0

            if a.command == "call":
                if not a.target:
                    out(json.dumps({"error": {"code": "invalid_input", "message": "Specify a tool name for 'call'."}}, indent=2))
                    return 2
                try:
                    parsed_args = json.loads(a.json_args)
                    if not isinstance(parsed_args, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                except ValueError as exc:
                    out(json.dumps({"error": {"code": "bad_json", "message": str(exc)}}, indent=2))
                    return 2
                env = mcp_client.call_tool(
                    a.target,
                    parsed_args,
                    reason=f"Direct CLI MCP tool invocation of {a.target!r}.",
                )
                out(json.dumps(env, indent=2, ensure_ascii=False, default=str))
                return 0

            llm_client = llm_client_from_env() if a.planner == "auto" else None
            agent = LandslideAgent(store, llm_client=llm_client, mcp_client=mcp_client)

            if a.command == "demo":
                return _run_mcp_demo(agent, mcp_client, fmt, out)

            if not a.target or not a.target.strip():
                out(json.dumps({"error": {"code": "invalid_input", "message": "Provide a natural-language prompt for 'agent'."}}, indent=2))
                return 2

            report = agent.run(
                a.target,
                target_segment_id=a.target_segment,
                target_bridge_id=a.target_bridge,
                month=a.month,
            )
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


if __name__ == "__main__":
    sys.exit(main())

