"""Official Model Context Protocol (MCP) server exposing Landslide Router graph tools (Phase 4).

Uses `mcp.server.MCPServer` from the official `mcp` Python SDK to expose:
1. `resolve_location`
2. `apply_closure`
3. `clear_closure`
4. `find_cut_off_places`
5. `reroute`
6. `find_unreachable_hospitals`
7. `find_weak_links`
8. `seasonal_risk`
9. `draft_alerts`
10. `explain_path`
11. `remember_closure`
12. `audit_closure`

Every tool validates its inputs, preserves the complete graph-derived tool envelope
(`{tool, result, cypher, params, explanation, graph, warnings, algorithm}`), redacts any
accidental personal or sensitive tokens, and surfaces structured JSON error payloads via
`MCPServerToolError` so MCP clients can distinguish input validation, not-found, and
graph-integrity failures.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPServerToolError

from ..agent.resolver import LocationResolver
from ..graph.errors import ToolError as DomainToolError, ToolInputError
from ..graph.store import FalkorStore
from ..graph.tools import Toolbox
from ..privacy import redact_sensitive_text, sanitize_payload  # noqa: F401  (re-exported: client.py and tests import them from here)

MCP_SERVER_NAME = "landslide-router"
MCP_SERVER_VERSION = "0.4.0"
MCP_SERVER_INSTRUCTIONS = (
    "Landslide Router MCP Server for the Dimapur-Kohima NH29 corridor in Nagaland, India. "
    "Planning aid, not emergency dispatch. Always call `resolve_location` first for natural-language "
    "incident reports, and never call `apply_closure` when `requires_clarification` is true."
)

EXPECTED_MCP_TOOLS: tuple[str, ...] = (
    "resolve_location",
    "apply_closure",
    "clear_closure",
    "find_cut_off_places",
    "reroute",
    "find_unreachable_hospitals",
    "find_weak_links",
    "seasonal_risk",
    "draft_alerts",
    "explain_path",
    "remember_closure",
    "audit_closure",
)



def _require_non_empty_str(value: Any, field_name: str, max_len: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{field_name!r} must be a non-empty string")
    cleaned = value.strip()
    if len(cleaned) > max_len:
        raise ToolInputError(f"{field_name!r} exceeds maximum length of {max_len} characters")
    return redact_sensitive_text(cleaned)


def _validate_optional_str(value: Any, field_name: str, max_len: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolInputError(f"{field_name!r} must be a string or null")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        raise ToolInputError(f"{field_name!r} exceeds maximum length of {max_len} characters")
    return redact_sensitive_text(cleaned)


def _validate_coordinates(lat: float | None, lon: float | None) -> tuple[float | None, float | None]:
    if (lat is None) != (lon is None):
        raise ToolInputError("Both 'lat' and 'lon' must be provided together, or both omitted")
    if lat is not None and lon is not None:
        if isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise ToolInputError("'lat' and 'lon' must be numeric coordinates")
        lat_f, lon_f = float(lat), float(lon)
        if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
            raise ToolInputError("'lat' and 'lon' must be finite numbers")
        if not (-90.0 <= lat_f <= 90.0):
            raise ToolInputError(f"'lat'={lat_f} is outside valid latitude range [-90, 90]")
        if not (-180.0 <= lon_f <= 180.0):
            raise ToolInputError(f"'lon'={lon_f} is outside valid longitude range [-180, 180]")
        return lat_f, lon_f
    return None, None


def _validate_positive_int(value: Any, field_name: str, max_val: int = 100) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"{field_name!r} must be an integer")
    if value < 1 or value > max_val:
        raise ToolInputError(f"{field_name!r} must be between 1 and {max_val}")
    return value


def _format_mcp_tool_error(tool_name: str, exc: Exception) -> MCPServerToolError:
    if isinstance(exc, DomainToolError):
        payload = {
            "code": exc.code,
            "type": type(exc).__name__,
            "tool": tool_name,
            "message": redact_sensitive_text(str(exc)),
        }
    else:
        payload = {
            "code": "invalid_input",
            "type": type(exc).__name__,
            "tool": tool_name,
            "message": redact_sensitive_text(str(exc)),
        }
    return MCPServerToolError(json.dumps(payload, ensure_ascii=False))


def build_mcp_server(
    store: FalkorStore,
    *,
    clock: Callable[[], str] | None = None,
    id_factory: Callable[[], str] | None = None,
    server_name: str = MCP_SERVER_NAME,
    simulated_delay_s: float = 0.0,
) -> MCPServer:
    """Create an official `MCPServer` instance wired to `store` with all 12 Landslide Router tools."""
    mcp_srv = MCPServer(
        server_name,
        version=MCP_SERVER_VERSION,
        instructions=MCP_SERVER_INSTRUCTIONS,
    )
    toolbox = Toolbox(store, clock=clock, id_factory=id_factory)
    resolver = LocationResolver(store)
    invocation_log: list[dict[str, Any]] = []
    # Attach state on server instance for introspection in tests and diagnostics
    setattr(mcp_srv, "invocation_log", invocation_log)
    setattr(mcp_srv, "store", store)
    setattr(mcp_srv, "toolbox", toolbox)
    setattr(mcp_srv, "resolver", resolver)
    setattr(mcp_srv, "simulated_delay_s", simulated_delay_s)

    def _execute(tool_name: str, raw_args: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> str:
        t0 = time.perf_counter()
        delay = float(getattr(mcp_srv, "simulated_delay_s", 0.0) or 0.0)
        if delay > 0:
            time.sleep(delay)
        sanitized_args = sanitize_payload(raw_args)
        try:
            env = fn()
            sanitized_env = sanitize_payload(env)
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            invocation_log.append({
                "sequence": len(invocation_log) + 1,
                "tool": tool_name,
                "arguments": sanitized_args,
                "status": "ok",
                "duration_ms": elapsed_ms,
                "cypher_count": len(sanitized_env.get("cypher") or []),
                "warnings_count": len(sanitized_env.get("warnings") or []),
            })
            return json.dumps(sanitized_env, ensure_ascii=False, default=str)
        except (DomainToolError, ValueError, TypeError) as exc:
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            code = exc.code if isinstance(exc, DomainToolError) else "invalid_input"
            invocation_log.append({
                "sequence": len(invocation_log) + 1,
                "tool": tool_name,
                "arguments": sanitized_args,
                "status": "error",
                "error_code": code,
                "error_message": redact_sensitive_text(str(exc)),
                "duration_ms": elapsed_ms,
            })
            raise _format_mcp_tool_error(tool_name, exc) from exc

    @mcp_srv.tool(
        name="resolve_location",
        description=(
            "Resolve a natural-language event description or coordinates against the corridor graph. "
            "Returns candidate road segments and bridges, match reasons, provenance, and whether clarification is required."
        ),
        structured_output=False,
    )
    def resolve_location(
        description: str,
        lat: float | None = None,
        lon: float | None = None,
        target_segment_id: str | None = None,
        target_bridge_id: str | None = None,
        max_candidates: int = 5,
    ) -> str:
        raw = {
            "description": description,
            "lat": lat,
            "lon": lon,
            "target_segment_id": target_segment_id,
            "target_bridge_id": target_bridge_id,
            "max_candidates": max_candidates,
        }

        def _call() -> dict[str, Any]:
            desc = _require_non_empty_str(description, "description")
            lat_f, lon_f = _validate_coordinates(lat, lon)
            seg_id = _validate_optional_str(target_segment_id, "target_segment_id")
            br_id = _validate_optional_str(target_bridge_id, "target_bridge_id")
            max_c = _validate_positive_int(max_candidates, "max_candidates", max_val=50)
            return resolver.resolve_location(
                description=desc,
                lat=lat_f,
                lon=lon_f,
                target_segment_id=seg_id,
                target_bridge_id=br_id,
                max_candidates=max_c,
            )

        return _execute("resolve_location", raw, _call)

    @mcp_srv.tool(
        name="apply_closure",
        description="Create an active simulated ClosureEvent blocking a verified or confirmed road segment (and optional bridge).",
        structured_output=False,
    )
    def apply_closure(
        segment_id: str,
        description: str,
        cause: str = "landslide",
        bridge_id: str | None = None,
    ) -> str:
        raw = {"segment_id": segment_id, "description": description, "cause": cause, "bridge_id": bridge_id}

        def _call() -> dict[str, Any]:
            seg = _require_non_empty_str(segment_id, "segment_id", max_len=128)
            desc = _require_non_empty_str(description, "description")
            cause_s = _require_non_empty_str(cause, "cause", max_len=64)
            br = _validate_optional_str(bridge_id, "bridge_id", max_len=128)
            return toolbox.apply_closure(segment_id=seg, description=desc, cause=cause_s, bridge_id=br)

        return _execute("apply_closure", raw, _call)

    @mcp_srv.tool(
        name="clear_closure",
        description="Resolve an active simulated closure event while preserving its historical record.",
        structured_output=False,
    )
    def clear_closure(event_id: str) -> str:
        raw = {"event_id": event_id}

        def _call() -> dict[str, Any]:
            eid = _require_non_empty_str(event_id, "event_id", max_len=128)
            return toolbox.clear_closure(event_id=eid)

        return _execute("clear_closure", raw, _call)

    @mcp_srv.tool(
        name="find_cut_off_places",
        description="Compare baseline vs closed directed reachability from Dimapur and identify newly unreachable settlements and junctions.",
        structured_output=False,
    )
    def find_cut_off_places(closure_event_id: str) -> str:
        raw = {"closure_event_id": closure_event_id}

        def _call() -> dict[str, Any]:
            eid = _require_non_empty_str(closure_event_id, "closure_event_id", max_len=128)
            return toolbox.find_cut_off_places(closure_event_id=eid)

        return _execute("find_cut_off_places", raw, _call)

    @mcp_srv.tool(
        name="reroute",
        description="Find the minimum-travel-time directed path between two places excluding all currently blocked segments.",
        structured_output=False,
    )
    def reroute(
        from_place_id: str,
        to_place_id: str,
        avoid_event_id: str | None = None,
    ) -> str:
        raw = {"from_place_id": from_place_id, "to_place_id": to_place_id, "avoid_event_id": avoid_event_id}

        def _call() -> dict[str, Any]:
            orig = _require_non_empty_str(from_place_id, "from_place_id", max_len=128)
            dest = _require_non_empty_str(to_place_id, "to_place_id", max_len=128)
            avoid = _validate_optional_str(avoid_event_id, "avoid_event_id", max_len=128)
            return toolbox.reroute(from_place_id=orig, to_place_id=dest, avoid_event_id=avoid)

        return _execute("reroute", raw, _call)

    @mcp_srv.tool(
        name="find_unreachable_hospitals",
        description="Evaluate hospital reachability from settlements newly cut off by a closure event.",
        structured_output=False,
    )
    def find_unreachable_hospitals(
        closure_event_id: str,
        threshold_min: float | None = None,
    ) -> str:
        raw = {"closure_event_id": closure_event_id, "threshold_min": threshold_min}

        def _call() -> dict[str, Any]:
            eid = _require_non_empty_str(closure_event_id, "closure_event_id", max_len=128)
            if threshold_min is not None:
                if isinstance(threshold_min, bool) or not isinstance(threshold_min, (int, float)) or not math.isfinite(float(threshold_min)):
                    raise ToolInputError("'threshold_min' must be a finite positive number")
            return toolbox.find_unreachable_hospitals(closure_event_id=eid, threshold_min=threshold_min)

        return _execute("find_unreachable_hospitals", raw, _call)

    @mcp_srv.tool(
        name="find_weak_links",
        description="Rank corridor road segments, bridges, and places by travel-time-weighted betweenness centrality.",
        structured_output=False,
    )
    def find_weak_links(corridor_id: str, top_k: int = 5) -> str:
        raw = {"corridor_id": corridor_id, "top_k": top_k}

        def _call() -> dict[str, Any]:
            cid = _require_non_empty_str(corridor_id, "corridor_id", max_len=128)
            k = _validate_positive_int(top_k, "top_k", max_val=100)
            return toolbox.find_weak_links(corridor_id=cid, top_k=k)

        return _execute("find_weak_links", raw, _call)

    @mcp_srv.tool(
        name="seasonal_risk",
        description="Rank bridges and road segments for a given month using monsoon-risk labels and documented event history.",
        structured_output=False,
    )
    def seasonal_risk(month: int | str) -> str:
        raw = {"month": month}

        def _call() -> dict[str, Any]:
            if isinstance(month, bool) or not isinstance(month, (int, str)):
                raise ToolInputError("'month' must be an integer (1-12) or month name")
            return toolbox.seasonal_risk(month=month)

        return _execute("seasonal_risk", raw, _call)

    @mcp_srv.tool(
        name="draft_alerts",
        description="Create synthetic SMS-ready draft alerts for settlements newly cut off by a closure event.",
        structured_output=False,
    )
    def draft_alerts(closure_event_id: str) -> str:
        raw = {"closure_event_id": closure_event_id}

        def _call() -> dict[str, Any]:
            eid = _require_non_empty_str(closure_event_id, "closure_event_id", max_len=128)
            return toolbox.draft_alerts(closure_event_id=eid)

        return _execute("draft_alerts", raw, _call)

    @mcp_srv.tool(
        name="explain_path",
        description="Explain the selected minimum-travel-time path, per-segment travel-time contributions, exclusions, and provenance.",
        structured_output=False,
    )
    def explain_path(
        from_place_id: str,
        to_place_id: str,
        avoid_event_id: str | None = None,
    ) -> str:
        raw = {"from_place_id": from_place_id, "to_place_id": to_place_id, "avoid_event_id": avoid_event_id}

        def _call() -> dict[str, Any]:
            orig = _require_non_empty_str(from_place_id, "from_place_id", max_len=128)
            dest = _require_non_empty_str(to_place_id, "to_place_id", max_len=128)
            avoid = _validate_optional_str(avoid_event_id, "avoid_event_id", max_len=128)
            return toolbox.explain_path(from_place_id=orig, to_place_id=dest, avoid_event_id=avoid)

        return _execute("explain_path", raw, _call)

    @mcp_srv.tool(
        name="remember_closure",
        description="Persist a closure event in historical memory and link bridge failure records where supported.",
        structured_output=False,
    )
    def remember_closure(event_id: str) -> str:
        raw = {"event_id": event_id}

        def _call() -> dict[str, Any]:
            eid = _require_non_empty_str(event_id, "event_id", max_len=128)
            return toolbox.remember_closure(event_id=eid)

        return _execute("remember_closure", raw, _call)

    @mcp_srv.tool(
        name="audit_closure",
        description="Verify whether a closure has been resolved and whether the network has returned to its baseline state.",
        structured_output=False,
    )
    def audit_closure(event_id: str) -> str:
        raw = {"event_id": event_id}

        def _call() -> dict[str, Any]:
            eid = _require_non_empty_str(event_id, "event_id", max_len=128)
            return toolbox.audit_closure(event_id=eid)

        return _execute("audit_closure", raw, _call)

    return mcp_srv

