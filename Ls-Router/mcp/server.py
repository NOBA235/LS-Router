"""Standalone Model Context Protocol (MCP) server entrypoint for Landslide Router (Phase 4).

Usage (Windows Command Prompt, with `backend\\.venv` active):

    # Run stdio MCP server against live FalkorDB (for Claude Desktop, MCP Inspector, or stdio client):
    python ..\\mcp\\server.py

    # Run stdio MCP server against the deterministic offline synthetic fixture:
    python ..\\mcp\\server.py --fixture

    # Run stdio MCP server against a cached normalized corridor JSON:
    python ..\\mcp\\server.py --dataset ..\\data\\cache\\normalized.json

    # Inspect discovered MCP tools and JSON schemas:
    python ..\\mcp\\server.py --fixture --list-tools
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure repo root (`Ls-Router/`) cannot cause `Ls-Router/mcp/` to shadow the PyPI `mcp` package,
# and add `Ls-Router/backend/` to sys.path so `app.*` imports succeed from any working directory.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_BACKEND_DIR = _REPO_ROOT / "backend"

sys.path = [
    p for p in sys.path
    if Path(p or ".").resolve() not in (_THIS_DIR, _REPO_ROOT)
]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.agent.cli import build_synthetic_fixture_dataset  # noqa: E402
from app.graph.in_memory import InMemoryGraph  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402
from app.mcp_bridge.server import build_mcp_server  # noqa: E402


def _make_deterministic_helpers():
    n = 0
    t = 0

    def _id() -> str:
        nonlocal n
        n += 1
        return f"e{n}"

    def _clock() -> str:
        nonlocal t
        t += 1
        return f"2026-10-14T12:00:{t:02d}Z"

    return _clock, _id


def create_server_from_args(args: argparse.Namespace):
    clock = None
    id_factory = None
    if args.fixture:
        graph = InMemoryGraph(build_synthetic_fixture_dataset())
        clock, id_factory = _make_deterministic_helpers()
    elif args.dataset is not None:
        ds_path = Path(args.dataset)
        if not ds_path.exists():
            raise FileNotFoundError(f"Dataset file not found: {ds_path}")
        ds = json.loads(ds_path.read_text(encoding="utf-8"))
        graph = InMemoryGraph(ds)
    else:
        from falkordb import FalkorDB

        graph = FalkorDB(
            host=os.environ.get("FALKORDB_HOST", "localhost"),
            port=int(os.environ.get("FALKORDB_PORT", "6379")),
            password=os.environ.get("FALKORDB_PASSWORD") or None,
        ).select_graph(args.graph)

    store = FalkorStore(graph)
    return build_mcp_server(
        store,
        clock=clock,
        id_factory=id_factory,
        simulated_delay_s=(args.delay_ms / 1000.0) if args.delay_ms else 0.0,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio", help="MCP transport")
    ap.add_argument("--graph", default=os.environ.get("FALKORDB_GRAPH", "landslide"), help="FalkorDB graph name")
    ap.add_argument("--dataset", type=Path, default=None, help="Run offline against a normalized.json dataset file")
    ap.add_argument("--fixture", action="store_true", help="Run offline against the deterministic synthetic test fixture")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="Optional artificial delay per tool call in ms (for timeout tests)")
    ap.add_argument("--list-tools", action="store_true", help="Discover and print exposed MCP tools as JSON, then exit")
    args = ap.parse_args(argv)

    try:
        mcp_srv = create_server_from_args(args)
    except Exception as exc:
        sys.stderr.write(json.dumps({"error": {"code": "server_init_failed", "message": str(exc)}}) + "\n")
        return 2

    if args.list_tools:
        from app.mcp_bridge.client import MCPToolClient

        client = MCPToolClient(mcp_srv.store, mcp_server=mcp_srv)
        tools = client.list_tools()
        print(json.dumps({"server": mcp_srv.name, "tool_count": len(tools), "tools": tools}, indent=2))
        return 0

    mcp_srv.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())

