"""Tests for Phase 4 Model Context Protocol (MCP) Server, Client, Handoff Logging, and CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import scenarios as SC  # noqa: E402
from app.agent import cli as agent_cli  # noqa: E402
from app.agent.orchestrator import LandslideAgent  # noqa: E402
from app.graph.errors import ToolInputError, ToolStateError  # noqa: E402
from app.graph.store import FalkorStore  # noqa: E402
from app.mcp_bridge import (  # noqa: E402
    EXPECTED_MCP_TOOLS,
    MCPTimeoutError,
    MCPToolClient,
    build_mcp_server,
    redact_sensitive_text,
    sanitize_payload,
)
from app.mcp_bridge import cli as mcp_cli  # noqa: E402
from fake_graph import FakeGraph  # noqa: E402


class MCPWorld:
    def __init__(self, ds=None, simulated_delay_s: float = 0.0, timeout_s: float = 15.0):
        self.fake = FakeGraph(ds or SC.scenario_a())
        self.store = FalkorStore(self.fake)
        self._n = self._t = 0
        self.server = build_mcp_server(
            self.store,
            clock=self._clock,
            id_factory=self._id,
            simulated_delay_s=simulated_delay_s,
        )
        self.client = MCPToolClient(
            self.store,
            mcp_server=self.server,
            timeout_s=timeout_s,
        )

    def _id(self) -> str:
        self._n += 1
        return f"e{self._n}"

    def _clock(self) -> str:
        self._t += 1
        return f"2026-10-14T12:00:{self._t:02d}Z"


def test_mcp_tool_discovery_memory_and_stdio():
    # 1. In-memory MCP transport discovery
    w = MCPWorld()
    tools_mem = w.client.list_tools()
    names_mem = [t["name"] for t in tools_mem]
    assert tuple(names_mem) == EXPECTED_MCP_TOOLS
    for t in tools_mem:
        assert t["description"]
        assert t["input_schema"]["type"] == "object"
        assert "properties" in t["input_schema"]
    assert w.client.handoffs[-1]["phase"] == "discovery"
    assert w.client.handoffs[-1]["transport"] == "mcp-memory"
    assert w.client.handoffs[-1]["tool_count"] == 12

    # 2. Stdio subprocess MCP transport discovery against `mcp/server.py --fixture`
    with MCPToolClient(transport="stdio", stdio_args=["--fixture"]) as stdio_client:
        tools_stdio = stdio_client.list_tools()
        names_stdio = [t["name"] for t in tools_stdio]
        assert tuple(names_stdio) == EXPECTED_MCP_TOOLS
        assert stdio_client.server_info["name"] == "landslide-router"
        assert stdio_client.handoffs[-1]["phase"] == "discovery"
        assert stdio_client.handoffs[-1]["transport"] == "mcp-stdio"


def test_mcp_tool_invocation_and_graph_state_changes():
    w = MCPWorld()

    # 1. resolve_location over MCP
    res_env = w.client.call_tool(
        "resolve_location",
        {"description": "Bridge collapse at Bridge One"},
        reason="Resolve bridge mention against corridor snapshot.",
    )
    assert res_env["tool"] == "resolve_location"
    assert res_env["result"]["status"] == "resolved"
    assert res_env["result"]["selected_target"]["segment_id"] == "s2"
    assert res_env["result"]["selected_target"]["bridge_id"] == "BR1"
    assert res_env["mcp_handoff"]["status"] == "ok"

    # 2. apply_closure over MCP -> mutates graph
    cl_env = w.client.call_tool(
        "apply_closure",
        {
            "segment_id": "s2",
            "bridge_id": "BR1",
            "description": "Bridge collapse at Bridge One",
            "cause": "bridge_failure",
        },
        reason="Simulate closure on s2 / BR1.",
    )
    ev_id = cl_env["result"]["event"]["id"]
    assert ev_id == "sim-e1"
    assert w.fake.events[ev_id]["status"] == "active"
    assert (ev_id, "s2", "seg") in w.fake.blocks
    assert (ev_id, "BR1", "bridge") in w.fake.blocks

    # 3. reroute & explain_path over MCP -> reflects active closure (+20 min detour via Bravo)
    rr_env = w.client.call_tool(
        "reroute",
        {"from_place_id": "D", "to_place_id": "K", "avoid_event_id": ev_id},
        reason="Compute detour route avoiding s2.",
    )
    assert rr_env["result"]["route"]["nodes"] == ["D", "J1", "B", "J2", "K"]
    assert rr_env["result"]["additional_travel_time_min"] == 20.0

    ex_env = w.client.call_tool(
        "explain_path",
        {"from_place_id": "D", "to_place_id": "K", "avoid_event_id": ev_id},
        reason="Explain detour route segments.",
    )
    assert [c["segment_id"] for c in ex_env["result"]["contributions"]] == ["s1", "s4", "s5", "s3"]

    # 4. remember_closure over MCP -> records historical failure on BR1
    mem_env = w.client.call_tool(
        "remember_closure",
        {"event_id": ev_id},
        reason="Persist closure in historical memory.",
    )
    assert mem_env["result"]["failure_records_created"] == ["BR1"]

    # 5. clear_closure & audit_closure over MCP -> restores baseline connectivity
    clr_env = w.client.call_tool(
        "clear_closure",
        {"event_id": ev_id},
        reason="Resolve closure sim-e1.",
    )
    assert clr_env["result"]["event"]["status"] == "resolved"
    assert w.fake.events[ev_id]["status"] == "resolved"

    aud_env = w.client.call_tool(
        "audit_closure",
        {"event_id": ev_id},
        reason="Audit baseline restoration after clearing sim-e1.",
    )
    assert aud_env["result"]["resolved"] is True
    assert aud_env["result"]["returned_to_baseline"] is True
    assert len(w.server.invocation_log) == 7


def test_agent_invokes_tools_through_mcp_and_logs_handoffs():
    w = MCPWorld()
    agent = LandslideAgent(w.store, mcp_client=w.client)

    # 1. Ambiguous prompt: resolve_location output causes agent to halt before apply_closure
    rep_ambig = agent.run("Landslide near NH29 kilometre 42.")
    assert rep_ambig["status"] == "clarification_required"
    assert rep_ambig["mcp"]["enabled"] is True
    assert rep_ambig["mcp"]["transport"] == "mcp-memory"
    assert rep_ambig["mcp"]["handoff_count"] == 1
    assert rep_ambig["mcp"]["handoffs"][0]["tool"] == "resolve_location"
    assert "halts closure and requests clarification" in rep_ambig["mcp"]["handoffs"][0]["decision_impact"]
    assert not w.fake.events

    # 2. Confirmed closure on s3: full MCP sequence (Resolve -> Apply -> CutOff -> Hospitals -> Reroute -> Explain -> WeakLinks -> SeasonalRisk -> DraftAlerts)
    rep_s3 = agent.run("Landslide between J2 and Kohima", month=8)
    assert rep_s3["status"] == "completed"
    assert rep_s3["mcp"]["enabled"] is True
    handoff_tools = [h["tool"] for h in rep_s3["mcp"]["handoffs"]]
    assert handoff_tools == [
        "resolve_location",
        "apply_closure",
        "find_cut_off_places",
        "find_unreachable_hospitals",
        "reroute",
        "explain_path",
        "reroute",
        "explain_path",
        "find_weak_links",
        "seasonal_risk",
        "draft_alerts",
    ]
    for h in rep_s3["mcp"]["handoffs"]:
        assert h["status"] == "ok"
        assert h["selection_reason"]
        assert h["decision_impact"]
        assert h["direction"] == "agent -> mcp_server -> falkordb_graph -> mcp_server -> agent"
    assert "### MCP Handoffs" in rep_s3["markdown_report"]


def test_direct_mode_is_never_labeled_as_mcp():
    fake = FakeGraph(SC.scenario_a())
    store = FalkorStore(fake)
    agent = LandslideAgent(store, use_mcp=False)
    rep = agent.run("What are the weak links in the corridor?")
    assert rep["mcp"]["enabled"] is False
    assert rep["mcp"]["transport"] == "direct_python"
    assert rep["mcp"]["handoffs"] == []
    assert rep["tool_trace"][0]["transport"] == "direct_python"
    assert rep["tool_trace"][0]["mcp_handoff"] is None


def test_mcp_input_validation_and_error_handling():
    w = MCPWorld()

    # 1. Unknown tool name
    with pytest.raises(ToolInputError, match="Unknown MCP tool"):
        w.client.call_tool("nonexistent_tool", {})
    assert w.client.handoffs[-1]["status"] == "error"
    assert w.client.handoffs[-1]["error_code"] == "invalid_input"

    # 2. Empty description
    with pytest.raises(ToolInputError, match="non-empty string"):
        w.client.call_tool("resolve_location", {"description": "   "})
    assert w.client.handoffs[-1]["status"] == "error"

    # 3. Out-of-bounds latitude or mismatched coordinate pair
    with pytest.raises(ToolInputError, match="outside valid latitude range"):
        w.client.call_tool("resolve_location", {"description": "Landslide", "lat": 95.0, "lon": 93.8})
    with pytest.raises(ToolInputError, match="Both 'lat' and 'lon'"):
        w.client.call_tool("resolve_location", {"description": "Landslide", "lat": 25.8})

    # 4. Unknown segment_id in apply_closure
    with pytest.raises(ToolInputError, match="unknown road segment"):
        w.client.call_tool("apply_closure", {"segment_id": "unknown_seg", "description": "Landslide"})
    assert w.client.handoffs[-1]["status"] == "error"
    assert w.client.handoffs[-1]["error_code"] == "invalid_input"

    # 5. Clearing an observed historical event raises ToolStateError over MCP
    w.fake.events["obs-2023"] = {
        "id": "obs-2023",
        "data_status": "observed",
        "status": "active",
        "cause": "landslide",
    }
    with pytest.raises(ToolStateError):
        w.client.call_tool("clear_closure", {"event_id": "obs-2023"})
    assert w.client.handoffs[-1]["status"] == "error"
    assert w.client.handoffs[-1]["error_code"] == "invalid_state"


def test_mcp_timeout_handling():
    w = MCPWorld(simulated_delay_s=0.25, timeout_s=0.05)
    with pytest.raises(MCPTimeoutError, match="timed out"):
        w.client.call_tool("seasonal_risk", {"month": 7})
    last_handoff = w.client.handoffs[-1]
    assert last_handoff["status"] == "timeout"
    assert last_handoff["error_code"] == "mcp_timeout"


def test_mcp_sensitive_information_redaction():
    raw = (
        "Reported by officer@nagaland.gov.in (+91 98765-43210) with key sk-1234567890abcdef "
        "from C:\\Users\\Operator\\incidents.txt near Bridge One"
    )
    redacted = redact_sensitive_text(raw)
    assert "officer@nagaland.gov.in" not in redacted
    assert "98765-43210" not in redacted
    assert "sk-1234567890abcdef" not in redacted
    assert "C:\\Users\\Operator" not in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "[REDACTED_SECRET]" in redacted
    assert "[REDACTED_USER_PATH]" in redacted

    payload = sanitize_payload({
        "api_key": "secret-123",
        "source_url": "https://user:pass@www.openstreetmap.org/way/123",
        "note": raw,
    })
    assert payload["api_key"] == "[REDACTED]"
    assert payload["source_url"] == "https://[REDACTED]@www.openstreetmap.org/way/123"

    # End-to-end redaction across MCP apply_closure call
    w = MCPWorld()
    env = w.client.call_tool(
        "apply_closure",
        {"segment_id": "s2", "description": raw},
        reason="Test PII redaction on closure description.",
    )
    saved_desc = env["result"]["event"]["description"]
    assert "officer@nagaland.gov.in" not in saved_desc
    assert "98765-43210" not in saved_desc
    assert "[REDACTED_EMAIL]" in saved_desc


def test_mcp_cli_memory_and_stdio_end_to_end():
    # 1. `app.mcp_bridge.cli --fixture list-tools`
    lines: list[str] = []
    code = mcp_cli.main(["--fixture", "list-tools"], out=lines.append)
    assert code == 0
    listed = json.loads("\n".join(lines))
    assert listed["tool_count"] == 12

    # 2. `app.mcp_bridge.cli --fixture call resolve_location ...`
    lines.clear()
    code = mcp_cli.main(
        ["--fixture", "call", "resolve_location", '{"description": "Bridge collapse at Bridge One"}'],
        out=lines.append,
    )
    assert code == 0
    call_out = json.loads("\n".join(lines))
    assert call_out["result"]["selected_target"]["bridge_id"] == "BR1"

    # 3. `app.mcp_bridge.cli --fixture --transport stdio demo --json`
    lines.clear()
    code = mcp_cli.main(["--fixture", "--transport", "stdio", "--json", "demo"], out=lines.append)
    assert code == 0
    demo_out = json.loads("\n".join(lines))
    assert demo_out["transport"] == "mcp-stdio"
    assert demo_out["discovered_tool_count"] == 12
    assert len(demo_out["steps"]) == 5
    assert demo_out["steps"][0]["report"]["status"] == "clarification_required"
    assert demo_out["steps"][1]["report"]["routes"][0]["additional_travel_time_min"] == 20.0
    assert demo_out["steps"][2]["report"]["closure_audit"]["returned_to_baseline"] is True
    assert demo_out["steps"][3]["report"]["affected_settlements"]["newly_unreachable"]["count"] == 3
    assert demo_out["steps"][4]["report"]["closure_audit"]["returned_to_baseline"] is True

    # 4. `app.agent.cli --fixture --mcp-transport stdio --json`
    lines.clear()
    code = agent_cli.main(
        ["--fixture", "--mcp-transport", "stdio", "--json", "--confirm-id", "s2", "--clear-after", "Landslide near NH29 kilometre 42."],
        out=lines.append,
    )
    assert code == 0
    agent_out = json.loads("\n".join(lines))
    assert agent_out["mcp"]["enabled"] is True
    assert agent_out["mcp"]["transport"] == "mcp-stdio"
    assert agent_out["closure_audit"]["returned_to_baseline"] is True

