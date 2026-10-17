from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from falkordb import FalkorDB

from .config import Settings, get_settings
from .graph.errors import StoreError
from .graph.in_memory import InMemoryGraph
from .graph.store import FalkorStore

_OFFLINE_GRAPH_CACHE: dict[str, InMemoryGraph] = {}


def reset_offline_graph_cache() -> None:
    """Clear cached in-memory graphs used when LANDSLIDE_USE_FIXTURE or LANDSLIDE_DATASET_PATH is set."""
    _OFFLINE_GRAPH_CACHE.clear()


def _get_offline_graph(settings: Settings) -> InMemoryGraph:
    if settings.landslide_use_fixture:
        key = "__fixture__"
        if key not in _OFFLINE_GRAPH_CACHE:
            from .agent.cli import build_synthetic_fixture_dataset

            _OFFLINE_GRAPH_CACHE[key] = InMemoryGraph(build_synthetic_fixture_dataset())
        return _OFFLINE_GRAPH_CACHE[key]

    ds_path = Path(settings.landslide_dataset_path or "").expanduser().resolve()
    key = str(ds_path)
    if key not in _OFFLINE_GRAPH_CACHE:
        if not ds_path.exists():
            raise StoreError(f"Configured LANDSLIDE_DATASET_PATH does not exist: {ds_path}")
        try:
            raw = json.loads(ds_path.read_text(encoding="utf-8"))
            _OFFLINE_GRAPH_CACHE[key] = InMemoryGraph(raw)
        except Exception as exc:
            raise StoreError(f"Failed to load dataset from {ds_path}: {exc}") from exc
    return _OFFLINE_GRAPH_CACHE[key]


def check_falkordb(settings: Settings) -> dict:
    """Connect to FalkorDB and run `RETURN 1`. Never raises; failures go into `error`.

    Note: GRAPH.QUERY creates the named graph key (empty) if it does not exist yet.
    """
    status = {
        "connected": False,
        "graph": settings.falkordb_graph,
        "query_ok": False,
        "latency_ms": None,
        "error": None,
    }
    started = time.perf_counter()
    if settings.landslide_use_fixture or settings.landslide_dataset_path:
        try:
            g = _get_offline_graph(settings)
            result = g.query("RETURN 1 AS ok")
            status["connected"] = True
            status["query_ok"] = bool(result.result_set) and result.result_set[0][0] == 1
            status["mode"] = "offline_fixture" if settings.landslide_use_fixture else "offline_dataset"
        except Exception as exc:
            status["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            status["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return status

    client = None
    try:
        client = FalkorDB(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password or None,
            socket_timeout=settings.falkordb_timeout_s,
            socket_connect_timeout=settings.falkordb_timeout_s,
        )
        result = client.select_graph(settings.falkordb_graph).query("RETURN 1 AS ok")
        status["connected"] = True
        status["query_ok"] = bool(result.result_set) and result.result_set[0][0] == 1
    except Exception as exc:  # report, don't crash the health endpoint
        status["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        status["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if client is not None:
            try:
                client.connection.close()
            except Exception:
                pass
    return status


def get_store() -> Iterator[FalkorStore]:
    """FastAPI dependency yielding a FalkorStore connected to FalkorDB (or configured offline dataset)."""
    settings = get_settings()
    if settings.landslide_use_fixture or settings.landslide_dataset_path:
        yield FalkorStore(_get_offline_graph(settings))
        return

    client = None
    try:
        client = FalkorDB(
            host=settings.falkordb_host,
            port=settings.falkordb_port,
            password=settings.falkordb_password or None,
            socket_timeout=settings.falkordb_timeout_s,
            socket_connect_timeout=settings.falkordb_timeout_s,
        )
        graph = client.select_graph(settings.falkordb_graph)
    except Exception as exc:
        raise StoreError(
            f"Could not connect to FalkorDB at {settings.falkordb_host}:{settings.falkordb_port}: {exc}"
        ) from exc

    try:
        yield FalkorStore(graph)
    finally:
        if client is not None:
            try:
                client.connection.close()
            except Exception:
                pass

