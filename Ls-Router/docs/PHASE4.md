# Phase 4 — Model Context Protocol (MCP) Server

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

## 1. Overview & Architecture

Phase 4 implements the official Model Context Protocol (MCP) integration for **Landslide Router**, exposing all corridor routing, connectivity, risk, and alert tools as standard MCP tools under the official `mcp` Python SDK.

```
+-------------------------------------------------------------------------------+
|                      LandslideAgent / External MCP Host                       |
|          (Claude Desktop, Cursor, MCP Inspector, or Python CLI)               |
+-------------------------------------------------------------------------------+
                                      |
                         MCP Protocol JSON-RPC 2.0
                        tools/list  |  tools/call
                                      v
+-------------------------------------------------------------------------------+
|                    MCPToolClient (mcp_bridge/client.py)                       |
|   - Transports: stdio subprocess (mcp/server.py) or in-memory streams         |
|   - Real MCP handshake (`initialize`), discovery (`list_tools`)               |
|   - Handoff Logging: agent reasoning <-> graph operations                     |
|   - Timeout handling (`read_timeout_seconds`) and domain error mapping        |
+-------------------------------------------------------------------------------+
                                      |
                                      v
+-------------------------------------------------------------------------------+
|                      MCPServer (mcp_bridge/server.py)                         |
|   - 12 registered MCP tools with rich Pydantic input schemas                  |
|   - Strict coordinate, ID, and range validation                               |
|   - Automatic personal/sensitive data redaction                               |
|   - Structured tool execution envelopes & JSON error payloads                 |
+-------------------------------------------------------------------------------+
                                      |
                                      v
+-------------------------------------------------------------------------------+
|                    FalkorStore (FalkorDB Graph Backend)                       |
|   - RoadSegments, Places, Hospitals, Bridges, Closures, Alerts, Seasons       |
+-------------------------------------------------------------------------------+
```

---

## 2. Exposed MCP Tools

The server exposes 12 tools:

| # | Tool Name | Description | Key Arguments |
|---|---|---|---|
| 1 | `resolve_location` | Resolve natural-language event or coordinates against corridor graph | `description`, `lat`, `lon`, `target_segment_id`, `target_bridge_id`, `max_candidates` |
| 2 | `apply_closure` | Create simulated `ClosureEvent` blocking a road segment and optional bridge | `segment_id`, `description`, `cause`, `bridge_id` |
| 3 | `clear_closure` | Resolve active closure while preserving historical record | `event_id` |
| 4 | `find_cut_off_places` | Compare baseline vs closed reachability from Dimapur | `closure_event_id` |
| 5 | `reroute` | Find minimum-travel-time path excluding active closures | `from_place_id`, `to_place_id`, `avoid_event_id` |
| 6 | `find_unreachable_hospitals` | Evaluate hospital access from cut-off settlements | `closure_event_id`, `threshold_min` |
| 7 | `find_weak_links` | Rank corridor weak points by betweenness centrality | `corridor_id`, `top_k` |
| 8 | `seasonal_risk` | Rank monsoon-exposed road segments and bridges for a month | `month` |
| 9 | `draft_alerts` | Create synthetic SMS-ready draft alerts for affected communities | `closure_event_id` |
| 10 | `explain_path` | Explain segment-by-segment travel time, contributions, and exclusions | `from_place_id`, `to_place_id`, `avoid_event_id` |
| 11 | `remember_closure` | Persist closure in historical memory and link bridge failure records | `event_id` |
| 12 | `audit_closure` | Audit baseline network state restoration after closure resolution | `event_id` |

---

## 3. Strict Boundary & Non-Simulation Invariant

- **Rule 12 Compliance**: The agent never simulates MCP tool execution. Tool calls go through `ClientSession.call_tool(...)` exchanging real JSON-RPC 2.0 messages over standard MCP streams.
- **Ordinary Python Calls Distinct**: If `use_mcp=False` is passed, the tool execution transport is recorded as `direct_python` with `report["mcp"]["enabled"] = False`. Ordinary Python calls are never described as MCP.

---

## 4. MCP Handoff Logging Schema

Every tool call logs a structured handoff between agent reasoning and graph operations:

```json
{
  "handoff_id": "mcp-handoff-2",
  "phase": "tool_invocation",
  "transport": "mcp-stdio",
  "server_name": "landslide-router",
  "tool": "apply_closure",
  "selection_reason": "Apply simulated closure on resolved segment s2.",
  "arguments": {
    "segment_id": "s2",
    "description": "Landslide near NH29 kilometre 42."
  },
  "direction": "agent -> mcp_server -> falkordb_graph -> mcp_server -> agent",
  "status": "ok",
  "duration_ms": 4.44,
  "cypher_executed_count": 2,
  "decision_impact": "Created active closure 'sim-e1' on ['s2']; agent proceeds to evaluate cut-off settlements, hospitals, and routes."
}
```

---

## 5. Personal Information Redaction & Security

Free-text descriptions, query arguments, and tool results are sanitized to prevent accidental exposure of sensitive information:
- Emails: `[REDACTED_EMAIL]`
- Phone numbers: `[REDACTED_PHONE]`
- Secret tokens / API keys (`sk-...`, `Bearer ...`): `[REDACTED_SECRET]`
- Local user filesystem paths (`C:\Users\...`): `[REDACTED_USER_PATH]`
- Public OpenStreetMap provenance URLs are preserved while userinfo credentials are stripped (`https://[REDACTED]@...`).
- Stdio child subprocesses receive a strictly whitelisted environment, preventing leakage of `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

