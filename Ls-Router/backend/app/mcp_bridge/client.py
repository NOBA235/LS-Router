"""Official Model Context Protocol (MCP) client wrapper and handoff logger (Phase 4).

Connects `LandslideAgent` (or CLI / test callers) to the Landslide Router `MCPServer` using
`mcp.client.session.ClientSession` over either:
- `transport="memory"`: in-memory JSON-RPC 2.0 `SessionMessage` streams via
  `mcp.shared.memory.create_client_server_memory_streams`, sharing the caller's `FalkorStore`.
- `transport="stdio"`: OS subprocess `stdin`/`stdout` JSON-RPC 2.0 streams via
  `mcp.client.stdio.stdio_client`, launching `mcp/server.py` as a child process.

Every tool discovery (`tools/list`) and tool invocation (`tools/call`) is a genuine MCP protocol
exchange, never an ordinary Python function call disguised as MCP.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult

from ..graph.errors import (
    StoreError,
    ToolError as DomainToolError,
    ToolInputError,
    ToolStateError,
)
from ..graph.store import FalkorStore
from .server import MCP_SERVER_NAME, MCP_SERVER_VERSION, build_mcp_server, sanitize_payload


class MCPTimeoutError(DomainToolError):
    """Raised when an MCP tool call exceeds the configured read timeout."""

    code = "mcp_timeout"


class MCPExecutionError(DomainToolError):
    """Raised when an MCP tool call fails with an unexpected protocol or execution error."""

    code = "mcp_execution_error"


_STRUCTURED_ERR_RE = re.compile(r"\{.*\}", re.DOTALL)


def _summarize_decision_impact(tool_name: str, env: dict[str, Any]) -> str:
    """Summarize how the tool's output shapes the agent's next decision."""
    res = env.get("result") or {}
    if tool_name == "resolve_location":
        status = res.get("status")
        if status == "ambiguous":
            cands = [c.get("segment_id") for c in res.get("candidate_segments") or []]
            return f"Location is ambiguous across candidates {cands}; agent halts closure and requests clarification."
        if status == "no_match":
            return "No matching corridor segment or bridge found; agent halts without modifying the graph."
        target = res.get("selected_target") or {}
        return f"Resolved target segment {target.get('segment_id')!r}; agent proceeds to simulate closure."
    if tool_name == "apply_closure":
        ev = res.get("event") or {}
        return f"Created active closure {ev.get('id')!r} on {ev.get('blocks_segments')}; agent proceeds to evaluate cut-off settlements, hospitals, and routes."
    if tool_name == "find_cut_off_places":
        cnt = ((res.get("newly_unreachable") or {}).get("count")) or 0
        return f"Identified {cnt} newly cut-off place(s) from Dimapur; shapes hospital reachability and alert drafting."
    if tool_name == "find_unreachable_hospitals":
        no_route = res.get("hospitals_with_no_usable_route_from_any_affected_settlement") or []
        return f"Evaluated hospital access for affected settlements (unreachable from all affected={no_route})."
    if tool_name == "reroute":
        status = res.get("status")
        if status == "route_found" and res.get("route"):
            mins = res["route"].get("total_travel_time_min")
            delta = res.get("additional_travel_time_min")
            return f"Found valid alternate route ({mins} min, delta={delta} min); agent proceeds to explain path."
        return f"No alternate route available ({res.get('reason')}); agent records no-route status."
    if tool_name == "explain_path":
        return f"Extracted per-segment travel-time contributions and closure exclusions ({res.get('status')})."
    if tool_name == "find_weak_links":
        segs = [s.get("segment_id") for s in (res.get("segments") or [])[:3]]
        return f"Ranked top corridor weak links by betweenness centrality: {segs}."
    if tool_name == "seasonal_risk":
        return f"Ranked {len(res.get('ranked') or [])} monsoon-exposed feature(s) for month {res.get('month')}."
    if tool_name == "draft_alerts":
        alerts = res.get("alerts") or []
        return f"Generated {len(alerts)} synthetic SMS draft alert(s) for newly cut-off settlements."
    if tool_name == "clear_closure":
        ev = res.get("event") or {}
        return f"Resolved closure {ev.get('id')!r} (restored={res.get('restored_segments')}); agent proceeds to audit baseline state."
    if tool_name == "audit_closure":
        return f"Verified baseline restoration: resolved={res.get('resolved')}, returned_to_baseline={res.get('returned_to_baseline')}."
    if tool_name == "remember_closure":
        return f"Persisted closure in historical memory (failure_records_created={res.get('failure_records_created')})."
    return env.get("explanation") or f"Processed {tool_name} output."


def _parse_and_raise_mcp_error(tool_name: str, error_text: str) -> None:
    """Parse structured or validation errors returned in `CallToolResult(is_error=True)` and raise the appropriate DomainToolError."""
    match = _STRUCTURED_ERR_RE.search(error_text or "")
    if match:
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict) and "code" in payload and "message" in payload:
                code = payload.get("code")
                msg = payload.get("message", error_text)
                if code == "invalid_input":
                    raise ToolInputError(msg)
                if code == "invalid_state":
                    raise ToolStateError(msg)
                if code == "store_error":
                    raise StoreError(msg)
                raise MCPExecutionError(msg)
        except json.JSONDecodeError:
            pass

    lower = (error_text or "").lower()
    if "validation error" in lower or "unknown tool" in lower or "invalid_input" in lower:
        raise ToolInputError(error_text)
    if "invalid_state" in lower:
        raise ToolStateError(error_text)
    if "store_error" in lower:
        raise StoreError(error_text)
    raise MCPExecutionError(error_text or f"MCP tool {tool_name!r} failed")


def _safe_stdio_env() -> dict[str, str]:
    """Build a minimal environment for stdio subprocesses without leaking LLM API keys or secrets."""
    allowed_keys = (
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "PATHEXT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "PYTHONIOENCODING",
        "PYTHONPATH",
        "FALKORDB_HOST",
        "FALKORDB_PORT",
        "FALKORDB_GRAPH",
    )
    env: dict[str, str] = {"PYTHONIOENCODING": "utf-8"}
    for key in allowed_keys:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


class _StdioSessionWorker:
    """Maintains a persistent stdio MCP `ClientSession` in a background thread so multi-step agent runs share server state."""

    def __init__(self, server_params: StdioServerParameters):
        self.server_params = server_params
        self._req_q: queue.Queue[tuple[str, Any, queue.Queue] | None] = queue.Queue()
        self._ready_q: queue.Queue[tuple[bool, Any]] = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._run_thread, name="mcp-stdio-session", daemon=True)
        self._thread.start()
        ok, payload = self._ready_q.get(timeout=15.0)
        if not ok:
            raise MCPExecutionError(f"Failed to initialize stdio MCP server: {payload}")
        self.server_info: dict[str, Any] = payload

    def _run_thread(self) -> None:
        async def _async_loop() -> None:
            try:
                async with stdio_client(self.server_params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        init = await session.initialize()
                        info = {
                            "name": getattr(init.server_info, "name", MCP_SERVER_NAME),
                            "version": getattr(init.server_info, "version", MCP_SERVER_VERSION),
                        }
                        self._ready_q.put((True, info))
                        while True:
                            item = await anyio.to_thread.run_sync(self._req_q.get)
                            if item is None:
                                break
                            op, args, reply_q = item
                            try:
                                if op == "list_tools":
                                    res = await session.list_tools()
                                    reply_q.put(("ok", res))
                                elif op == "call_tool":
                                    name, tool_args, timeout_s = args
                                    with anyio.fail_after(timeout_s):
                                        res = await session.call_tool(
                                            name,
                                            tool_args,
                                            read_timeout_seconds=timeout_s,
                                        )
                                    reply_q.put(("ok", res))
                                else:
                                    reply_q.put(("err", ValueError(f"Unknown stdio op {op!r}")))
                            except TimeoutError as exc:
                                reply_q.put(("timeout", exc))
                            except BaseException as exc:  # noqa: BLE001
                                reply_q.put(("err", exc))
            except BaseException as exc:  # noqa: BLE001
                self._ready_q.put((False, str(exc)))

        anyio.run(_async_loop)

    def request(self, op: str, args: Any = None, timeout_s: float = 15.0) -> Any:
        if self._closed:
            raise MCPExecutionError("Stdio MCP session is already closed")
        reply_q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._req_q.put((op, args, reply_q))
        try:
            status, payload = reply_q.get(timeout=timeout_s + 5.0)
        except queue.Empty as exc:
            raise MCPTimeoutError(f"Stdio MCP request {op!r} timed out after {timeout_s}s") from exc
        if status == "timeout":
            raise MCPTimeoutError(f"MCP tool call timed out after {timeout_s}s")
        if status == "err":
            if isinstance(payload, TimeoutError):
                raise MCPTimeoutError(f"MCP tool call timed out after {timeout_s}s") from payload
            raise payload
        return payload

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._req_q.put(None)
            self._thread.join(timeout=5.0)


class MCPToolClient:
    """Official MCP client that discovers tools, validates calls, logs handoffs, and executes tools over MCP."""

    def __init__(
        self,
        store: FalkorStore | None = None,
        *,
        mcp_server=None,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
        transport: str = "memory",
        timeout_s: float = 15.0,
        stdio_args: list[str] | None = None,
    ):
        if transport not in ("memory", "stdio"):
            raise ValueError(f"Unsupported MCP transport {transport!r}; expected 'memory' or 'stdio'")
        self.transport = transport
        self.transport_label = f"mcp-{transport}"
        self.timeout_s = float(timeout_s)
        self.store = store
        self.handoffs: list[dict[str, Any]] = []
        self.discovered_tools: list[dict[str, Any]] = []
        self._discovered_by_name: dict[str, dict[str, Any]] = {}
        self.server_info: dict[str, Any] = {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION}
        self._stdio_worker: _StdioSessionWorker | None = None

        if transport == "memory":
            if mcp_server is not None:
                self.mcp_server = mcp_server
            else:
                if store is None:
                    raise ValueError("MCPToolClient(transport='memory') requires either `store` or `mcp_server`")
                self.mcp_server = build_mcp_server(store, clock=clock, id_factory=id_factory)
        else:
            self.mcp_server = None
            script_path = Path(__file__).resolve().parents[3] / "mcp" / "server.py"
            cmd_args = [str(script_path), *(stdio_args or [])]
            params = StdioServerParameters(
                command=sys.executable,
                args=cmd_args,
                env=_safe_stdio_env(),
            )
            self._stdio_worker = _StdioSessionWorker(params)
            self.server_info = dict(self._stdio_worker.server_info)

    def __enter__(self) -> MCPToolClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        if self._stdio_worker is not None:
            self._stdio_worker.close()
            self._stdio_worker = None

    def reset_handoffs(self) -> None:
        self.handoffs.clear()

    # ------------------------------------------------------------------ MCP discovery
    def list_tools(self) -> list[dict[str, Any]]:
        """Discover available MCP tools via a real `ClientSession.list_tools()` JSON-RPC call."""
        t0 = time.perf_counter()
        if self.transport == "memory":
            assert self.mcp_server is not None
            srv = self.mcp_server

            async def _discover_mem():
                holder: list[Any] = []
                async with create_client_server_memory_streams() as (client_streams, server_streams):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(
                            srv._lowlevel_server.run,
                            server_streams[0],
                            server_streams[1],
                            srv._lowlevel_server.create_initialization_options(),
                        )
                        async with ClientSession(client_streams[0], client_streams[1]) as session:
                            init = await session.initialize()
                            listed = await session.list_tools()
                            holder.append((init, listed))
                            tg.cancel_scope.cancel()
                return holder[0]

            init_res, list_res = anyio.run(_discover_mem)
            self.server_info = {
                "name": getattr(init_res.server_info, "name", MCP_SERVER_NAME),
                "version": getattr(init_res.server_info, "version", MCP_SERVER_VERSION),
            }
        else:
            assert self._stdio_worker is not None
            list_res = self._stdio_worker.request("list_tools", timeout_s=self.timeout_s)

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        tools_out: list[dict[str, Any]] = []
        for t in list_res.tools:
            entry = {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema or {"type": "object", "properties": {}},
            }
            tools_out.append(entry)

        self.discovered_tools = tools_out
        self._discovered_by_name = {t["name"]: t for t in tools_out}
        self.handoffs.append({
            "handoff_id": f"mcp-handoff-{len(self.handoffs) + 1}",
            "phase": "discovery",
            "transport": self.transport_label,
            "server_name": self.server_info["name"],
            "server_version": self.server_info["version"],
            "direction": "agent -> mcp_server (tools/list) -> agent",
            "status": "ok",
            "duration_ms": elapsed_ms,
            "tool_count": len(tools_out),
            "discovered_tools": [t["name"] for t in tools_out],
        })
        return tools_out

    # ------------------------------------------------------------------ MCP tool invocation
    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        reason: str = "Agent selected tool for graph operation.",
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Invoke an MCP tool over `ClientSession.call_tool` and return its decoded tool envelope."""
        if not self._discovered_by_name:
            self.list_tools()

        clean_args = sanitize_payload({k: v for k, v in (arguments or {}).items() if v is not None})
        effective_timeout = float(timeout_s if timeout_s is not None else self.timeout_s)

        if not isinstance(name, str) or not name.strip():
            raise ToolInputError("Tool name must be a non-empty string")

        if name not in self._discovered_by_name:
            self.handoffs.append({
                "handoff_id": f"mcp-handoff-{len(self.handoffs) + 1}",
                "phase": "tool_invocation",
                "transport": self.transport_label,
                "server_name": self.server_info["name"],
                "tool": name,
                "selection_reason": reason,
                "arguments": clean_args,
                "direction": "agent -> mcp_server (rejected: unknown tool)",
                "status": "error",
                "error_code": "invalid_input",
                "error_message": f"Unknown MCP tool {name!r}",
                "duration_ms": 0.0,
            })
            raise ToolInputError(f"Unknown MCP tool {name!r}")

        t0 = time.perf_counter()
        try:
            if self.transport == "memory":
                assert self.mcp_server is not None
                srv = self.mcp_server

                async def _call_mem() -> CallToolResult:
                    holder: list[CallToolResult] = []
                    timed_out: list[bool] = [False]
                    async with create_client_server_memory_streams() as (client_streams, server_streams):
                        async with anyio.create_task_group() as tg:
                            tg.start_soon(
                                srv._lowlevel_server.run,
                                server_streams[0],
                                server_streams[1],
                                srv._lowlevel_server.create_initialization_options(),
                            )
                            async with ClientSession(client_streams[0], client_streams[1]) as session:
                                await session.initialize()
                                try:
                                    with anyio.fail_after(effective_timeout):
                                        res = await session.call_tool(
                                            name,
                                            clean_args,
                                            read_timeout_seconds=effective_timeout,
                                        )
                                    holder.append(res)  # type: ignore[arg-type]
                                    tg.cancel_scope.cancel()
                                except TimeoutError:
                                    timed_out[0] = True
                                    tg.cancel_scope.cancel()
                    if timed_out[0]:
                        raise MCPTimeoutError(f"MCP tool {name!r} timed out after {effective_timeout}s")
                    return holder[0]

                call_res = anyio.run(_call_mem)
            else:
                assert self._stdio_worker is not None
                call_res = self._stdio_worker.request(
                    "call_tool",
                    (name, clean_args, effective_timeout),
                    timeout_s=effective_timeout,
                )
        except (TimeoutError, MCPTimeoutError, BaseException) as exc:
            # Unwrap BaseExceptionGroup if raised by anyio.run
            is_timeout = isinstance(exc, (TimeoutError, MCPTimeoutError))
            if not is_timeout and hasattr(exc, "exceptions"):
                is_timeout = any(isinstance(e, (TimeoutError, MCPTimeoutError)) for e in getattr(exc, "exceptions", []))
            if not is_timeout:
                raise

            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            self.handoffs.append({
                "handoff_id": f"mcp-handoff-{len(self.handoffs) + 1}",
                "phase": "tool_invocation",
                "transport": self.transport_label,
                "server_name": self.server_info["name"],
                "tool": name,
                "selection_reason": reason,
                "arguments": clean_args,
                "direction": "agent -> mcp_server -> timeout",
                "status": "timeout",
                "error_code": "mcp_timeout",
                "error_message": f"MCP tool {name!r} timed out after {effective_timeout}s",
                "duration_ms": elapsed_ms,
            })
            raise MCPTimeoutError(f"MCP tool {name!r} timed out after {effective_timeout}s") from exc

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        text_blocks = [getattr(c, "text", "") for c in (call_res.content or []) if getattr(c, "type", "") == "text"]
        raw_text = "\n".join(text_blocks).strip()

        if getattr(call_res, "is_error", False):
            try:
                _parse_and_raise_mcp_error(name, raw_text)
            except DomainToolError as dom_exc:
                self.handoffs.append({
                    "handoff_id": f"mcp-handoff-{len(self.handoffs) + 1}",
                    "phase": "tool_invocation",
                    "transport": self.transport_label,
                    "server_name": self.server_info["name"],
                    "tool": name,
                    "selection_reason": reason,
                    "arguments": clean_args,
                    "direction": "agent -> mcp_server -> error -> agent",
                    "status": "error",
                    "error_code": dom_exc.code,
                    "error_message": str(dom_exc),
                    "duration_ms": elapsed_ms,
                })
                raise

        try:
            env = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise MCPExecutionError(f"MCP tool {name!r} returned non-JSON payload: {raw_text[:200]!r}") from exc

        handoff_id = f"mcp-handoff-{len(self.handoffs) + 1}"
        impact = _summarize_decision_impact(name, env)
        handoff_record = {
            "handoff_id": handoff_id,
            "phase": "tool_invocation",
            "transport": self.transport_label,
            "server_name": self.server_info["name"],
            "tool": name,
            "selection_reason": reason,
            "arguments": clean_args,
            "direction": "agent -> mcp_server -> falkordb_graph -> mcp_server -> agent",
            "status": "ok",
            "duration_ms": elapsed_ms,
            "cypher_executed_count": len(env.get("cypher") or []),
            "decision_impact": impact,
        }
        self.handoffs.append(handoff_record)
        env["mcp_handoff"] = handoff_record
        return env
