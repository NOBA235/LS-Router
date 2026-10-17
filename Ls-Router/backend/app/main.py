"""FastAPI HTTP API for Landslide Router (Phase 5).

Authoritative HTTP interface over the seeded Dimapur-Kohima NH29 corridor graph:
- `GET /health`
- `POST /closure`
- `GET /graph`
- `GET /cutoff/{event_id}`
- `GET /reroute`
- `GET /weak-links`
- `GET /seasonal-risk`
- `POST /alerts/{event_id}`
- `POST /resolve/{event_id}`

Every endpoint executes real graph queries via `FalkorStore`, preserves executed Cypher
statements and provenance in the response, and never fabricates graph facts.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .agent.resolver import LocationResolver
from .config import get_settings
from .db import check_falkordb, get_store
from .graph.errors import (
    StoreError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
    ToolStateError,
)
from .graph.store import FalkorStore
from .graph.tools import Toolbox
from .graph.view import build_graph_view
from .privacy import redact_sensitive_text, sanitize_payload

SAFETY_DISCLAIMER = (
    "Planning aid, not emergency dispatch. Routes and risk estimates are based on available "
    "data and simulations. Verify conditions with official authorities before acting."
)

DEFAULT_CORRIDOR_ID = "nh29-dimapur-kohima"

_settings = get_settings()

app = FastAPI(
    title="Landslide Router API",
    version=__version__,
    description=(
        "Graph-powered disaster-routing API for the Dimapur–Kohima NH29 corridor in Nagaland, India. "
        f"{SAFETY_DISCLAIMER}"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def falkordb_status() -> dict:
    """Dependency so tests can replace the real database health check."""
    return check_falkordb(get_settings())


def get_toolbox(store: FalkorStore = Depends(get_store)) -> Toolbox:
    """Dependency returning a Toolbox bound to the active FalkorStore."""
    return Toolbox(store)


def get_resolver(store: FalkorStore = Depends(get_store)) -> LocationResolver:
    """Dependency returning a LocationResolver bound to the active FalkorStore."""
    return LocationResolver(store)


# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------
class ClosureRequest(BaseModel):
    """Request body for `POST /closure`."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "description": "Landslide on NH29 between Piphema and Chümoukedima",
                    "lat": None,
                    "lon": None,
                },
                {
                    "description": "Landslide near NH29 kilometre 42",
                    "target_segment_id": "seg-nh29-001",
                    "cause": "landslide",
                },
            ]
        },
    )

    description: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Natural-language incident description (e.g. 'Landslide at NH29 near kilometre 42.')",
    )
    lat: float | None = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="Optional incident latitude in decimal degrees [-90, 90]. Must be paired with 'lon'.",
    )
    lon: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Optional incident longitude in decimal degrees [-180, 180]. Must be paired with 'lat'.",
    )
    target_segment_id: str | None = Field(
        default=None,
        max_length=200,
        description="Explicit RoadSegment ID when confirming an ambiguous location or selecting a known segment.",
    )
    target_bridge_id: str | None = Field(
        default=None,
        max_length=200,
        description="Explicit Bridge ID when confirming or targeting a bridge closure.",
    )
    cause: str | None = Field(
        default=None,
        max_length=50,
        description="Optional override for closure cause (defaults to cause parsed from description, e.g. 'landslide').",
    )
    max_candidates: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of candidate segments/bridges returned when clarification is required.",
    )
    remember_in_history: bool = Field(
        default=False,
        description="If true and the closure is applied, also mark the simulated event as remembered in history.",
    )
    started_at: str | None = Field(
        default=None,
        max_length=64,
        description="Optional ISO-8601 start date/time for simulated scenario (e.g. '2026-07-15T12:00:00Z' or '2026-07-15').",
    )

    @model_validator(mode="after")
    def _validate_and_clean(self) -> ClosureRequest:
        cleaned_desc = redact_sensitive_text(self.description.strip())
        if not cleaned_desc:
            raise ValueError("'description' must be a non-empty string")
        self.description = cleaned_desc

        if (self.lat is None) != (self.lon is None):
            raise ValueError("Both 'lat' and 'lon' must be provided together, or both omitted")
        if self.lat is not None and self.lon is not None:
            if not (math.isfinite(self.lat) and math.isfinite(self.lon)):
                raise ValueError("'lat' and 'lon' must be finite numbers")

        for attr in ("target_segment_id", "target_bridge_id", "cause", "started_at"):
            val = getattr(self, attr)
            if val is not None:
                cleaned = redact_sensitive_text(val.strip())
                setattr(self, attr, cleaned if cleaned else None)
        return self


