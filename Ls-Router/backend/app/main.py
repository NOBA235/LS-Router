from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .db import check_falkordb

app = FastAPI(
    title="Landslide Router API",
    version=__version__,
    description="Phase 0 scaffold: health check only. Planning aid, not emergency dispatch.",
)


def falkordb_status() -> dict:
    """Dependency so tests can replace the real database check."""
    return check_falkordb(get_settings())


@app.get("/health", tags=["system"])
def health(falkordb: dict = Depends(falkordb_status)) -> JSONResponse:
    """200 when FalkorDB answers a query, 503 otherwise."""
    ok = falkordb["connected"] and falkordb["query_ok"]
    body = {
        "status": "ok" if ok else "degraded",
        "service": "landslide-router-api",
        "version": __version__,
        "falkordb": falkordb,
    }
    return JSONResponse(body, status_code=200 if ok else 503)
