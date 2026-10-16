# Model Context Protocol (MCP) Server — Landslide Router

> **Planning aid, not emergency dispatch.** Routes and risk estimates are based on available data and simulations. Verify conditions with official authorities before acting.

## Overview

The Landslide Router MCP server exposes 12 graph-powered tools for disaster routing along the Dimapur–Kohima NH29 corridor in Nagaland, India. Built with the official `mcp` Python SDK (`mcp.server.MCPServer`), it allows autonomous AI agents and external MCP hosts (such as Claude Desktop, Cursor, or MCP Inspector) to query connectivity, simulate road closures, compute alternative routes, evaluate hospital reachability, and draft emergency alerts over standard MCP JSON-RPC 2.0.

## Exposed MCP Tools

1. `resolve_location(description, lat=None, lon=None, target_segment_id=None, target_bridge_id=None, max_candidates=5)`
2. `apply_closure(segment_id, description, cause='landslide', bridge_id=None)`
3. `clear_closure(event_id)`
4. `find_cut_off_places(closure_event_id)`
5. `reroute(from_place_id, to_place_id, avoid_event_id=None)`
6. `find_unreachable_hospitals(closure_event_id, threshold_min=None)`
7. `find_weak_links(corridor_id, top_k=5)`
8. `seasonal_risk(month)`
9. `draft_alerts(closure_event_id)`
10. `explain_path(from_place_id, to_place_id, avoid_event_id=None)`
11. `remember_closure(event_id)`
12. `audit_closure(event_id)`

## Running the Server (Windows Command Prompt)

Run from the repository root with the backend virtual environment active:

```bat
cd backend
.venv\Scripts\activate.bat

:: Stdio transport against the deterministic synthetic fixture (offline)
python ..\mcp\server.py --fixture

:: Stdio transport against cached normalized corridor data (offline)
python ..\mcp\server.py --dataset ..\data\cache\normalized_corridor.json

:: Stdio transport against live FalkorDB instance (Docker running)
python ..\mcp\server.py --graph landslide

:: Inspect exposed tool schemas
python ..\mcp\server.py --fixture --list-tools
```

## Connecting from Claude Desktop

Add the following to your Claude Desktop configuration file (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "landslide-router": {
      "command": "c:\\Users\\User\\Downloads\\LS-router\\Ls-Router\\backend\\.venv\\Scripts\\python.exe",
      "args": [
        "c:\\Users\\User\\Downloads\\LS-router\\Ls-Router\\mcp\\server.py",
        "--fixture"
      ]
    }
  }
}
```
