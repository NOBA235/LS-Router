"""Graph-backed disaster-routing agent orchestrator (Phase 3).

Implements the Section 10 multi-agent workflow:
1. Parse the event description.
2. Resolve the location against available corridor data.
3. Return candidate road segments or bridges.
4. Require clarification if multiple candidates are ambiguous.
5. Apply a simulated closure after the intended target is identified.
6. Calculate affected settlements.
7. Find unreachable hospitals.
8. Calculate alternate routes.
9. Identify network weak links.
10. Draft alerts.
11. Return a structured report with provenance and warnings.

Every claim in the returned report is derived from actual graph tool results and includes
its supporting path/calculation, executed Cypher queries, algorithm metadata, and source URLs.
"""
from __future__ import annotations

from typing import Callable

from ..graph.errors import ToolError, ToolInputError
from ..graph.tools import Toolbox
from .llm import LLMToolCall, TOOL_SCHEMAS
from .resolver import LocationResolver, parse_event

SYSTEM_PROMPT = (
    "You are Landslide Router, a graph-backed disaster-planning prototype. "
    "Never claim that a location is isolated, a hospital is unreachable, or a route is fastest "
    "unless the graph tools support that conclusion. Every graph-dependent claim must include "
    "its supporting path, calculation, or result. If the network data is incomplete, explicitly "
    "disclose the uncertainty. Never invent geographic features, road closures, routes, travel times, "
    "hospital capabilities, or risk probabilities. You provide planning assistance only and do not "
    "dispatch emergency services."
)

SAFETY_DISCLAIMER = (
    "Planning aid, not emergency dispatch. Routes and risk estimates are based on available data "
    "and simulations. Verify conditions with official authorities before acting."
)