class ResolveClosureRequest(BaseModel):
    """Optional request body for `POST /resolve/{event_id}`."""

    model_config = ConfigDict(extra="forbid")

    remember_in_history: bool = Field(
        default=False,
        description="If true, also mark the resolved simulated event as remembered in history before auditing.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _merge_graphs(*graphs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges_by_id: dict[str, dict[str, Any]] = {}
    for g in graphs:
        if not isinstance(g, dict):
            continue
        for n in g.get("nodes", []):
            nid = n.get("id")
            if nid:
                if nid not in nodes_by_id or (not nodes_by_id[nid].get("role") and n.get("role")):
                    nodes_by_id[nid] = n
        for e in g.get("edges", []):
            eid = e.get("id")
            if eid:
                if eid not in edges_by_id or e.get("status") == "blocked":
                    edges_by_id[eid] = e
    return {
        "nodes": [nodes_by_id[k] for k in sorted(nodes_by_id)],
        "edges": [edges_by_id[k] for k in sorted(edges_by_id)],
    }


def _error_response(status_code: int, code: str, exc_type: str, message: str, details: Any = None) -> JSONResponse:
    safe_msg = redact_sensitive_text(str(message))
    err_obj: dict[str, Any] = {
        "code": code,
        "type": exc_type,
        "message": safe_msg,
        "status_code": status_code,
    }
    if details is not None:
        err_obj["details"] = sanitize_payload(details)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": err_obj,
            "detail": safe_msg,
            "disclaimer": SAFETY_DISCLAIMER,
        },
    )


# ---------------------------------------------------------------------------
# Exception Handlers
# ---------------------------------------------------------------------------
@app.exception_handler(ToolNotFoundError)
async def handle_not_found(_request: Request, exc: ToolNotFoundError) -> JSONResponse:
    return _error_response(404, "not_found", type(exc).__name__, str(exc))


@app.exception_handler(ToolInputError)
async def handle_input_error(_request: Request, exc: ToolInputError) -> JSONResponse:
    return _error_response(422, exc.code, type(exc).__name__, str(exc))


@app.exception_handler(ToolStateError)
async def handle_state_error(_request: Request, exc: ToolStateError) -> JSONResponse:
    return _error_response(409, exc.code, type(exc).__name__, str(exc))


@app.exception_handler(StoreError)
async def handle_store_error(_request: Request, exc: StoreError) -> JSONResponse:
    return _error_response(503, exc.code, type(exc).__name__, str(exc))


@app.exception_handler(ToolError)
async def handle_generic_tool_error(_request: Request, exc: ToolError) -> JSONResponse:
    return _error_response(400, exc.code, type(exc).__name__, str(exc))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    cleaned_errors = []
    messages = []
    for err in errors:
        loc = ".".join(str(x) for x in err.get("loc", []))
        msg = err.get("msg", "Invalid value")
        messages.append(f"{loc}: {msg}" if loc else msg)
        cleaned_errors.append(
            {
                "loc": list(err.get("loc", [])),
                "msg": msg,
                "type": err.get("type", "value_error"),
            }
        )
    summary = "; ".join(messages) if messages else "Request validation failed"
    return _error_response(422, "validation_error", "RequestValidationError", summary, details=cleaned_errors)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["system"], summary="Check API and FalkorDB connectivity")
