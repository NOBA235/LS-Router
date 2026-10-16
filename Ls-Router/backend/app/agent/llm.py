"""Tool JSON schemas and optional LLM tool-calling clients (Anthropic Messages / OpenAI Chat Completions via httpx).

When an API key is configured (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`), the agent can use the LLM to select tools
and summarize results under the strict guardrails of `orchestrator.py`:
- The LLM may select tools and summarize their results.
- The LLM must never replace deterministic routing calculations with its own guesses.
- Ambiguous location matches are gated in code before `apply_closure` can run.
When no API key is set, the deterministic planner in `orchestrator.py` selects and executes the exact same tool sequence.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import httpx

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "resolve_location",
        "description": (
            "Resolve a natural-language event description or coordinates against the corridor graph. "
            "Returns candidate road segments and bridges, match reasons, provenance, and whether clarification is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Natural-language event or location description."},
                "lat": {"type": ["number", "null"], "description": "Optional latitude."},
                "lon": {"type": ["number", "null"], "description": "Optional longitude."},
                "target_segment_id": {"type": ["string", "null"], "description": "Optional confirmed road segment ID."},
                "target_bridge_id": {"type": ["string", "null"], "description": "Optional confirmed bridge ID."},
                "max_candidates": {"type": "integer", "default": 5},
            },
            "required": ["description"],
        },
    },
    {
        "name": "apply_closure",
        "description": "Create an active simulated ClosureEvent blocking a verified or confirmed road segment (and optional bridge).",
        "input_schema": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "string", "description": "RoadSegment ID to close."},
                "description": {"type": "string", "description": "Description of the simulated closure."},
                "cause": {"type": "string", "default": "landslide"},
                "bridge_id": {"type": ["string", "null"], "description": "Optional Bridge ID ON the segment."},
            },
            "required": ["segment_id", "description"],
        },
    },
    {
        "name": "clear_closure",
        "description": "Resolve an active simulated closure event while preserving its historical record.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Simulated ClosureEvent ID to resolve."},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "find_cut_off_places",
        "description": "Compare baseline vs closed directed reachability from Dimapur and identify newly unreachable settlements and junctions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "closure_event_id": {"type": "string", "description": "ClosureEvent ID."},
            },
            "required": ["closure_event_id"],
        },
    },
    {
        "name": "reroute",
        "description": "Find the minimum-travel-time directed path between two places excluding all currently blocked segments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_place_id": {"type": "string", "description": "Origin Place ID."},
                "to_place_id": {"type": "string", "description": "Destination Place ID."},
                "avoid_event_id": {"type": ["string", "null"], "description": "Optional ClosureEvent ID to avoid."},
            },
            "required": ["from_place_id", "to_place_id"],
        },
    },
    {
        "name": "find_unreachable_hospitals",
        "description": "Evaluate hospital reachability from settlements newly cut off by a closure event.",
        "input_schema": {
            "type": "object",
            "properties": {
                "closure_event_id": {"type": "string", "description": "ClosureEvent ID."},
                "threshold_min": {"type": ["number", "null"], "description": "Optional travel-time threshold in minutes."},
            },
            "required": ["closure_event_id"],
        },
    },
    {
        "name": "find_weak_links",
        "description": "Rank corridor road segments, bridges, and places by travel-time-weighted betweenness centrality.",
        "input_schema": {
            "type": "object",
            "properties": {
                "corridor_id": {"type": "string", "description": "Corridor ID."},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["corridor_id"],
        },
    },
    {
        "name": "seasonal_risk",
        "description": "Rank bridges and road segments for a given month using monsoon-risk labels and documented event history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": ["integer", "string"], "description": "Month number (1-12) or month name."},
            },
            "required": ["month"],
        },
    },
    {
        "name": "draft_alerts",
        "description": "Create synthetic SMS-ready draft alerts for settlements newly cut off by a closure event.",
        "input_schema": {
            "type": "object",
            "properties": {
                "closure_event_id": {"type": "string", "description": "ClosureEvent ID."},
            },
            "required": ["closure_event_id"],
        },
    },
    {
        "name": "explain_path",
        "description": "Explain the selected minimum-travel-time path, per-segment travel-time contributions, exclusions, and provenance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_place_id": {"type": "string", "description": "Origin Place ID."},
                "to_place_id": {"type": "string", "description": "Destination Place ID."},
                "avoid_event_id": {"type": ["string", "null"], "description": "Optional ClosureEvent ID to avoid."},
            },
            "required": ["from_place_id", "to_place_id"],
        },
    },
    {
        "name": "remember_closure",
        "description": "Persist a closure event in historical memory and link bridge failure records where supported.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ClosureEvent ID."},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "audit_closure",
        "description": "Verify whether a closure has been resolved and whether the network has returned to its baseline state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ClosureEvent ID."},
            },
            "required": ["event_id"],
        },
    },
]


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict
    reason: str | None = None


@dataclass
class LLMTurn:
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    final_text: str | None = None
    provider: str = "deterministic"
    model: str = "deterministic-rule-planner"


class OpenAICompatibleLLMClient:
    """Calls an OpenAI-compatible `/chat/completions` endpoint with tool definitions."""

    provider = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini", base_url: str = "https://api.openai.com/v1", timeout_s: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def step(self, system_prompt: str, messages: list[dict], tools: list[dict] = TOOL_SCHEMAS) -> LLMTurn:
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "tools": oai_tools,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            calls.append(LLMToolCall(id=tc.get("id", fn.get("name", "call")), name=fn.get("name", ""), arguments=args))
        return LLMTurn(tool_calls=calls, final_text=msg.get("content"), provider=self.provider, model=self.model)


class AnthropicMessagesLLMClient:
    """Calls the Anthropic `/v1/messages` endpoint with tool definitions."""

    provider = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-3-5-sonnet-latest", base_url: str = "https://api.anthropic.com", timeout_s: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def step(self, system_prompt: str, messages: list[dict], tools: list[dict] = TOOL_SCHEMAS) -> LLMTurn:
        # Convert tool-result messages into Anthropic content blocks
        anth_messages = []
        for m in messages:
            if m["role"] == "tool":
                anth_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", "call"),
                            "content": m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]),
                        }
                    ],
                })
            elif m["role"] == "assistant" and m.get("tool_calls"):
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
                anth_messages.append({"role": "assistant", "content": blocks})
            else:
                anth_messages.append({"role": m["role"], "content": m["content"]})

        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": 0.0,
            "system": system_prompt,
            "messages": anth_messages,
            "tools": tools,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            " content-type": "application/json",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(f"{self.base_url}/v1/messages", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        calls = []
        texts = []
        for block in data.get("content") or []:
            if block.get("type") == "tool_use":
                calls.append(
                    LLMToolCall(
                        id=block.get("id", block.get("name", "call")),
                        name=block.get("name", ""),
                        arguments=dict(block.get("input") or {}),
                    )
                )
            elif block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
        return LLMTurn(
            tool_calls=calls,
            final_text="\n".join(texts) if texts else None,
            provider=self.provider,
            model=self.model,
        )


def llm_client_from_env():
    """Return an LLM client if `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set in the environment, else None."""
    anth_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if anth_key:
        model = (os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-sonnet-latest").strip()
        base = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").strip()
        return AnthropicMessagesLLMClient(api_key=anth_key, model=model, base_url=base)

    oai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if oai_key:
        model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
        base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
        return OpenAICompatibleLLMClient(api_key=oai_key, model=model, base_url=base)

    return None