class LandslideAgent:
    """Orchestrates location resolution and the eleven graph tools through MCP with strict provenance tracking."""

    def __init__(
        self,
        store,
        *,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
        llm_client=None,
        tool_executor: Callable[[str, dict], dict] | None = None,
        use_mcp: bool = True,
        mcp_client=None,
        mcp_transport: str = "memory",
        mcp_timeout_s: float = 15.0,
    ):
        self.store = store
        self.toolbox = Toolbox(store, clock=clock, id_factory=id_factory)
        self.resolver = LocationResolver(store)
        self.llm_client = llm_client
        self._external_executor = tool_executor
        self.use_mcp = bool(use_mcp and tool_executor is None)
        if mcp_client is not None:
            self.mcp_client = mcp_client
            self.use_mcp = True
        elif self.use_mcp:
            from ..mcp_bridge.client import MCPToolClient

            self.mcp_client = MCPToolClient(
                store,
                clock=clock,
                id_factory=id_factory,
                transport=mcp_transport,
                timeout_s=mcp_timeout_s,
            )
        else:
            self.mcp_client = None

    # ------------------------------------------------------------------ tool dispatch
    def _dispatch_tool(self, name: str, kwargs: dict, reason: str = "") -> dict:
        if self._external_executor is not None:
            return self._external_executor(name, kwargs)
        if self.use_mcp and self.mcp_client is not None:
            return self.mcp_client.call_tool(name, kwargs, reason=reason)
        if name == "resolve_location":
            return self.resolver.resolve_location(**kwargs)
        if hasattr(self.toolbox, name):
            return getattr(self.toolbox, name)(**kwargs)
        raise ToolInputError(f"Unknown tool {name!r}")

    def _call(self, trace: list[dict], name: str, reason: str, **kwargs) -> dict:
        clean_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        env = self._dispatch_tool(name, clean_kwargs, reason=reason)
        transport = (
            self.mcp_client.transport_label
            if (self.use_mcp and self.mcp_client is not None)
            else "direct_python"
        )
        trace.append({
            "step": len(trace) + 1,
            "tool": name,
            "transport": transport,
            "arguments": clean_kwargs,
            "reason": reason,
            "explanation": env.get("explanation", ""),
            "algorithm": env.get("algorithm", {}),
            "cypher": env.get("cypher", []),
            "params": env.get("params", {}),
            "warnings": env.get("warnings", []),
            "result": env.get("result", {}),
            "graph": env.get("graph", {"nodes": [], "edges": []}),
            "mcp_handoff": env.get("mcp_handoff"),
        })
        return env

    # ------------------------------------------------------------------ provenance helpers
    @staticmethod
    def _extract_sources(env: dict, extra_items: list[dict] | None = None) -> list[dict]:
        seen: dict[str, dict] = {}
        g = env.get("graph") or {}
        for n in g.get("nodes") or []:
            if n.get("id"):
                seen[n["id"]] = {
                    "id": n["id"],
                    "label": n.get("label", "Node"),
                    "name": n.get("name"),
                    "confidence": n.get("confidence"),
                    "data_status": n.get("data_status"),
                    "source_url": n.get("source_url"),
                }
        for e in g.get("edges") or []:
            if e.get("id"):
                seen[e["id"]] = {
                    "id": e["id"],
                    "label": e.get("label", "RoadSegment"),
                    "name": e.get("name") or e.get("ref"),
                    "confidence": e.get("confidence"),
                    "data_status": e.get("data_status"),
                    "source_url": e.get("source_url"),
                }
        for item in extra_items or []:
            iid = item.get("id") or item.get("segment_id") or item.get("bridge_id") or item.get("hospital_id") or item.get("place_id")
            if iid and iid not in seen:
                seen[iid] = {
                    "id": iid,
                    "label": item.get("label", "Entity"),
                    "name": item.get("name") or item.get("segment_name") or item.get("hospital_name"),
                    "confidence": item.get("confidence"),
                    "data_status": item.get("data_status"),
                    "source_url": item.get("source_url"),
                }
        return [seen[k] for k in sorted(seen)]

    def _make_claim(
        self,
        claims: list[dict],
        category: str,
        statement: str,
        env: dict,
        supporting_data: dict,
        extra_sources: list[dict] | None = None,
    ) -> None:
        sources = self._extract_sources(env, extra_sources)
        all_verified = bool(sources) and all(s.get("confidence") == "verified" for s in sources)
        claims.append({
            "claim_id": f"claim-{len(claims) + 1}",
            "category": category,
            "statement": statement,
            "supported_by_tool": env["tool"],
            "supporting_data": supporting_data,
            "provenance": {
                "algorithm": env.get("algorithm", {}),
                "cypher_executed": env.get("cypher", []),
                "query_names": (env.get("params") or {}).get("query_names", []),
                "sources": sources,
                "all_verified": all_verified,
            },
        })

    # ------------------------------------------------------------------ main entry
    def run(
        self,
        prompt: str,
        *,
        lat: float | None = None,
        lon: float | None = None,
        target_segment_id: str | None = None,
        target_bridge_id: str | None = None,
        month: int | str | None = None,
        top_k: int = 5,
        hospital_threshold_min: float | None = None,
        max_candidates: int = 5,
    ) -> dict:
        """Run the agent on a natural-language event or query and return a structured, provenance-backed report."""
        parsed = parse_event(prompt, lat=lat, lon=lon)
        trace: list[dict] = []
        claims: list[dict] = []
        llm_notes: list[str] = []

        # Optional LLM tool-selection pass (if an LLM client is configured)
        if self.llm_client is not None:
            return self._run_with_llm(
                prompt,
                parsed,
                lat=lat,
                lon=lon,
                target_segment_id=target_segment_id,
                target_bridge_id=target_bridge_id,
                month=month,
                top_k=top_k,
                hospital_threshold_min=hospital_threshold_min,
                max_candidates=max_candidates,
            )

        # Deterministic intent-based tool selection
        intent = parsed.intent
        if target_segment_id is not None or target_bridge_id is not None:
            intent = "closure_impact"

        if intent == "clear_closure" and parsed.explicit_event_ids:
            return self._run_clear_and_audit(prompt, parsed, trace, claims)
        if intent == "audit_closure" and parsed.explicit_event_ids:
            return self._run_audit_only(prompt, parsed, trace, claims)
        if intent == "remember_closure" and parsed.explicit_event_ids:
            return self._run_remember_only(prompt, parsed, trace, claims)
        if intent == "weak_links":
            return self._run_weak_links_only(prompt, parsed, trace, claims, top_k=top_k)
        if intent == "seasonal_risk":
            return self._run_seasonal_risk_only(prompt, parsed, trace, claims, month=month or parsed.month or 7)
        if intent == "reroute":
            return self._run_reroute_only(prompt, parsed, trace, claims)

        return self._run_closure_workflow(
            prompt,
            parsed,
            trace,
            claims,
            lat=lat,
            lon=lon,
            target_segment_id=target_segment_id,
            target_bridge_id=target_bridge_id,
            month=month if month is not None else parsed.month,
            top_k=top_k,
            hospital_threshold_min=hospital_threshold_min,
            max_candidates=max_candidates,
            llm_notes=llm_notes,
        )

    # ------------------------------------------------------------------ primary 11-step closure workflow
    def _run_closure_workflow(
        self,
        prompt: str,
        parsed,
        trace: list[dict],
        claims: list[dict],
        *,
        lat: float | None,
        lon: float | None,
        target_segment_id: str | None,
        target_bridge_id: str | None,
        month: int | str | None,
        top_k: int,
        hospital_threshold_min: float | None,
        max_candidates: int,
        llm_notes: list[str],
    ) -> dict:
        # Steps 1-3: Parse & resolve location against corridor data
        res_env = self._call(
            trace,
            "resolve_location",
            "Resolve the natural-language event location against corridor road segments, bridges, and places.",
            description=prompt,
            lat=lat,
            lon=lon,
            target_segment_id=target_segment_id,
            target_bridge_id=target_bridge_id,
            max_candidates=max_candidates,
        )
        res = res_env["result"]

        # Step 4: Require clarification if multiple candidates are ambiguous (or no match)
        if res["status"] == "ambiguous":
            self._make_claim(
                claims,
                "location_resolution",
                res["clarification_prompt"] or res_env["explanation"],
                res_env,
                {
                    "status": "ambiguous",
                    "candidate_segment_ids": [c["segment_id"] for c in res["candidate_segments"]],
                    "candidate_bridge_ids": [b["bridge_id"] for b in res["candidate_bridges"]],
                },
                extra_sources=res["candidate_segments"] + res["candidate_bridges"],
            )
            return self._assemble_report(
                status="clarification_required",
                prompt=prompt,
                parsed=parsed,
                trace=trace,
                claims=claims,
                resolution=res,
            )

        if res["status"] == "no_match":
            self._make_claim(
                claims,
                "location_resolution",
                res_env["explanation"],
                res_env,
                {"status": "no_match", "candidate_segment_ids": [], "candidate_bridge_ids": []},
            )
            return self._assemble_report(
                status="no_match",
                prompt=prompt,
                parsed=parsed,
                trace=trace,
                claims=claims,
                resolution=res,
            )

        selected = res["selected_target"]
        seg_id = selected["segment_id"]
        br_id = selected.get("bridge_id")
        self._make_claim(
            claims,
            "location_resolution",
            (
                f"Resolved event location to road segment {seg_id} "
                f"({selected['from_place_id']} -> {selected['to_place_id']}, confidence={selected['confidence']})"
                + (f" and bridge {br_id}." if br_id else ".")
            ),
            res_env,
            selected,
            extra_sources=[selected],
        )

        # Step 5: Apply simulated closure after intended target is identified
        closure_env = self._call(
            trace,
            "apply_closure",
            f"Apply simulated closure on resolved segment {seg_id}.",
            segment_id=seg_id,
            description=prompt,
            cause=parsed.cause,
            bridge_id=br_id,
        )
        closure_res = closure_env["result"]
        event_id = closure_res["event"]["id"]
        self._make_claim(
            claims,
            "closure_applied",
            closure_env["explanation"],
            closure_env,
            {
                "event_id": event_id,
                "target_segment_id": seg_id,
                "bridge_id": br_id,
                "state_change": closure_res["state_change"],
                "location_confidence": closure_res["location_confidence"],
            },
            extra_sources=[closure_res["target_segment"]],
        )

        # Step 6: Calculate affected settlements
        cutoff_env = self._call(
            trace,
            "find_cut_off_places",
            f"Compare baseline vs closed reachability from Dimapur under closure {event_id}.",
            closure_event_id=event_id,
        )
        cutoff_res = cutoff_env["result"]
        self._make_claim(
            claims,
            "reachability",
            cutoff_env["explanation"],
            cutoff_env,
            {
                "origin": cutoff_res["reachability"]["origin"],
                "baseline_reachable_places": cutoff_res["reachability"]["baseline_reachable_places"],
                "reachable_with_active_closures": cutoff_res["reachability"]["reachable_with_active_closures"],
                "newly_unreachable_count": cutoff_res["newly_unreachable"]["count"],
                "already_unreachable_in_baseline": [p["id"] for p in cutoff_res["already_unreachable_in_baseline"]],
                "from_kohima": cutoff_res["from_kohima"],
                "component_analysis": cutoff_res["component_analysis"],
            },
        )
        for group in ("villages", "towns", "junctions", "other"):
            for item in cutoff_res["newly_unreachable"].get(group, []):
                ev_path = item["evidence"]
                self._make_claim(
                    claims,
                    "settlement_cut_off",
                    (
                        f"{item['name'] or item['id']} ({item['type']}) is newly unreachable from "
                        f"{cutoff_res['reachability']['origin']['name'] or cutoff_res['reachability']['origin']['id']}; "
                        f"its baseline path {ev_path['baseline_path_nodes']} via segments {ev_path['baseline_path_segments']} "
                        f"is blocked at {ev_path['blocked_segments_on_path']}."
                    ),
                    cutoff_env,
                    item,
                )

        # Step 7: Find unreachable hospitals
        hosp_env = self._call(
            trace,
            "find_unreachable_hospitals",
            f"Evaluate hospital access from settlements newly affected by closure {event_id}.",
            closure_event_id=event_id,
            threshold_min=hospital_threshold_min,
        )
        hosp_res = hosp_env["result"]
        self._make_claim(
            claims,
            "hospital_reachability",
            hosp_env["explanation"],
            hosp_env,
            {
                "affected_settlements": hosp_res["affected_settlements"],
                "hospitals_with_no_usable_route_from_any_affected_settlement": hosp_res[
                    "hospitals_with_no_usable_route_from_any_affected_settlement"
                ],
                "hospitals_without_road_attachment": hosp_res["hospitals_without_road_attachment"],
                "per_settlement": hosp_res["per_settlement"],
            },
            extra_sources=hosp_res["per_hospital"],
        )

        # Step 8: Calculate and explain alternate routes
        routes_out: list[dict] = []
        snap = self.store.load_snapshot()
        self.store.reset_log()
        dimapur = snap.reference("dimapur")
        kohima = snap.reference("kohima")
        route_pairs: list[tuple[str, str, str]] = []
        if dimapur and kohima:
            route_pairs.append((dimapur.id, kohima.id, "Primary corridor route (Dimapur -> Kohima)"))
        from_p, to_p = selected["from_place_id"], selected["to_place_id"]
        if from_p and to_p and (not route_pairs or (from_p, to_p) != (route_pairs[0][0], route_pairs[0][1])):
            route_pairs.append((from_p, to_p, f"Local bypass across closed segment ({snap.label(from_p)} -> {snap.label(to_p)})"))

        for orig_id, dest_id, label in route_pairs:
            reroute_env = self._call(
                trace,
                "reroute",
                f"Calculate minimum-travel-time alternate route for {label} avoiding {event_id}.",
                from_place_id=orig_id,
                to_place_id=dest_id,
                avoid_event_id=event_id,
            )
            explain_env = self._call(
                trace,
                "explain_path",
                f"Explain segment-by-segment travel time and exclusions for {label}.",
                from_place_id=orig_id,
                to_place_id=dest_id,
                avoid_event_id=event_id,
            )
            rr = reroute_env["result"]
            ex = explain_env["result"]
            entry = {
                "label": label,
                "from_place_id": orig_id,
                "from_place_name": snap.label(orig_id),
                "to_place_id": dest_id,
                "to_place_name": snap.label(dest_id),
                "status": rr["status"],
                "reason": rr["reason"],
                "route": rr["route"],
                "baseline_route": rr["baseline_route"],
                "additional_travel_time_min": rr["additional_travel_time_min"],
                "explanation_details": ex,
            }
            routes_out.append(entry)
            self._make_claim(
                claims,
                "alternate_route",
                f"{label}: {reroute_env['explanation']}",
                reroute_env,
                {
                    "from_place_id": orig_id,
                    "to_place_id": dest_id,
                    "status": rr["status"],
                    "reason": rr["reason"],
                    "route_nodes": rr["route"]["nodes"] if rr["route"] else None,
                    "route_segments": [s["segment_id"] for s in rr["route"]["segments"]] if rr["route"] else None,
                    "total_travel_time_min": rr["route"]["total_travel_time_min"] if rr["route"] else None,
                    "baseline_travel_time_min": rr["baseline_route"]["total_travel_time_min"] if rr["baseline_route"] else None,
                    "additional_travel_time_min": rr["additional_travel_time_min"],
                },
                extra_sources=(rr["route"]["segments"] if rr["route"] else []),
            )
            self._make_claim(
                claims,
                "path_explanation",
                f"{label} path breakdown: {explain_env['explanation']}",
                explain_env,
                {
                    "status": ex["status"],
                    "selected_path": ex["selected_path"],
                    "contributions": ex.get("contributions"),
                    "exclusions": ex.get("exclusions"),
                },
                extra_sources=ex.get("contributions") or [],
            )

        # Step 9: Identify network weak links (and seasonal risk when month is provided)
        corridors = sorted(snap.corridors())
        weak_res = None
        if corridors:
            weak_env = self._call(
                trace,
                "find_weak_links",
                f"Identify structural weak links in corridor {corridors[0]} by betweenness centrality.",
                corridor_id=corridors[0],
                top_k=top_k,
            )
            weak_res = weak_env["result"]
            self._make_claim(
                claims,
                "weak_link",
                weak_env["explanation"],
                weak_env,
                {
                    "corridor_id": weak_res["corridor_id"],
                    "top_segments": [
                        {"rank": s["rank"], "segment_id": s["segment_id"], "betweenness": s["betweenness"], "if_closed": s.get("if_closed")}
                        for s in weak_res["segments"]
                    ],
                    "top_bridges": [
                        {"rank": b["rank"], "bridge_id": b["bridge_id"], "betweenness": b["betweenness"]}
                        for b in weak_res["bridges"]
                    ],
                },
                extra_sources=weak_res["segments"] + weak_res["bridges"],
            )

        seasonal_res = None
        if month is not None:
            s_env = self._call(
                trace,
                "seasonal_risk",
                f"Evaluate seasonal monsoon risk ranking for month={month!r}.",
                month=month,
            )
            seasonal_res = s_env["result"]
            self._make_claim(
                claims,
                "seasonal_risk",
                s_env["explanation"],
                s_env,
                {
                    "month": seasonal_res["month"],
                    "seasons": seasonal_res["seasons"],
                    "ranked": seasonal_res["ranked"],
                },
                extra_sources=seasonal_res["ranked"],
            )

        # Step 10: Draft community alerts
        alerts_env = self._call(
            trace,
            "draft_alerts",
            f"Draft synthetic SMS alerts for settlements newly cut off by closure {event_id}.",
            closure_event_id=event_id,
        )
        alerts_res = alerts_env["result"]
        self._make_claim(
            claims,
            "draft_alert",
            alerts_env["explanation"],
            alerts_env,
            {
                "settlements_considered": alerts_res["settlements_considered"],
                "alerts": [
                    {
                        "alert_id": a["alert_id"],
                        "target_place_id": a["target_place_id"],
                        "text": a["text"],
                        "routed_via_segments": a["routed_via_segments"],
                    }
                    for a in alerts_res["alerts"]
                ],
            },
        )

        # Step 11: Return structured report with provenance and warnings
        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            resolution=res,
            closure=closure_res,
            affected_settlements=cutoff_res,
            hospital_reachability=hosp_res,
            routes=routes_out,
            weak_links=weak_res,
            seasonal_risk=seasonal_res,
            draft_alerts=alerts_res,
            llm_notes=llm_notes,
        )

    # ------------------------------------------------------------------ targeted query workflows
    def _run_clear_and_audit(self, prompt: str, parsed, trace: list[dict], claims: list[dict]) -> dict:
        eid = parsed.explicit_event_ids[0]
        c_env = self._call(trace, "clear_closure", f"Resolve simulated closure {eid}.", event_id=eid)
        self._make_claim(claims, "closure_resolution", c_env["explanation"], c_env, c_env["result"])
        a_env = self._call(trace, "audit_closure", f"Audit network state after resolving {eid}.", event_id=eid)
        self._make_claim(claims, "closure_audit", a_env["explanation"], a_env, a_env["result"])
        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            closure_resolution=c_env["result"],
            closure_audit=a_env["result"],
        )

    def _run_audit_only(self, prompt: str, parsed, trace: list[dict], claims: list[dict]) -> dict:
        eid = parsed.explicit_event_ids[0]
        a_env = self._call(trace, "audit_closure", f"Audit closure {eid} against baseline network.", event_id=eid)
        self._make_claim(claims, "closure_audit", a_env["explanation"], a_env, a_env["result"])
        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            closure_audit=a_env["result"],
        )

    def _run_remember_only(self, prompt: str, parsed, trace: list[dict], claims: list[dict]) -> dict:
        eid = parsed.explicit_event_ids[0]
        m_env = self._call(trace, "remember_closure", f"Persist closure {eid} in historical memory.", event_id=eid)
        self._make_claim(claims, "historical_memory", m_env["explanation"], m_env, m_env["result"])
        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            remembered_closure=m_env["result"],
        )

    def _run_weak_links_only(self, prompt: str, parsed, trace: list[dict], claims: list[dict], *, top_k: int) -> dict:
        snap = self.store.load_snapshot()
        self.store.reset_log()
        corridors = sorted(snap.corridors())
        if not corridors:
            raise ToolInputError("No corridor exists in the graph.")
        w_env = self._call(
            trace,
            "find_weak_links",
            f"Rank weak links in corridor {corridors[0]}.",
            corridor_id=corridors[0],
            top_k=top_k,
        )
        self._make_claim(
            claims,
            "weak_link",
            w_env["explanation"],
            w_env,
            w_env["result"],
            extra_sources=w_env["result"]["segments"] + w_env["result"]["bridges"],
        )
        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            weak_links=w_env["result"],
        )

    def _run_seasonal_risk_only(self, prompt: str, parsed, trace: list[dict], claims: list[dict], *, month: int | str) -> dict:
        s_env = self._call(trace, "seasonal_risk", f"Evaluate seasonal monsoon risk for month={month!r}.", month=month)
        self._make_claim(
            claims,
            "seasonal_risk",
            s_env["explanation"],
            s_env,
            s_env["result"],
            extra_sources=s_env["result"]["ranked"],
        )
        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            seasonal_risk=s_env["result"],
        )

    def _run_reroute_only(self, prompt: str, parsed, trace: list[dict], claims: list[dict]) -> dict:
        snap = self.store.load_snapshot()
        self.store.reset_log()
        places = self.resolver._find_mentioned_places(snap, prompt, parsed)
        if len(places) >= 2:
            orig_id, dest_id = places[0].id, places[1].id
        else:
            d, k = snap.reference("dimapur"), snap.reference("kohima")
            if not (d and k):
                raise ToolInputError("Specify two known place names or IDs to route between.")
            orig_id, dest_id = d.id, k.id

        avoid_id = parsed.explicit_event_ids[0] if parsed.explicit_event_ids else None
        rr_env = self._call(
            trace,
            "reroute",
            f"Find minimum-travel-time route from {orig_id} to {dest_id}.",
            from_place_id=orig_id,
            to_place_id=dest_id,
            avoid_event_id=avoid_id,
        )
        ex_env = self._call(
            trace,
            "explain_path",
            f"Explain route calculation from {orig_id} to {dest_id}.",
            from_place_id=orig_id,
            to_place_id=dest_id,
            avoid_event_id=avoid_id,
        )
        rr, ex = rr_env["result"], ex_env["result"]
        self._make_claim(
            claims,
            "alternate_route",
            rr_env["explanation"],
            rr_env,
            rr,
            extra_sources=(rr["route"]["segments"] if rr["route"] else []),
        )
        self._make_claim(
            claims,
            "path_explanation",
            ex_env["explanation"],
            ex_env,
            ex,
            extra_sources=ex.get("contributions") or [],
        )
        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            routes=[
                {
                    "label": f"{snap.label(orig_id)} -> {snap.label(dest_id)}",
                    "from_place_id": orig_id,
                    "from_place_name": snap.label(orig_id),
                    "to_place_id": dest_id,
                    "to_place_name": snap.label(dest_id),
                    "status": rr["status"],
                    "reason": rr["reason"],
                    "route": rr["route"],
                    "baseline_route": rr["baseline_route"],
                    "additional_travel_time_min": rr["additional_travel_time_min"],
                    "explanation_details": ex,
                }
            ],
        )

    # ------------------------------------------------------------------ LLM tool-calling loop with guardrails
    def _run_with_llm(
        self,
        prompt: str,
        parsed,
        *,
        lat: float | None,
        lon: float | None,
        target_segment_id: str | None,
        target_bridge_id: str | None,
        month: int | str | None,
        top_k: int,
        hospital_threshold_min: float | None,
        max_candidates: int,
    ) -> dict:
        trace: list[dict] = []
        claims: list[dict] = []
        llm_notes: list[str] = []
        messages: list[dict] = [{"role": "user", "content": prompt}]
        resolution_env: dict | None = None
        confirmed_seg = target_segment_id

        for _step in range(10):
            turn = self.llm_client.step(SYSTEM_PROMPT, messages, TOOL_SCHEMAS)
            if not turn.tool_calls:
                if turn.final_text:
                    llm_notes.append(turn.final_text)
                break

            assistant_calls = []
            tool_messages = []
            blocked_by_ambiguity = False

            for tc in turn.tool_calls:
                args = dict(tc.arguments or {})
                if tc.name == "resolve_location":
                    args.setdefault("description", prompt)
                    if lat is not None:
                        args.setdefault("lat", lat)
                    if lon is not None:
                        args.setdefault("lon", lon)
                    if target_segment_id is not None:
                        args["target_segment_id"] = target_segment_id
                    if target_bridge_id is not None:
                        args["target_bridge_id"] = target_bridge_id

                # Hard guardrail: never allow apply_closure before location resolution succeeds unambiguously
                if tc.name == "apply_closure":
                    if resolution_env is None:
                        resolution_env = self._call(
                            trace,
                            "resolve_location",
                            "Guardrail: resolve location before applying a closure.",
                            description=prompt,
                            lat=lat,
                            lon=lon,
                            target_segment_id=target_segment_id,
                            target_bridge_id=target_bridge_id,
                            max_candidates=max_candidates,
                        )
                    res_status = resolution_env["result"]["status"]
                    if res_status != "resolved" and confirmed_seg is None:
                        blocked_by_ambiguity = True
                        break
                    if resolution_env["result"]["selected_target"]:
                        args["segment_id"] = resolution_env["result"]["selected_target"]["segment_id"]

                env = self._call(trace, tc.name, tc.reason or f"Selected by LLM ({turn.provider}:{turn.model})", **args)
                if tc.name == "resolve_location":
                    resolution_env = env
                    if env["result"]["status"] != "resolved" and confirmed_seg is None:
                        blocked_by_ambiguity = True
                        break

                assistant_calls.append({"id": tc.id, "name": tc.name, "arguments": args})
                tool_messages.append({"role": "tool", "tool_call_id": tc.id, "content": env["result"]})

            if blocked_by_ambiguity and resolution_env is not None:
                res = resolution_env["result"]
                self._make_claim(
                    claims,
                    "location_resolution",
                    res.get("clarification_prompt") or resolution_env["explanation"],
                    resolution_env,
                    {
                        "status": res["status"],
                        "candidate_segment_ids": [c["segment_id"] for c in res["candidate_segments"]],
                        "candidate_bridge_ids": [b["bridge_id"] for b in res["candidate_bridges"]],
                    },
                    extra_sources=res["candidate_segments"] + res["candidate_bridges"],
                )
                return self._assemble_report(
                    status="clarification_required" if res["status"] == "ambiguous" else "no_match",
                    prompt=prompt,
                    parsed=parsed,
                    trace=trace,
                    claims=claims,
                    resolution=res,
                    llm_notes=llm_notes,
                )

            if assistant_calls:
                messages.append({"role": "assistant", "content": turn.final_text or "", "tool_calls": assistant_calls})
                messages.extend(tool_messages)

        # Ensure complete grounded report via deterministic workflow if closure impact was requested
        if not trace:
            return self._run_closure_workflow(
                prompt,
                parsed,
                trace,
                claims,
                lat=lat,
                lon=lon,
                target_segment_id=target_segment_id,
                target_bridge_id=target_bridge_id,
                month=month if month is not None else parsed.month,
                top_k=top_k,
                hospital_threshold_min=hospital_threshold_min,
                max_candidates=max_candidates,
                llm_notes=llm_notes,
            )

        # Build claims and structured sections strictly from executed tool envelopes in `trace`
        by_tool: dict[str, list[dict]] = {}
        for step in trace:
            by_tool.setdefault(step["tool"], []).append(step)

        res_step = by_tool.get("resolve_location", [None])[0]
        closure_step = by_tool.get("apply_closure", [None])[0]
        if closure_step and (
            "find_cut_off_places" not in by_tool
            or "find_unreachable_hospitals" not in by_tool
            or "reroute" not in by_tool
            or "find_weak_links" not in by_tool
            or "draft_alerts" not in by_tool
        ):
            # Complete any missing required steps deterministically so the report is never partial
            seg_id = closure_step["result"]["target_segment"]["id"]
            br_id = (closure_step["result"]["event"]["blocks_bridges"] or [None])[0]
            trace.clear()
            claims.clear()
            return self._run_closure_workflow(
                prompt,
                parsed,
                trace,
                claims,
                lat=lat,
                lon=lon,
                target_segment_id=seg_id,
                target_bridge_id=br_id,
                month=month if month is not None else parsed.month,
                top_k=top_k,
                hospital_threshold_min=hospital_threshold_min,
                max_candidates=max_candidates,
                llm_notes=llm_notes,
            )

        for step in trace:
            self._make_claim(claims, step["tool"], step["explanation"], step, step["result"])

        return self._assemble_report(
            status="completed",
            prompt=prompt,
            parsed=parsed,
            trace=trace,
            claims=claims,
            resolution=res_step["result"] if res_step else None,
            closure=closure_step["result"] if closure_step else None,
            affected_settlements=by_tool["find_cut_off_places"][0]["result"] if "find_cut_off_places" in by_tool else None,
            hospital_reachability=by_tool["find_unreachable_hospitals"][0]["result"] if "find_unreachable_hospitals" in by_tool else None,
            routes=[
                {
                    "label": f"{s['result']['from_place_id']} -> {s['result']['to_place_id']}",
                    "from_place_id": s["result"]["from_place_id"],
                    "to_place_id": s["result"]["to_place_id"],
                    "status": s["result"]["status"],
                    "reason": s["result"]["reason"],
                    "route": s["result"]["route"],
                    "baseline_route": s["result"]["baseline_route"],
                    "additional_travel_time_min": s["result"]["additional_travel_time_min"],
                }
                for s in by_tool.get("reroute", [])
            ],
            weak_links=by_tool["find_weak_links"][0]["result"] if "find_weak_links" in by_tool else None,
            seasonal_risk=by_tool["seasonal_risk"][0]["result"] if "seasonal_risk" in by_tool else None,
            draft_alerts=by_tool["draft_alerts"][0]["result"] if "draft_alerts" in by_tool else None,
            llm_notes=llm_notes,
        )

    # ------------------------------------------------------------------ report & markdown assembly
    def _assemble_report(
        self,
        *,
        status: str,
        prompt: str,
        parsed,
        trace: list[dict],
        claims: list[dict],
        resolution: dict | None = None,
        closure: dict | None = None,
        affected_settlements: dict | None = None,
        hospital_reachability: dict | None = None,
        routes: list[dict] | None = None,
        weak_links: dict | None = None,
        seasonal_risk: dict | None = None,
        draft_alerts: dict | None = None,
        closure_resolution: dict | None = None,
        closure_audit: dict | None = None,
        remembered_closure: dict | None = None,
        llm_notes: list[str] | None = None,
    ) -> dict:
        warnings: list[str] = []
        seen_w: set[str] = set()
        for step in trace:
            for w in step.get("warnings") or []:
                if w not in seen_w:
                    seen_w.add(w)
                    warnings.append(w)

        all_sources: dict[str, dict] = {}
        cypher_queries: list[str] = []
        algorithms_used: list[dict] = []
        seen_alg: set[str] = set()
        for step in trace:
            for q in step.get("cypher") or []:
                if q not in cypher_queries:
                    cypher_queries.append(q)
            alg = step.get("algorithm") or {}
            if alg.get("name") and alg["name"] not in seen_alg:
                seen_alg.add(alg["name"])
                algorithms_used.append(alg)
            for s in self._extract_sources(step):
                all_sources[s["id"]] = s

        sources_list = [all_sources[k] for k in sorted(all_sources)]
        verified_count = sum(1 for s in sources_list if s.get("confidence") == "verified")
        unverified_count = sum(1 for s in sources_list if s.get("confidence") != "verified")

        provenance = {
            "tools_called": [step["tool"] for step in trace],
            "total_tool_calls": len(trace),
            "unique_cypher_statements_executed": len(cypher_queries),
            "cypher_executed": cypher_queries,
            "algorithms": algorithms_used,
            "sources": sources_list,
            "confidence_summary": {
                "total_entities": len(sources_list),
                "verified": verified_count,
                "unverified": unverified_count,
            },
        }

        run_handoffs = [s["mcp_handoff"] for s in trace if s.get("mcp_handoff") is not None]
        if self.use_mcp and self.mcp_client is not None:
            mcp_summary = {
                "enabled": True,
                "transport": self.mcp_client.transport_label,
                "server_info": self.mcp_client.server_info,
                "discovered_tools": [t["name"] for t in self.mcp_client.discovered_tools],
                "handoff_count": len(run_handoffs),
                "handoffs": run_handoffs,
            }
        else:
            mcp_summary = {
                "enabled": False,
                "transport": "direct_python",
                "server_info": None,
                "discovered_tools": [],
                "handoff_count": 0,
                "handoffs": [],
            }

        report = {
            "disclaimer": SAFETY_DISCLAIMER,
            "system_prompt": SYSTEM_PROMPT,
            "status": status,
            "query": prompt,
            "parsed_event": parsed.to_dict(),
            "mcp": mcp_summary,
            "resolution": resolution,
            "closure": closure,
            "affected_settlements": affected_settlements,
            "hospital_reachability": hospital_reachability,
            "routes": routes or [],
            "weak_links": weak_links,
            "seasonal_risk": seasonal_risk,
            "draft_alerts": draft_alerts,
            "closure_resolution": closure_resolution,
            "closure_audit": closure_audit,
            "remembered_closure": remembered_closure,
            "claims": claims,
            "provenance": provenance,
            "warnings": warnings,
            "tool_trace": [
                {
                    "step": s["step"],
                    "tool": s["tool"],
                    "transport": s.get("transport", "direct_python"),
                    "arguments": s["arguments"],
                    "reason": s["reason"],
                    "explanation": s["explanation"],
                    "algorithm": s["algorithm"],
                    "cypher_count": len(s["cypher"]),
                    "query_names": (s.get("params") or {}).get("query_names", []),
                    "warnings": s["warnings"],
                    "mcp_handoff": s.get("mcp_handoff"),
                }
                for s in trace
            ],
        }
        if llm_notes:
            report["llm_notes"] = llm_notes
        report["markdown_report"] = self.format_markdown(report)
        return report

    @staticmethod
    def format_markdown(report: dict) -> str:
        """Render a concise, human-readable Markdown summary grounded strictly in `report`."""
        mcp_info = report.get("mcp") or {}
        transport_str = mcp_info.get("transport", "direct_python")
        lines = [
            f"> **{report['disclaimer']}**",
            "",
            f"## Landslide Router Report -- Status: `{report['status']}`",
            f"- **Input**: {report['query']}",
            f"- **Tool transport**: `{transport_str}`"
            + (
                f" (server=`{mcp_info['server_info']['name']}`, discovered_tools={len(mcp_info.get('discovered_tools', []))}, "
                f"handoffs={mcp_info.get('handoff_count', 0)})"
                if mcp_info.get("enabled") and mcp_info.get("server_info")
                else ""
            ),
            f"- **Tools executed ({report['provenance']['total_tool_calls']})**: "
            + (" -> ".join(f"`{t}`" for t in report["provenance"]["tools_called"]) or "none"),
        ]

        res = report.get("resolution")
        if res:
            lines.append("")
            lines.append("### 1. Location Resolution")
            lines.append(f"- **Resolution status**: `{res['status']}`")
            if res.get("selected_target"):
                st = res["selected_target"]
                lines.append(
                    f"- **Selected segment**: `{st['segment_id']}` ({st['from_place_id']} -> {st['to_place_id']}, "
                    f"confidence=`{st['confidence']}`, source={st['source_url']})"
                )
                if st.get("bridge_id"):
                    lines.append(f"- **Selected bridge**: `{st['bridge_id']}`")
            if res.get("requires_clarification"):
                lines.append(f"- **Clarification required**: {res.get('clarification_prompt')}")
                for c in res.get("candidate_segments", []):
                    km_note = (
                        f", cumulative_km={c['cumulative_km_from_dimapur'][0]}-{c['cumulative_km_from_dimapur'][1]}"
                        if "cumulative_km_from_dimapur" in c
                        else ""
                    )
                    lines.append(
                        f"  - Candidate #{c['rank']}: `{c['segment_id']}` ({c['from_place_id']} -> {c['to_place_id']}, "
                        f"on_nh29={c['on_nh29']}, length_km={c['length_km']}, time={c['travel_time_min']} min{km_note}, "
                        f"confidence=`{c['confidence']}`) -- {'; '.join(c['match_reasons'])}"
                    )

        cl = report.get("closure")
        if cl:
            ev = cl["event"]
            lines.append("")
            lines.append("### 2. Simulated Closure")
            lines.append(
                f"- **Closure Event**: `{ev['id']}` (`data_status={ev['data_status']}`, `status={ev['status']}`, "
                f"blocks_segments={ev['blocks_segments']}, blocks_bridges={ev['blocks_bridges']})"
            )

        aff = report.get("affected_settlements")
        if aff:
            nu = aff["newly_unreachable"]
            lines.append("")
            lines.append("### 3. Connectivity & Affected Settlements (Directed from Dimapur)")
            lines.append(
                f"- **Baseline reachable places**: {aff['reachability']['baseline_reachable_places']} | "
                f"**Reachable with active closures**: {aff['reachability']['reachable_with_active_closures']}"
            )
            lines.append(
                f"- **Newly unreachable places**: {nu['count']} "
                f"(villages={[v['name'] or v['id'] for v in nu['villages']]}, "
                f"towns={[t['name'] or t['id'] for t in nu['towns']]}, "
                f"junctions={[j['id'] for j in nu['junctions']]})"
            )
            if aff.get("already_unreachable_in_baseline"):
                lines.append(
                    f"- **Pre-existing unreachable in baseline (not newly cut off)**: "
                    f"{[p['name'] or p['id'] for p in aff['already_unreachable_in_baseline']]}"
                )

        hosp = report.get("hospital_reachability")
        if hosp:
            lines.append("")
            lines.append("### 4. Hospital Reachability")
            lines.append(
                f"- **Affected settlements evaluated**: {hosp['affected_settlements']} | "
                f"**Hospitals with no usable route from any affected settlement**: "
                f"{hosp['hospitals_with_no_usable_route_from_any_affected_settlement']}"
            )
            for ps in hosp.get("per_settlement", []):
                statuses = [f"{h['hospital_id']}:{h['status']}" for h in ps["hospitals"]]
                lines.append(f"  - `{ps['village_id']}` ({ps['name']}): {', '.join(statuses)}")

        if report.get("routes"):
            lines.append("")
            lines.append("### 5. Alternate Routes & Travel-Time Estimates")
            for r in report["routes"]:
                if r["status"] == "route_found" and r.get("route"):
                    rt = r["route"]
                    detour = (
                        f" (baseline {r['baseline_route']['total_travel_time_min']} min, detour {r['additional_travel_time_min']:+g} min)"
                        if r.get("baseline_route") and r.get("additional_travel_time_min") is not None
                        else ""
                    )
                    lines.append(
                        f"- **{r['label']}**: `{rt['total_travel_time_min']} min` (estimate){detour} via nodes "
                        f"`{rt['nodes']}` and segments `{[s['segment_id'] for s in rt['segments']]}`"
                    )
                else:
                    lines.append(f"- **{r['label']}**: `no_route` -- {r.get('reason')}")

        wl = report.get("weak_links")
        if wl:
            lines.append("")
            lines.append("### 6. Network Weak Links (Betweenness Centrality)")
            for s in wl.get("segments", [])[:3]:
                lines.append(
                    f"- Rank #{s['rank']} segment `{s['segment_id']}`: betweenness={s['betweenness']}, "
                    f"shortest_path_pairs={s['ordered_pairs_whose_chosen_shortest_path_uses_it']}"
                )

        sr = report.get("seasonal_risk")
        if sr:
            lines.append("")
            lines.append(f"### Seasonal Risk (Month {sr['month']})")
            lines.append(
                f"- **Applicable segments**: {sr.get('applicable_segments', 0)} | "
                f"**Ranked items with risk label or in-season history**: {len(sr.get('ranked', []))}"
            )

        da = report.get("draft_alerts")
        if da:
            lines.append("")
            lines.append("### 7. Draft Community Alerts (Synthetic -- Not Sent)")
            if da.get("alerts"):
                for a in da["alerts"]:
                    lines.append(f"- **`{a['target_place_id']}` ({a['target_name']})**: {a['text']}")
            else:
                lines.append("- No settlements were newly cut off from Dimapur; 0 draft alerts generated.")

        cr = report.get("closure_resolution")
        if cr:
            lines.append("")
            lines.append("### Closure Resolution")
            lines.append(
                f"- **Event `{cr['event']['id']}`**: `{cr['prior_status']}` -> `{cr['event']['status']}` "
                f"(restored_segments={cr['restored_segments']}, still_blocked={cr['still_blocked_by_other_closures']})"
            )

        ca = report.get("closure_audit")
        if ca:
            lines.append("")
            lines.append("### Baseline Audit")
            lines.append(
                f"- **Event `{ca['event']['id']}`**: resolved=`{ca['resolved']}`, "
                f"returned_to_baseline=`{ca['returned_to_baseline']}`, discrepancies={len(ca['discrepancies'])}"
            )

        rc = report.get("remembered_closure")
        if rc:
            lines.append("")
            lines.append("### Historical Memory")
            lines.append(
                f"- **Event `{rc['event']['id']}`**: history_kind=`{rc['history_kind']}`, "
                f"remembered_at=`{rc['remembered_at']}`, failure_records_created={rc['failure_records_created']}"
            )

        if mcp_info.get("enabled") and mcp_info.get("handoffs"):
            lines.append("")
            lines.append("### MCP Handoffs (Agent Reasoning <-> Graph Operations)")
            for h in mcp_info["handoffs"]:
                lines.append(
                    f"- **`{h['handoff_id']}` (`{h['tool']}`, `{h['transport']}`, `{h['duration_ms']} ms`)**: "
                    f"reason=\"{h['selection_reason']}\" -> {h['decision_impact']}"
                )

        prov = report.get("provenance") or {}
        cs = prov.get("confidence_summary") or {}
        lines.append("")
        lines.append("### Provenance & Verification Summary")
        lines.append(
            f"- **Supporting claims**: {len(report.get('claims', []))} | "
            f"**Unique Cypher statements executed**: {prov.get('unique_cypher_statements_executed', 0)} | "
            f"**Entities referenced**: {cs.get('total_entities', 0)} "
            f"(verified={cs.get('verified', 0)}, unverified={cs.get('unverified', 0)})"
        )

        if report.get("warnings"):
            lines.append("")
            lines.append("### Warnings & Uncertainty Disclosures")
            for w in report["warnings"]:
                lines.append(f"- {w}")

        return "\n".join(lines)