def health(falkordb: dict = Depends(falkordb_status)) -> JSONResponse:
    """200 when FalkorDB answers a query, 503 otherwise."""
    ok = bool(falkordb.get("connected") and falkordb.get("query_ok"))
    body = {
        "status": "ok" if ok else "degraded",
        "service": "landslide-router-api",
        "version": __version__,
        "falkordb": falkordb,
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get(
    "/graph",
    tags=["corridor"],
    summary="Return the corridor graph with places, road segments, hospitals, bridges, provenance, and closure status",
)
def get_graph(
    include_geometry: bool = Query(
        default=True,
        description="Include parsed GeoJSON coordinate arrays [[lon, lat], ...] on road segments.",
    ),
    store: FalkorStore = Depends(get_store),
) -> dict[str, Any]:
    """Read the full corridor graph from FalkorDB and return entities, provenance, geometry, and closure state."""
    store.reset_log()
    snap = store.load_snapshot()
    details = store.load_details()
    log = store.drain_log()

    view = build_graph_view(snap, details, include_geometry=include_geometry)
    payload = {
        **view,
        "cypher": [q["query"] for q in log],
        "params": {
            "inputs": {"include_geometry": include_geometry},
            "query_names": [q["name"] for q in log],
            "queries": [q["params"] for q in log],
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.post(
    "/closure",
    tags=["closures"],
    summary="Resolve a natural-language incident description and apply an authorized simulated road closure",
)
def create_closure(
    req: ClosureRequest,
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Resolve an incident target against the corridor graph and apply a simulated closure when unambiguous.

    Enforces Section 10 of the specification:
    - If `resolve_location` returns `status == 'ambiguous'` (e.g., chainage/kilometre marker,
      coordinate-only proximity, or multiple candidate segments), the graph is NOT mutated and
      the endpoint returns `status = 'clarification_required'` with candidate segments/bridges.
    - If `resolve_location` returns `status == 'no_match'`, the graph is NOT mutated and the
      endpoint returns `status = 'no_match'`.
    - When `resolve_location` resolves unambiguously (or `target_segment_id` / `target_bridge_id`
      is explicitly provided), `apply_closure` creates the `ClosureEvent` node and `BLOCKS` edge(s).
    """
    resolver = LocationResolver(toolbox.store)
    res_env = resolver.resolve_location(
        description=req.description,
        lat=req.lat,
        lon=req.lon,
        target_segment_id=req.target_segment_id,
        target_bridge_id=req.target_bridge_id,
        max_candidates=req.max_candidates,
    )
    res_result = res_env["result"]
    res_status = res_result["status"]

    if res_status == "ambiguous":
        payload = {
            "status": "clarification_required",
            "requires_clarification": True,
            "created": False,
            "event_id": None,
            "event": None,
            "target_segment": None,
            "location_confidence": None,
            "state_change": None,
            "resolution": res_result,
            "closure": None,
            "remembered": None,
            "explanation": res_env["explanation"],
            "cypher": res_env["cypher"],
            "params": {
                "resolve_location": res_env["params"],
                "apply_closure": None,
            },
            "graph": res_env["graph"],
            "warnings": res_env["warnings"],
            "algorithm": {
                "resolve_location": res_env["algorithm"],
                "apply_closure": None,
            },
            "disclaimer": SAFETY_DISCLAIMER,
        }
        return sanitize_payload(payload)

    if res_status == "no_match":
        payload = {
            "status": "no_match",
            "requires_clarification": False,
            "created": False,
            "event_id": None,
            "event": None,
            "target_segment": None,
            "location_confidence": None,
            "state_change": None,
            "resolution": res_result,
            "closure": None,
            "remembered": None,
            "explanation": res_env["explanation"],
            "cypher": res_env["cypher"],
            "params": {
                "resolve_location": res_env["params"],
                "apply_closure": None,
            },
            "graph": res_env["graph"],
            "warnings": res_env["warnings"],
            "algorithm": {
                "resolve_location": res_env["algorithm"],
                "apply_closure": None,
            },
            "disclaimer": SAFETY_DISCLAIMER,
        }
        return sanitize_payload(payload)

    selected = res_result["selected_target"]
    segment_id = selected["segment_id"]
    bridge_id = req.target_bridge_id or selected.get("bridge_id")
    cause = req.cause or res_result.get("parsed_event", {}).get("cause") or "landslide"

    closure_env = toolbox.apply_closure(
        segment_id=segment_id,
        description=req.description,
        cause=cause,
        bridge_id=bridge_id,
        started_at=req.started_at,
    )
    closure_result = closure_env["result"]
    event_id = closure_result["event"]["id"]

    remember_env = toolbox.remember_closure(event_id=event_id) if req.remember_in_history else None

    cypher = list(res_env["cypher"]) + list(closure_env["cypher"])
    warnings = list(res_env["warnings"]) + list(closure_env["warnings"])
    explanation_parts = [res_env["explanation"], closure_env["explanation"]]
    if remember_env is not None:
        cypher.extend(remember_env["cypher"])
        warnings.extend(remember_env["warnings"])
        explanation_parts.append(remember_env["explanation"])

    payload = {
        "status": "applied",
        "requires_clarification": False,
        "created": closure_result["created"],
        "event_id": event_id,
        "event": closure_result["event"],
        "target_segment": closure_result["target_segment"],
        "location_confidence": closure_result["location_confidence"],
        "state_change": closure_result["state_change"],
        "resolution": res_result,
        "closure": closure_result,
        "remembered": remember_env["result"] if remember_env else None,
        "explanation": " ".join(explanation_parts),
        "cypher": cypher,
        "params": {
            "resolve_location": res_env["params"],
            "apply_closure": closure_env["params"],
            **({"remember_closure": remember_env["params"]} if remember_env else {}),
        },
        "graph": closure_env["graph"],
        "warnings": _dedupe_strings(warnings),
        "algorithm": {
            "resolve_location": res_env["algorithm"],
            "apply_closure": closure_env["algorithm"],
            **({"remember_closure": remember_env["algorithm"]} if remember_env else {}),
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.get(
    "/cutoff/{event_id}",
    tags=["connectivity"],
    summary="Return newly cut-off settlements and hospital reachability impact for a closure event",
)
def get_cutoff(
    event_id: str = Path(..., min_length=1, max_length=200, description="ClosureEvent ID (e.g. 'sim-...')"),
    threshold_min: float | None = Query(
        default=None,
        gt=0.0,
        description="Optional travel-time threshold in minutes for hospital reachability evaluation.",
    ),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Compute directed reachability from Dimapur, component splits, and hospital reachability for `event_id`."""
    cleaned_event_id = redact_sensitive_text(event_id.strip())
    if not cleaned_event_id:
        raise ToolInputError("'event_id' must be a non-empty string")

    cutoff_env = toolbox.find_cut_off_places(closure_event_id=cleaned_event_id)
    hosp_env = toolbox.find_unreachable_hospitals(
        closure_event_id=cleaned_event_id,
        threshold_min=threshold_min,
    )

    cutoff_res = cutoff_env["result"]
    hosp_res = hosp_env["result"]

    payload = {
        "event_id": cleaned_event_id,
        **cutoff_res,
        "cut_off_places": cutoff_res,
        "hospital_reachability": hosp_res,
        "explanation": f"{cutoff_env['explanation']} {hosp_env['explanation']}",
        "cypher": list(cutoff_env["cypher"]) + list(hosp_env["cypher"]),
        "params": {
            "find_cut_off_places": cutoff_env["params"],
            "find_unreachable_hospitals": hosp_env["params"],
        },
        "graph": _merge_graphs(cutoff_env["graph"], hosp_env["graph"]),
        "warnings": _dedupe_strings(list(cutoff_env["warnings"]) + list(hosp_env["warnings"])),
        "algorithm": {
            "find_cut_off_places": cutoff_env["algorithm"],
            "find_unreachable_hospitals": hosp_env["algorithm"],
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.get(
    "/reroute",
    tags=["routing"],
    summary="Calculate the fastest valid route avoiding blocked segments and explain its path weights",
)
def get_reroute(
    from_place: str | None = Query(
        default=None,
        alias="from",
        max_length=200,
        description="Origin Place ID (e.g. 'place-dimapur').",
    ),
    to_place: str | None = Query(
        default=None,
        alias="to",
        max_length=200,
        description="Destination Place ID (e.g. 'place-kohima').",
    ),
    from_place_id: str | None = Query(
        default=None,
        max_length=200,
        description="Alternative query parameter name for origin Place ID.",
    ),
    to_place_id: str | None = Query(
        default=None,
        max_length=200,
        description="Alternative query parameter name for destination Place ID.",
    ),
    avoid_event_id: str | None = Query(
        default=None,
        max_length=200,
        description="Optional ClosureEvent ID whose blocked segments should also be excluded.",
    ),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Compute a minimum-travel-time route (`reroute`) and per-segment breakdown (`explain_path`)."""
    raw_from = from_place if from_place is not None else from_place_id
    raw_to = to_place if to_place is not None else to_place_id

    if raw_from is None or not raw_from.strip():
        raise ToolInputError("Query parameter 'from' (or 'from_place_id') is required and must be non-empty.")
    if raw_to is None or not raw_to.strip():
        raise ToolInputError("Query parameter 'to' (or 'to_place_id') is required and must be non-empty.")

    origin_id = redact_sensitive_text(raw_from.strip())
    dest_id = redact_sensitive_text(raw_to.strip())
    avoid_id = redact_sensitive_text(avoid_event_id.strip()) if (avoid_event_id and avoid_event_id.strip()) else None

    reroute_env = toolbox.reroute(
        from_place_id=origin_id,
        to_place_id=dest_id,
        avoid_event_id=avoid_id,
    )
    explain_env = toolbox.explain_path(
        from_place_id=origin_id,
        to_place_id=dest_id,
        avoid_event_id=avoid_id,
    )

    reroute_res = reroute_env["result"]
    explain_res = explain_env["result"]

    payload = {
        **reroute_res,
        "reroute": reroute_res,
        "path_explanation": explain_res,
        "explanation": f"{reroute_env['explanation']} {explain_env['explanation']}",
        "cypher": list(reroute_env["cypher"]) + list(explain_env["cypher"]),
        "params": {
            "reroute": reroute_env["params"],
            "explain_path": explain_env["params"],
        },
        "graph": reroute_env["graph"],
        "warnings": _dedupe_strings(list(reroute_env["warnings"]) + list(explain_env["warnings"])),
        "algorithm": {
            "reroute": reroute_env["algorithm"],
            "explain_path": explain_env["algorithm"],
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.get(
    "/weak-links",
    tags=["risk"],
    summary="Return top-ranked corridor weak links (segments, bridges, and places) by betweenness centrality",
)
def get_weak_links(
    corridor_id: str | None = Query(
        default=None,
        max_length=200,
        description="Corridor ID (defaults to 'nh29-dimapur-kohima' or the primary corridor in the graph).",
    ),
    top_k: int = Query(
        default=5,
        ge=1,
        le=200,
        description="Maximum number of ranked segments, bridges, and places to return (1-200).",
    ),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Compute shortest-path betweenness centrality and articulation points on the corridor network."""
    if corridor_id is not None and corridor_id.strip():
        resolved_corridor = redact_sensitive_text(corridor_id.strip())
    else:
        snap = toolbox.store.load_snapshot()
        corridors = snap.corridors()
        if DEFAULT_CORRIDOR_ID in corridors:
            resolved_corridor = DEFAULT_CORRIDOR_ID
        elif corridors:
            resolved_corridor = sorted(corridors)[0]
        else:
            resolved_corridor = DEFAULT_CORRIDOR_ID

    env = toolbox.find_weak_links(corridor_id=resolved_corridor, top_k=top_k)
    res = env["result"]
    payload = {
        **res,
        **env,
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.get(
    "/seasonal-risk",
    tags=["risk"],
    summary="Return the documented seasonal monsoon risk ranking for a given month",
)
def get_seasonal_risk(
    month: str = Query(
        ...,
        min_length=1,
        max_length=20,
        description="Month number (1-12) or month name (e.g. '7', 'jul', 'july').",
    ),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Rank corridor segments and bridges for `month` using risk labels and in-season closure history."""
    cleaned_month: int | str = redact_sensitive_text(month.strip())
    if isinstance(cleaned_month, str) and cleaned_month.isdigit():
        cleaned_month = int(cleaned_month)

    env = toolbox.seasonal_risk(month=cleaned_month)
    res = env["result"]
    payload = {
        **res,
        **env,
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.post(
    "/alerts/{event_id}",
    tags=["alerts"],
    summary="Generate synthetic SMS draft alerts for communities cut off by a closure event",
)
def create_draft_alerts(
    event_id: str = Path(..., min_length=1, max_length=200, description="ClosureEvent ID"),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Create and persist synthetic draft alerts (`status='draft'`, `sent=False`). Never dispatches messages."""
    cleaned_event_id = redact_sensitive_text(event_id.strip())
    if not cleaned_event_id:
        raise ToolInputError("'event_id' must be a non-empty string")

    env = toolbox.draft_alerts(closure_event_id=cleaned_event_id)
    res = env["result"]
    payload = {
        "event_id": cleaned_event_id,
        **res,
        **env,
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.post(
    "/resolve/{event_id}",
    tags=["closures"],
    summary="Resolve a simulated closure and audit that the network state returned to baseline",
)
def resolve_closure_endpoint(
    event_id: str = Path(..., min_length=1, max_length=200, description="Simulated ClosureEvent ID to resolve"),
    remember_in_history: bool = Query(
        default=False,
        description="If true, also mark the resolved event as remembered in history before auditing.",
    ),
    body: ResolveClosureRequest | None = Body(default=None),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Resolve `event_id` (`clear_closure`) and verify network integrity (`audit_closure`) while preserving history."""
    cleaned_event_id = redact_sensitive_text(event_id.strip())
    if not cleaned_event_id:
        raise ToolInputError("'event_id' must be a non-empty string")

    want_remember = bool(remember_in_history or (body is not None and body.remember_in_history))

    clear_env = toolbox.clear_closure(event_id=cleaned_event_id)
    remember_env = toolbox.remember_closure(event_id=cleaned_event_id) if want_remember else None
    audit_env = toolbox.audit_closure(event_id=cleaned_event_id)

    clear_res = clear_env["result"]
    audit_res = audit_env["result"]

    cypher = list(clear_env["cypher"])
    warnings = list(clear_env["warnings"])
    explanation_parts = [clear_env["explanation"]]
    if remember_env is not None:
        cypher.extend(remember_env["cypher"])
        warnings.extend(remember_env["warnings"])
        explanation_parts.append(remember_env["explanation"])
    cypher.extend(audit_env["cypher"])
    warnings.extend(audit_env["warnings"])
    explanation_parts.append(audit_env["explanation"])

    payload = {
        "event_id": cleaned_event_id,
        "event": audit_res["event"],
        "resolved": audit_res["resolved"],
        "returned_to_baseline": audit_res["returned_to_baseline"],
        "restored_segments": clear_res["restored_segments"],
        "still_blocked_by_other_closures": clear_res["still_blocked_by_other_closures"],
        "history_preserved": clear_res["history_preserved"],
        "clearance": clear_res,
        "audit": audit_res,
        "remembered": remember_env["result"] if remember_env else None,
        "explanation": " ".join(explanation_parts),
        "cypher": cypher,
        "params": {
            "clear_closure": clear_env["params"],
            **({"remember_closure": remember_env["params"]} if remember_env else {}),
            "audit_closure": audit_env["params"],
        },
        "graph": clear_env["graph"],
        "warnings": _dedupe_strings(warnings),
        "algorithm": {
            "clear_closure": clear_env["algorithm"],
            **({"remember_closure": remember_env["algorithm"]} if remember_env else {}),
            "audit_closure": audit_env["algorithm"],
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.post(
    "/remember/{event_id}",
    tags=["history"],
    summary="Persist a closure event in historical memory and idempotently record bridge failure links",
)
def remember_closure_endpoint(
    event_id: str = Path(..., min_length=1, max_length=200, description="ClosureEvent ID to remember"),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Mark `event_id` as remembered and record `(:Bridge)-[:FAILED_IN]->(:ClosureEvent)` where supported."""
    cleaned_event_id = redact_sensitive_text(event_id.strip())
    if not cleaned_event_id:
        raise ToolInputError("'event_id' must be a non-empty string")

    env = toolbox.remember_closure(event_id=cleaned_event_id)
    res = env["result"]
    payload = {
        "event_id": cleaned_event_id,
        **res,
        **env,
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.get(
    "/history",
    tags=["history"],
    summary="Return stored observed and simulated closure history, chronological timeline, and bridge failure records",
)
def get_history(
    store: FalkorStore = Depends(get_store),
) -> dict[str, Any]:
    """Read stored ClosureEvent nodes and FAILED_IN bridge failure links, visibly separating observed from simulated history."""
    store.reset_log()
    snap = store.load_snapshot()
    log = store.drain_log()

    bridge_failures = []
    for bid, eid, year in sorted(snap.failed_in):
        ev = snap.events.get(eid)
        br = snap.bridges.get(bid)
        ds = ev.data_status if ev else None
        bridge_failures.append({
            "bridge_id": bid,
            "bridge_name": br.name if br else None,
            "event_id": eid,
            "year": year,
            "event_data_status": ds,
            "history_label": (
                "SIMULATED (demonstration scenario)"
                if ds == "simulated"
                else "OBSERVED (historical report)"
                if ds == "observed"
                else "UNKNOWN"
            ),
        })

    observed_events = []
    simulated_events = []
    all_rows = []
    for e in sorted(snap.events.values(), key=lambda x: x.id):
        is_sim = e.data_status == "simulated"
        seg_details = [
            {
                "id": sid,
                "name": snap.segments[sid].name,
                "from_place_id": snap.segments[sid].from_place_id,
                "to_place_id": snap.segments[sid].to_place_id,
                "on_nh29": snap.segments[sid].on_nh29,
                "confidence": snap.segments[sid].confidence,
            }
            for sid in e.blocks_segments
            if sid in snap.segments
        ]
        br_details = [
            {
                "id": bid,
                "name": snap.bridges[bid].name,
                "confidence": snap.bridges[bid].confidence,
            }
            for bid in e.blocks_bridges
            if bid in snap.bridges
        ]
        row = {
            "id": e.id,
            "description": e.description,
            "status": e.status,
            "data_status": e.data_status,
            "history_label": "SIMULATED (demonstration scenario)" if is_sim else "OBSERVED (historical report)",
            "remembered": bool(e.remembered_at),
            "remembered_at": e.remembered_at,
            "cause": e.cause,
            "origin": e.origin,
            "confidence": e.confidence,
            "started_at": e.started_at,
            "ended_at": e.ended_at,
            "location_text": e.location_text,
            "source_url": e.source_url,
            "blocks_segments": list(e.blocks_segments),
            "blocks_bridges": list(e.blocks_bridges),
            "near_places": list(e.near_places),
            "segment_details": seg_details,
            "bridge_details": br_details,
            "failed_bridges": [f for f in bridge_failures if f["event_id"] == e.id],
        }
        all_rows.append(row)
        if is_sim:
            simulated_events.append(row)
        else:
            observed_events.append(row)

    # Chronological timeline ordered by started_at (undated items last, then by id)
    timeline_sorted = sorted(
        all_rows,
        key=lambda r: (0 if r.get("started_at") else 1, r.get("started_at") or "", r["id"]),
    )
    timeline = [{**item, "index": idx} for idx, item in enumerate(timeline_sorted, 1)]

    warnings = [
        "Simulated closures and simulated bridge failures are demonstration data and remain strictly separated from observed history.",
        "Historical counts are evidence to review; no future road or bridge failure is predicted.",
    ]
    payload = {
        "timeline": timeline,
        "observed_events": observed_events,
        "simulated_events": simulated_events,
        "bridge_failures": bridge_failures,
        "counts": {
            "total_events": len(all_rows),
            "observed_events": len(observed_events),
            "simulated_events": len(simulated_events),
            "active_simulated": sum(1 for e in simulated_events if e["status"] == "active"),
            "resolved_simulated": sum(1 for e in simulated_events if e["status"] == "resolved"),
            "remembered_events": sum(1 for e in all_rows if e["remembered"]),
            "bridge_failures_total": len(bridge_failures),
            "bridge_failures_observed": sum(1 for f in bridge_failures if f["event_data_status"] == "observed"),
            "bridge_failures_simulated": sum(1 for f in bridge_failures if f["event_data_status"] == "simulated"),
        },
        "cypher": [q["query"] for q in log],
        "params": {
            "inputs": {},
            "query_names": [q["name"] for q in log],
            "queries": [q["params"] for q in log],
        },
        "warnings": warnings,
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


def _format_alert_plan_text(
    event_id: str,
    closure_event: dict[str, Any],
    alerts: list[dict[str, Any]],
    disclaimer: str,
) -> str:
    """Format a synthetic draft alert plan as human-readable plain text with mandatory draft labels."""
    is_sim = closure_event.get("data_status") == "simulated"
    hist_label = "SIMULATED (demonstration scenario)" if is_sim else "OBSERVED (historical report)"
    lines = [
        "========================================================================",
        "LANDSLIDE ROUTER — SYNTHETIC COMMUNITY ALERT PLAN (DRAFT ONLY — NOT SENT)",
        "========================================================================",
        f"Safety Disclaimer: {SAFETY_DISCLAIMER}",
        f"Alert Plan Disclaimer: {disclaimer}",
        f"Event ID: {event_id}",
        f"Event Classification: {hist_label} | status={closure_event.get('status')} | data_status={closure_event.get('data_status')}",
        f"Cause / Started At: {closure_event.get('cause') or 'unknown'} | {closure_event.get('started_at') or 'unknown'}",
        f"Blocked Road Segments: {', '.join(closure_event.get('blocks_segments') or []) or 'none'}",
        f"Blocked Bridges: {', '.join(closure_event.get('blocks_bridges') or []) or 'none'}",
        f"Total Draft Alerts: {len(alerts)} (all status='draft', sent=False)",
        "------------------------------------------------------------------------",
    ]
    if not alerts:
        lines.append("No settlement (village/town) is newly cut off from Dimapur by this closure; 0 draft alerts generated.")
    else:
        for idx, a in enumerate(alerts, 1):
            lines.extend([
                f"[DRAFT #{idx}] Target: {a.get('target_name') or a.get('target_place_id')} ({a.get('target_place_id')})",
                f"  Alert ID: {a.get('alert_id')} | Channel: {a.get('channel')} | Status: {a.get('status')} | Sent: {a.get('sent')}",
                f"  Hospital Route Segments: {', '.join(a.get('routed_via_segments') or []) or 'none'}",
                f"  SMS Draft Text ({a.get('length')} chars):",
                f"    {a.get('text')}",
                "------------------------------------------------------------------------",
            ])
    return "\n".join(lines) + "\n"


@app.get(
    "/alerts/{event_id}/export",
    tags=["alerts"],
    summary="Export synthetic community draft alerts for a closure event as JSON or plain text",
)
def export_draft_alerts(
    event_id: str = Path(..., min_length=1, max_length=200, description="ClosureEvent ID"),
    format: Literal["json", "text"] = Query(
        default="json",
        description="Export format: 'json' (structured JSON plan) or 'text' (plain-text SMS draft sheet).",
    ),
    replay: bool = Query(
        default=True,
        description="If true, evaluate stored/resolved/historical events in replay mode when generating draft alerts.",
    ),
    toolbox: Toolbox = Depends(get_toolbox),
) -> Any:
    """Export synthetic community alert drafts (`status='draft'`, `sent=False`) as JSON or plain text."""
    cleaned_event_id = redact_sensitive_text(event_id.strip())
    if not cleaned_event_id:
        raise ToolInputError("'event_id' must be a non-empty string")

    env = toolbox.draft_alerts(closure_event_id=cleaned_event_id, replay=replay)
    res = env["result"]
    alerts = res["alerts"]
    # Enforce draft-only invariant on all exported alerts
    for a in alerts:
        a["status"] = "draft"
        a["sent"] = False

    text_plan = _format_alert_plan_text(
        event_id=cleaned_event_id,
        closure_event=res["closure_event"],
        alerts=alerts,
        disclaimer=res["disclaimer"],
    )

    if format == "text":
        safe_text = redact_sensitive_text(text_plan)
        return PlainTextResponse(
            content=safe_text,
            headers={
                "Content-Disposition": f'attachment; filename="draft-alerts-{cleaned_event_id}.txt"',
            },
        )

    payload = {
        "event_id": cleaned_event_id,
        "export_format": "json",
        "all_drafts": all(a.get("status") == "draft" for a in alerts),
        "any_sent": any(bool(a.get("sent")) for a in alerts),
        **res,
        "text_plan": text_plan,
        **env,
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


@app.get(
    "/replay/{event_id}",
    tags=["history"],
    summary="Replay a stored historical or simulated closure event against the corridor graph without mutating active state",
)
@app.post(
    "/replay/{event_id}",
    tags=["history"],
    summary="Replay a stored historical or simulated closure event against the corridor graph without mutating active state",
)
def replay_closure_endpoint(
    event_id: str = Path(..., min_length=1, max_length=200, description="Stored ClosureEvent ID to replay"),
    from_place: str | None = Query(
        default=None,
        alias="from",
        max_length=200,
        description="Optional origin Place ID for alternate route comparison (defaults to Dimapur reference node).",
    ),
    to_place: str | None = Query(
        default=None,
        alias="to",
        max_length=200,
        description="Optional destination Place ID for alternate route comparison (defaults to Kohima reference node).",
    ),
    include_alerts: bool = Query(
        default=True,
        description="If true, also generate synthetic draft alerts for settlements cut off during the replay.",
    ),
    toolbox: Toolbox = Depends(get_toolbox),
) -> dict[str, Any]:
    """Evaluate reachability, hospital access, alternate route comparison, and draft alerts for any stored event (`event_id`)."""
    cleaned_event_id = redact_sensitive_text(event_id.strip())
    if not cleaned_event_id:
        raise ToolInputError("'event_id' must be a non-empty string")

    cutoff_env = toolbox.find_cut_off_places(closure_event_id=cleaned_event_id, replay=True)
    hosp_env = toolbox.find_unreachable_hospitals(closure_event_id=cleaned_event_id, replay=True)
    cutoff_res = cutoff_env["result"]
    hosp_res = hosp_env["result"]

    snap = toolbox.store.load_snapshot()
    ev_obj = snap.events[cleaned_event_id]
    dimapur = snap.reference("dimapur")
    kohima = snap.reference("kohima")
    origin_id = (
        redact_sensitive_text(from_place.strip())
        if (from_place and from_place.strip())
        else (dimapur.id if dimapur else sorted(snap.places)[0])
    )
    dest_id = (
        redact_sensitive_text(to_place.strip())
        if (to_place and to_place.strip())
        else (kohima.id if kohima else sorted(snap.places)[-1])
    )

    reroute_env = toolbox.reroute(
        from_place_id=origin_id,
        to_place_id=dest_id,
        avoid_event_id=cleaned_event_id,
    )
    explain_env = toolbox.explain_path(
        from_place_id=origin_id,
        to_place_id=dest_id,
        avoid_event_id=cleaned_event_id,
    )
    reroute_res = reroute_env["result"]
    explain_res = explain_env["result"]

    alerts_env = toolbox.draft_alerts(closure_event_id=cleaned_event_id, replay=True) if include_alerts else None
    alerts_res = alerts_env["result"] if alerts_env else None

    is_sim = ev_obj.data_status == "simulated"
    event_detail = {
        **cutoff_res["closure_event"],
        "confidence": ev_obj.confidence,
        "source_url": ev_obj.source_url,
        "history_label": "SIMULATED (demonstration scenario)" if is_sim else "OBSERVED (historical report)",
    }

    cutoff_payload = {
        "event_id": cleaned_event_id,
        **cutoff_res,
        "cut_off_places": cutoff_res,
        "hospital_reachability": hosp_res,
        "explanation": f"{cutoff_env['explanation']} {hosp_env['explanation']}",
        "cypher": list(cutoff_env["cypher"]) + list(hosp_env["cypher"]),
        "params": {
            "find_cut_off_places": cutoff_env["params"],
            "find_unreachable_hospitals": hosp_env["params"],
        },
        "graph": _merge_graphs(cutoff_env["graph"], hosp_env["graph"]),
        "warnings": _dedupe_strings(list(cutoff_env["warnings"]) + list(hosp_env["warnings"])),
        "algorithm": {
            "find_cut_off_places": cutoff_env["algorithm"],
            "find_unreachable_hospitals": hosp_env["algorithm"],
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }

    reroute_payload = {
        **reroute_res,
        "reroute": reroute_res,
        "path_explanation": explain_res,
        "explanation": f"{reroute_env['explanation']} {explain_env['explanation']}",
        "cypher": list(reroute_env["cypher"]) + list(explain_env["cypher"]),
        "params": {
            "reroute": reroute_env["params"],
            "explain_path": explain_env["params"],
        },
        "graph": reroute_env["graph"],
        "warnings": _dedupe_strings(list(reroute_env["warnings"]) + list(explain_env["warnings"])),
        "algorithm": {
            "reroute": reroute_env["algorithm"],
            "explain_path": explain_env["algorithm"],
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }

    alerts_payload = (
        {
            "event_id": cleaned_event_id,
            **alerts_res,
            **alerts_env,
            "disclaimer": SAFETY_DISCLAIMER,
        }
        if (alerts_env and alerts_res)
        else None
    )

    combined_cypher = (
        list(cutoff_payload["cypher"])
        + list(reroute_payload["cypher"])
        + (list(alerts_env["cypher"]) if alerts_env else [])
    )
    combined_warnings = _dedupe_strings(
        list(cutoff_payload["warnings"])
        + list(reroute_payload["warnings"])
        + (list(alerts_env["warnings"]) if alerts_env else [])
    )

    expl = (
        f"Timeline replay of {cleaned_event_id} ({event_detail['history_label']}, status={ev_obj.status}): "
        f"{cutoff_env['explanation']} {hosp_env['explanation']} {reroute_env['explanation']}"
    )

    payload = {
        "replay_mode": True,
        "event_id": cleaned_event_id,
        "event": event_detail,
        "cutoff": cutoff_payload,
        "reroute": reroute_payload,
        "alerts": alerts_payload,
        "explanation": expl,
        "cypher": combined_cypher,
        "params": {
            "find_cut_off_places": cutoff_env["params"],
            "find_unreachable_hospitals": hosp_env["params"],
            "reroute": reroute_env["params"],
            "explain_path": explain_env["params"],
            **({"draft_alerts": alerts_env["params"]} if alerts_env else {}),
        },
        "warnings": combined_warnings,
        "algorithm": {
            "find_cut_off_places": cutoff_env["algorithm"],
            "find_unreachable_hospitals": hosp_env["algorithm"],
            "reroute": reroute_env["algorithm"],
            "explain_path": explain_env["algorithm"],
            **({"draft_alerts": alerts_env["algorithm"]} if alerts_env else {}),
        },
        "disclaimer": SAFETY_DISCLAIMER,
    }
    return sanitize_payload(payload)


# ---------------------------------------------------------------------------
# 12. POST /demo/reset & POST /demo/run (Phase 9 Reproducible Demo & Reset)
# ---------------------------------------------------------------------------
@app.post(
    "/demo/reset",
    tags=["demo"],
    summary="Resolve all active simulated closures and audit return to baseline",
)
def reset_demo_endpoint(store: FalkorStore = Depends(get_store)) -> dict[str, Any]:
    """Resolve all active simulated closures on the graph and verify baseline restoration."""
    from .demo import reset_demo_state

    return sanitize_payload(reset_demo_state(store))


@app.post(
    "/demo/run",
    tags=["demo"],
    summary="Execute the reproducible 12-step demonstration workflow against the active graph",
)
def run_demo_endpoint(
    include_weak_links: bool = Query(
        default=True,
        description="Include full shortest-path betweenness centrality ranking in Step 10.",
    ),
    store: FalkorStore = Depends(get_store),
) -> dict[str, Any]:
    """Run all 12 steps of the reproducible demonstration workflow and return verified results."""
    from .demo import run_twelve_step_demo

    return sanitize_payload(run_twelve_step_demo(store, include_weak_links=include_weak_links))


