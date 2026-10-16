import time

from falkordb import FalkorDB

from .config import Settings


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
