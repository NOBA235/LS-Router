"""Run the reproducible 12-step demonstration from the Windows Command Prompt.

Usage (from repo root, with `backend\\.venv` active):

    # Run against live FalkorDB (configured in .env):
    python scripts\\run_demo.py

    # Run against the cached real OpenStreetMap NH29 dataset (zero-Docker / offline):
    python scripts\\run_demo.py --dataset data\\cache\\normalized.json

    # Run against the deterministic synthetic fixture:
    python scripts\\run_demo.py --fixture

    # Output full JSON report:
    python scripts\\run_demo.py --dataset data\\cache\\normalized.json --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.agent.cli import build_synthetic_fixture_dataset  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.demo import run_twelve_step_demo  # noqa: E402
from app.graph.in_memory import InMemoryGraph  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402


def _build_store(args: argparse.Namespace) -> FalkorStore:
    if args.fixture:
        return FalkorStore(InMemoryGraph(build_synthetic_fixture_dataset()))
    if args.dataset is not None:
        ds_path = Path(args.dataset).expanduser().resolve()
        if not ds_path.exists():
            raise FileNotFoundError(f"Dataset file not found: {ds_path}")
        raw = json.loads(ds_path.read_text(encoding="utf-8"))
        return FalkorStore(InMemoryGraph(raw))

    from falkordb import FalkorDB

    s = get_settings()
    client = FalkorDB(
        host=s.falkordb_host,
        port=s.falkordb_port,
        username=s.falkordb_username or None,
        password=s.falkordb_password or None,
        ssl=s.falkordb_ssl,
        socket_timeout=s.falkordb_timeout_s,
        socket_connect_timeout=s.falkordb_timeout_s,
    )
    return FalkorStore(client.select_graph(args.graph or s.falkordb_graph))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=None, help="Run against a normalized.json dataset file in memory")
    ap.add_argument("--fixture", action="store_true", help="Run against the deterministic synthetic test fixture")
    ap.add_argument("--graph", default=None, help="FalkorDB graph name (defaults to FALKORDB_GRAPH in .env)")
    ap.add_argument("--skip-weak-links", action="store_true", help="Skip full betweenness centrality in Step 10")
    ap.add_argument("--json", action="store_true", help="Print full JSON output instead of human-readable report")
    args = ap.parse_args(argv)

    store = _build_store(args)
    report = run_twelve_step_demo(store, include_weak_links=not args.skip_weak_links)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print("=" * 80)
    print("LANDSLIDE ROUTER -- REPRODUCIBLE 12-STEP DEMONSTRATION REPORT")
    print("=" * 80)
    print(f"Safety Disclaimer: {report['disclaimer']}")
    print(
        f"Corridor: {report['targets']['corridor_id']} | "
        f"Steps: {report['step_count']} | "
        f"Total Elapsed: {report['total_elapsed_seconds']:.2f}s "
        f"(Under 3-Minute Limit: {report['under_three_minutes']})"
    )
    print(f"MCP Tool Invocations Logged: {report['mcp_handoff_count']} | Back to Baseline: {report['back_to_baseline']}")
    print("-" * 80)
    for st in report["steps"]:
        title = st["title"].replace("\u2014", "--").replace("\u2013", "-")
        summary = st["summary"].replace("\u2014", "--").replace("\u2013", "-")
        print(f"[Step {st['step']:02d}/{report['step_count']}] {title} ({st['elapsed_seconds']:.2f}s)")
        print(f"  {summary}")
        print("-" * 80)

    return 0 if report["under_three_minutes"] and report["back_to_baseline"] else 1


if __name__ == "__main__":
    sys.exit(main())
