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
from typing import Any

from fastapi import Body, Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

        for attr in ("target_segment_id", "target_bridge_id", "cause"):
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
