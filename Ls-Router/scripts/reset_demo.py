"""Cleanly reset all active simulated closures and verify baseline graph restoration.

Usage (from repo root, with `backend\\.venv` active):

    # Reset live FalkorDB graph (configured in .env):
    python scripts\\reset_demo.py

    # Reset cached dataset / runtime history file:
    python scripts\\reset_demo.py --dataset data\\cache\\normalized.json
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
from app.demo import reset_demo_state  # noqa: E402
from app.graph.in_memory import InMemoryGraph  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=None, help="Reset against a normalized.json dataset file")
    ap.add_argument("--fixture", action="store_true", help="Reset against the synthetic test fixture")
    ap.add_argument("--graph", default=None, help="FalkorDB graph name (defaults to FALKORDB_GRAPH in .env)")
    args = ap.parse_args(argv)

    s = get_settings()
    if args.fixture:
        store = FalkorStore(InMemoryGraph(build_synthetic_fixture_dataset()))
    elif args.dataset is not None:
        ds_path = Path(args.dataset).expanduser().resolve()
        raw = json.loads(ds_path.read_text(encoding="utf-8"))
        store = FalkorStore(InMemoryGraph(raw, history_path=s.landslide_history_file))
    else:
        from falkordb import FalkorDB

        client = FalkorDB(
            host=s.falkordb_host,
            port=s.falkordb_port,
            username=s.falkordb_username or None,
            password=s.falkordb_password or None,
            ssl=s.falkordb_ssl,
            socket_timeout=s.falkordb_timeout_s,
            socket_connect_timeout=s.falkordb_timeout_s,
        )
        store = FalkorStore(client.select_graph(args.graph or s.falkordb_graph))

    res = reset_demo_state(store)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("back_to_baseline") else 1


if __name__ == "__main__":
    sys.exit(main())
