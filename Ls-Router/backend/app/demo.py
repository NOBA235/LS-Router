"""Phase 9 reproducible 12-step demonstration runner and clean reset procedure.

Executes the 12-step demonstration workflow against the active `FalkorStore` (live FalkorDB
or cached OpenStreetMap NH29 dataset `data/cache/normalized.json`, or the deterministic
synthetic fixture for unit tests). Every count, travel time, detour delta, centrality score,
and alert draft is computed dynamically from the actual graph via MCP tool calls.
"""
from __future__ import annotations

import time
from typing import Any

from .agent.resolver import LocationResolver
from .graph.store import FalkorStore
from .graph.tools import Toolbox
from .mcp_bridge.client import MCPToolClient

SAFETY_DISCLAIMER = (
    "Planning aid, not emergency dispatch. Routes and risk estimates are based on available "
    "data and simulations. Verify conditions with official authorities before acting."
)


def reset_demo_state(store: FalkorStore) -> dict[str, Any]:
    """Resolve all active simulated closures on the graph and audit return to baseline."""
    started = time.perf_counter()
    toolbox = Toolbox(store)
    snap = store.load_snapshot()
    active_ids = list(snap.active_events())
    resolved_events: list[dict[str, Any]] = []
    last_audit: dict[str, Any] | None = None

    for ev_id in active_ids:
        ev = snap.events.get(ev_id)
        if ev and ev.data_status == "simulated":
            clr = toolbox.clear_closure(event_id=ev_id)["result"]
            aud = toolbox.audit_closure(event_id=ev_id)["result"]
            resolved_events.append(clr)
            last_audit = aud

    snap_after = store.load_snapshot()
    remaining_active = list(snap_after.active_events())
    elapsed_s = round(time.perf_counter() - started, 3)
    returned = (
        len(remaining_active) == 0
        and (bool(last_audit["returned_to_baseline"]) if last_audit and last_audit["returned_to_baseline"] is not None else True)
    )
    return {
        "status": "reset_complete",
        "resolved_count": len(resolved_events),
        "resolved_event_ids": [r["event"]["id"] for r in resolved_events],
        "remaining_active_closures": remaining_active,
        "last_audit": last_audit,
        "back_to_baseline": returned,
        "elapsed_seconds": elapsed_s,
        "disclaimer": SAFETY_DISCLAIMER,
    }


def _pick_demo_targets(store: FalkorStore) -> dict[str, Any]:
    """Select real segments from the loaded graph for (A) detour routing and (B) settlement cut-off."""
    snap = store.load_snapshot()
    dimapur = snap.reference("dimapur")
    kohima = snap.reference("kohima")
    origin_id = dimapur.id if dimapur else sorted(snap.places)[0]
    dest_id = kohima.id if kohima else sorted(snap.places)[-1]

    # Check if this is the real NH29 OpenStreetMap dataset or the synthetic fixture
    if "seg-569069510-569069527-1353575333" in snap.segments:
        return {
            "corridor_id": "nh29-dimapur-kohima",
            "origin_id": origin_id,
            "dest_id": dest_id,
            "ambiguous_prompt": "Landslide at NH29 near kilometre 42.",
            "detour_segment_id": "seg-569069510-569069527-1353575333",
            "cutoff_prompt": "Landslide on NH29 between Kezo Town and Chakhabama",
            "cutoff_segment_id": "seg-2469699767-6286548534-239157848",
        }
    if "s2" in snap.segments and "s7" in snap.segments:
        corridors = sorted(snap.corridors())
        return {
            "corridor_id": corridors[0] if corridors else "corr-test",
            "origin_id": origin_id,
            "dest_id": dest_id,
            "ambiguous_prompt": "Landslide at NH29 near kilometre 42.",
            "detour_segment_id": "s2",
            "cutoff_prompt": "Landslide between Kohima and Charlie",
            "cutoff_segment_id": "s7",
        }

    # Generic fallback for any custom seeded graph
    corridors = sorted(snap.corridors())
    tb = Toolbox(store)
    base_r = tb.reroute(origin_id, dest_id)["result"]
    base_segs = [s["segment_id"] for s in (base_r.get("route") or {}).get("segments", [])]
    detour_seg = base_segs[0] if base_segs else sorted(snap.segments)[0]
    cutoff_seg = base_segs[-1] if len(base_segs) > 1 else detour_seg
    return {
        "corridor_id": corridors[0] if corridors else "nh29-dimapur-kohima",
        "origin_id": origin_id,
        "dest_id": dest_id,
        "ambiguous_prompt": "Landslide at NH29 near kilometre 42.",
        "detour_segment_id": detour_seg,
        "cutoff_prompt": f"Landslide on segment {cutoff_seg}",
        "cutoff_segment_id": cutoff_seg,
    }


def run_twelve_step_demo(
    store: FalkorStore,
    *,
    include_weak_links: bool = True,
) -> dict[str, Any]:
    """Execute the reproducible 12-step Phase 9 demonstration against `store` via MCP.

    Steps:
      1. Show the corridor map and sourced road network.
      2. Enter a simulated landslide location (natural-language input).
      3. Show the resolved road segment, candidate disambiguation, and confidence.
      4. Apply the closure through MCP (`apply_closure`).
      5. Show the actual reachability changes (`find_cut_off_places` & `find_unreachable_hospitals`).
      6. Calculate an alternate route if one exists (`reroute`).
      7. Show the total estimated travel time and detour (`explain_path` with `baseline_comparison`).
      8. Draft alerts based on the calculated impact (`draft_alerts`).
      9. Inspect the graph paths and tool queries (executed Cypher & supporting subgraph).
     10. Display weak links (`find_weak_links`) and seasonal risk evidence (`seasonal_risk`).
     11. Resolve the closure (`clear_closure`).
     12. Audit the resulting graph state (`audit_closure`).
    """
    overall_start = time.perf_counter()

    # Ensure a clean baseline before starting the demo
    reset_info = reset_demo_state(store)
    targets = _pick_demo_targets(store)
    resolver = LocationResolver(store)
    mcp = MCPToolClient(store, transport="memory", timeout_s=60.0)

    steps: list[dict[str, Any]] = []

    try:
        # ------------------------------------------------------------------
        # STEP 1: Show the corridor map and sourced road network
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        store.reset_log()
        snap = store.load_snapshot()
        load_queries = store.drain_log()
        nh29_segs = [s for s in snap.segments.values() if s.on_nh29]
        nh29_km = round(sum(s.length_km or 0.0 for s in nh29_segs), 3)
        base_route_env = mcp.call_tool(
            "reroute",
            {
                "from_place_id": targets["origin_id"],
                "to_place_id": targets["dest_id"],
            },
            reason="Step 1: Verify baseline Dimapur-Kohima route on the sourced corridor network",
        )
        base_route = base_route_env["result"]["route"]
        steps.append(
            {
                "step": 1,
                "title": "Corridor Map & Sourced Road Network",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Loaded corridor '{targets['corridor_id']}' with {len(snap.places)} places, "
                    f"{len(snap.segments)} road segments ({len(nh29_segs)} on NH29 covering {nh29_km} km), "
                    f"{len(snap.bridges)} bridges, {len(snap.hospitals)} hospitals, and {len(snap.events)} stored events. "
                    f"Baseline {snap.label(targets['origin_id'])} -> {snap.label(targets['dest_id'])} route: "
                    f"{base_route['total_travel_time_min']} min ({base_route['total_length_km']} km across "
                    f"{len(base_route['segments'])} segments)."
                ),
                "data": {
                    "corridor_id": targets["corridor_id"],
                    "counts": {
                        "places": len(snap.places),
                        "villages": sum(1 for p in snap.places.values() if p.type == "village"),
                        "towns": sum(1 for p in snap.places.values() if p.type == "town"),
                        "junctions": sum(1 for p in snap.places.values() if p.type == "junction"),
                        "segments": len(snap.segments),
                        "nh29_segments": len(nh29_segs),
                        "nh29_length_km": nh29_km,
                        "bridges": len(snap.bridges),
                        "hospitals": len(snap.hospitals),
                        "stored_events": len(snap.events),
                    },
                    "baseline_route": {
                        "from_place_id": targets["origin_id"],
                        "from_name": snap.label(targets["origin_id"]),
                        "to_place_id": targets["dest_id"],
                        "to_name": snap.label(targets["dest_id"]),
                        "total_travel_time_min": base_route["total_travel_time_min"],
                        "total_length_km": base_route["total_length_km"],
                        "segment_count": len(base_route["segments"]),
                    },
                    "cypher_count": len(load_queries) + len(base_route_env["cypher"]),
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 2: Enter a simulated landslide location
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        amb_env = resolver.resolve_location(targets["ambiguous_prompt"])
        amb_res = amb_env["result"]
        steps.append(
            {
                "step": 2,
                "title": "Enter Simulated Landslide Location (Ambiguity Gating)",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Input '{targets['ambiguous_prompt']}' returned status='{amb_res['status']}' "
                    f"(requires_clarification={amb_res['requires_clarification']}) with "
                    f"{len(amb_res['candidate_segments'])} candidate segments; graph is not mutated until confirmed."
                ),
                "data": {
                    "input_description": targets["ambiguous_prompt"],
                    "status": amb_res["status"],
                    "requires_clarification": amb_res["requires_clarification"],
                    "clarification_prompt": amb_res["clarification_prompt"],
                    "candidate_segments": [
                        {
                            "segment_id": c["segment_id"],
                            "ref": c["ref"],
                            "confidence": c["confidence"],
                            "travel_time_min": c["travel_time_min"],
                            "match_reasons": c["match_reasons"],
                        }
                        for c in amb_res["candidate_segments"]
                    ],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 3: Show the resolved road segment and its confidence
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        conf_detour_env = resolver.resolve_location(
            targets["ambiguous_prompt"],
            target_segment_id=targets["detour_segment_id"],
        )
        conf_detour = conf_detour_env["result"]["selected_target"]
        conf_cutoff_env = resolver.resolve_location(
            targets["cutoff_prompt"],
            target_segment_id=targets["cutoff_segment_id"],
        )
        conf_cutoff = conf_cutoff_env["result"]["selected_target"]
        steps.append(
            {
                "step": 3,
                "title": "Resolved Road Segments & Source Confidence",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Confirmed trunk detour segment '{conf_detour['segment_id']}' "
                    f"(confidence='{conf_detour['confidence']}', source='{conf_detour['source_url']}') and "
                    f"settlement cut-off segment '{conf_cutoff['segment_id']}' "
                    f"(confidence='{conf_cutoff['confidence']}', source='{conf_cutoff['source_url']}')."
                ),
                "data": {
                    "detour_target": conf_detour,
                    "cutoff_target": conf_cutoff,
                    "warnings": conf_detour_env["warnings"],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 4: Apply the closure (via MCP)
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        apply_detour_env = mcp.call_tool(
            "apply_closure",
            {
                "segment_id": targets["detour_segment_id"],
                "description": targets["ambiguous_prompt"],
                "cause": "landslide",
            },
            reason="Step 4a: Apply simulated NH29 trunk closure to evaluate alternate route detour",
        )
        detour_event_id = apply_detour_env["result"]["event"]["id"]

        apply_cutoff_env = mcp.call_tool(
            "apply_closure",
            {
                "segment_id": targets["cutoff_segment_id"],
                "description": targets["cutoff_prompt"],
                "cause": "landslide",
            },
            reason="Step 4b: Apply simulated branch closure to evaluate village isolation and draft alerts",
        )
        cutoff_event_id = apply_cutoff_env["result"]["event"]["id"]
        steps.append(
            {
                "step": 4,
                "title": "Apply Simulated Closure Through MCP",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Created simulated ClosureEvents '{detour_event_id}' (blocking {targets['detour_segment_id']}) "
                    f"and '{cutoff_event_id}' (blocking {targets['cutoff_segment_id']}) via MCP tool 'apply_closure'."
                ),
                "data": {
                    "detour_closure_event": apply_detour_env["result"]["event"],
                    "cutoff_closure_event": apply_cutoff_env["result"]["event"],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 5: Show the actual reachability changes
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        cutoff_places_env = mcp.call_tool(
            "find_cut_off_places",
            {"closure_event_id": cutoff_event_id},
            reason="Step 5a: Compute settlements newly unreachable from Dimapur",
        )
        hosp_env = mcp.call_tool(
            "find_unreachable_hospitals",
            {"closure_event_id": cutoff_event_id},
            reason="Step 5b: Compute hospital reachability impact for affected settlements",
        )
        cp_res = cutoff_places_env["result"]
        hp_res = hosp_env["result"]
        origin_info = cp_res["reachability"]["origin"]
        cut_settlements = cp_res["newly_unreachable"]["villages"] + cp_res["newly_unreachable"]["towns"]
        cut_names = [v["name"] or v["id"] for v in cut_settlements]
        steps.append(
            {
                "step": 5,
                "title": "Reachability Changes & Hospital Access Impact",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Closure '{cutoff_event_id}' newly cuts off {len(cut_settlements)} settlement(s) "
                    f"({cp_res['newly_unreachable']['count']} total places including junctions) "
                    f"from {origin_info['name'] or origin_info['id']}: {cut_names}. "
                    f"Reachable places with active closures: {cp_res['reachability']['reachable_with_active_closures']}/"
                    f"{cp_res['reachability']['baseline_reachable_places']}. "
                    f"Affected settlements evaluated for hospital access: {len(hp_res['affected_settlements'])}."
                ),
                "data": {
                    "reference_origin": origin_info,
                    "baseline_reachable_places": cp_res["reachability"]["baseline_reachable_places"],
                    "reachable_with_active_closures": cp_res["reachability"]["reachable_with_active_closures"],
                    "newly_unreachable_settlement_count": len(cut_settlements),
                    "newly_unreachable_total_places_count": cp_res["newly_unreachable"]["count"],
                    "newly_unreachable_settlements": cut_names,
                    "attributable_to_this_event": cp_res["attributable_to_this_event"],
                    "pre_existing_unreachable": cp_res["already_unreachable_in_baseline"],
                    "component_analysis": cp_res["component_analysis"],
                    "hospitals_unreachable_from_affected": hp_res[
                        "hospitals_with_no_usable_route_from_any_affected_settlement"
                    ],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 6: Calculate an alternate route if one exists
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        reroute_env = mcp.call_tool(
            "reroute",
            {
                "from_place_id": targets["origin_id"],
                "to_place_id": targets["dest_id"],
                "avoid_event_id": detour_event_id,
            },
            reason="Step 6: Calculate fastest weighted alternate route avoiding blocked NH29 segment",
        )
        rr_res = reroute_env["result"]
        from_label = snap.label(rr_res["from_place_id"])
        to_label = snap.label(rr_res["to_place_id"])
        steps.append(
            {
                "step": 6,
                "title": "Calculate Weighted Alternate Route",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Alternate route status='{rr_res['status']}' from {from_label} to {to_label} "
                    f"across {len((rr_res.get('route') or {}).get('segments', []))} unblocked segments."
                ),
                "data": {
                    "status": rr_res["status"],
                    "from_place_id": rr_res["from_place_id"],
                    "to_place_id": rr_res["to_place_id"],
                    "segment_count": len((rr_res.get("route") or {}).get("segments", [])),
                    "blocked_segments": list(rr_res["blocked_segments"].keys()),
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 7: Show the total estimated travel time and detour
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        explain_env = mcp.call_tool(
            "explain_path",
            {
                "from_place_id": targets["origin_id"],
                "to_place_id": targets["dest_id"],
                "avoid_event_id": detour_event_id,
            },
            reason="Step 7: Explain per-segment travel times and compare against baseline route",
        )
        ex_res = explain_env["result"]
        b_comp = ex_res["baseline_comparison"]
        steps.append(
            {
                "step": 7,
                "title": "Total Estimated Travel Time & Detour Comparison",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Baseline travel time: {b_comp['baseline_total_travel_time_min']} min "
                    f"({b_comp['baseline_total_length_km']} km) -> Detour travel time: "
                    f"{ex_res['total_travel_time_min']} min ({ex_res['total_length_km']} km), "
                    f"adding +{b_comp['detour_travel_time_delta_min']} min "
                    f"(+{b_comp['detour_length_delta_km']} km). "
                    f"Checks: segment_times_sum_to_total={ex_res['checks']['segment_times_sum_to_total']}, "
                    f"no_selected_segment_is_blocked={ex_res['checks']['no_selected_segment_is_blocked']}."
                ),
                "data": {
                    "baseline_total_travel_time_min": b_comp["baseline_total_travel_time_min"],
                    "baseline_total_length_km": b_comp["baseline_total_length_km"],
                    "detour_total_travel_time_min": ex_res["total_travel_time_min"],
                    "detour_total_length_km": ex_res["total_length_km"],
                    "detour_travel_time_delta_min": b_comp["detour_travel_time_delta_min"],
                    "detour_length_delta_km": b_comp["detour_length_delta_km"],
                    "segments_avoided_from_baseline_count": len(b_comp["segments_avoided_from_baseline"]),
                    "segments_added_in_detour_count": len(b_comp["segments_added_in_detour"]),
                    "checks": ex_res["checks"],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 8: Draft alerts based on the calculated impact
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        alerts_env = mcp.call_tool(
            "draft_alerts",
            {"closure_event_id": cutoff_event_id},
            reason="Step 8: Generate synthetic draft SMS alerts for newly cut-off communities",
        )
        al_res = alerts_env["result"]
        alerts_list = al_res["alerts"]
        steps.append(
            {
                "step": 8,
                "title": "Draft Community Alerts (Synthetic Drafts Only — Never Sent)",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Generated {len(alerts_list)} synthetic SMS draft alert(s) for closure '{cutoff_event_id}'. "
                    f"All alerts have status='draft' and sent=False."
                ),
                "data": {
                    "alert_count": len(alerts_list),
                    "all_drafts": all(a["status"] == "draft" and a["sent"] is False for a in alerts_list),
                    "alerts": [
                        {
                            "alert_id": a["alert_id"],
                            "target_place_id": a["target_place_id"],
                            "target_name": a["target_name"],
                            "status": a["status"],
                            "sent": a["sent"],
                            "text": a["text"],
                        }
                        for a in alerts_list
                    ],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 9: Inspect the graph paths and tool queries
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        mcp_handoffs = list(mcp.handoffs)
        steps.append(
            {
                "step": 9,
                "title": "Inspect Graph Paths, Executed Cypher & MCP Handoff Log",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Recorded {len(mcp_handoffs)} real MCP tool invocations and "
                    f"{len(explain_env['cypher'])} Cypher statements for the route explanation."
                ),
                "data": {
                    "mcp_tool_invocations_so_far": len(mcp_handoffs),
                    "sample_cypher_statements": explain_env["cypher"][:4],
                    "sample_route_contributions": ex_res["contributions"][:3],
                    "algorithm": explain_env["algorithm"],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 10: Display weak links and seasonal risk evidence
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        seasonal_env = mcp.call_tool(
            "seasonal_risk",
            {"month": 7},
            reason="Step 10a: Rank corridor segments and bridges for July monsoon risk",
        )
        sr_res = seasonal_env["result"]
        wl_summary = "Skipped weak-links calculation."
        wl_data: dict[str, Any] = {}
        if include_weak_links:
            wl_env = mcp.call_tool(
                "find_weak_links",
                {"corridor_id": targets["corridor_id"], "top_k": 5},
                reason="Step 10b: Rank top 5 structural weak links by shortest-path betweenness centrality",
            )
            wl_res = wl_env["result"]
            top_seg = wl_res["segments"][0] if wl_res["segments"] else None
            top_br = wl_res["bridges"][0] if wl_res["bridges"] else None
            wl_summary = (
                f"Top weak-link segment: {top_seg['segment_id']} (betweenness={top_seg['betweenness']}) "
                + (
                    f"| Top bridge: {top_br['bridge_id']} (betweenness={top_br['betweenness']}). "
                    if top_br
                    else ". "
                )
            )
            wl_data = {
                "top_segments": wl_res["segments"][:5],
                "top_bridges": wl_res["bridges"][:3],
                "top_places": wl_res["places"][:3],
            }

        top_monsoon = sr_res["ranked"][0] if sr_res["ranked"] else None
        steps.append(
            {
                "step": 10,
                "title": "Corridor Weak Links & July Monsoon Seasonal Risk Evidence",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    wl_summary
                    + (
                        f"July monsoon risk ranked {len(sr_res['ranked'])} elements "
                        f"(top: {top_monsoon['id']} with advisory='{top_monsoon['advisory']}')."
                        if top_monsoon
                        else "July monsoon risk evaluated."
                    )
                ),
                "data": {
                    "weak_links": wl_data,
                    "seasonal_risk_month": sr_res["month"],
                    "seasonal_risk_ranking_method": sr_res["ranking_method"],
                    "top_seasonal_items": sr_res["ranked"][:5],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 11: Resolve the closure
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        clear_detour_env = mcp.call_tool(
            "clear_closure",
            {"event_id": detour_event_id},
            reason="Step 11a: Resolve trunk detour simulated closure",
        )
        clear_cutoff_env = mcp.call_tool(
            "clear_closure",
            {"event_id": cutoff_event_id},
            reason="Step 11b: Resolve branch cut-off simulated closure",
        )
        steps.append(
            {
                "step": 11,
                "title": "Resolve Simulated Closures",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Resolved simulated closures '{detour_event_id}' and '{cutoff_event_id}'; "
                    f"blocked segments remaining: {clear_cutoff_env['result']['blocked_segments_after']}."
                ),
                "data": {
                    "resolved_events": [
                        clear_detour_env["result"]["event"],
                        clear_cutoff_env["result"]["event"],
                    ],
                    "blocked_segments_after": clear_cutoff_env["result"]["blocked_segments_after"],
                },
            }
        )

        # ------------------------------------------------------------------
        # STEP 12: Audit the resulting graph state
        # ------------------------------------------------------------------
        t0 = time.perf_counter()
        audit_detour_env = mcp.call_tool(
            "audit_closure",
            {"event_id": detour_event_id},
            reason="Step 12a: Audit baseline restoration after resolving detour closure",
        )
        audit_cutoff_env = mcp.call_tool(
            "audit_closure",
            {"event_id": cutoff_event_id},
            reason="Step 12b: Audit baseline restoration after resolving cut-off closure",
        )
        aud_res = audit_cutoff_env["result"]
        returned_ok = bool(aud_res["returned_to_baseline"])
        steps.append(
            {
                "step": 12,
                "title": "Audit Resulting Graph State Against Baseline Fingerprints",
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "summary": (
                    f"Audit confirmed returned_to_baseline={aud_res['returned_to_baseline']}, "
                    f"other_active_closures={aud_res['other_active_closures']}, "
                    f"discrepancies={aud_res['discrepancies']}."
                ),
                "data": {
                    "detour_audit": audit_detour_env["result"],
                    "cutoff_audit": aud_res,
                },
            }
        )

        total_elapsed_s = round(time.perf_counter() - overall_start, 3)
        return {
            "status": "completed",
            "step_count": len(steps),
            "total_elapsed_seconds": total_elapsed_s,
            "under_three_minutes": total_elapsed_s < 180.0,
            "initial_reset": reset_info,
            "targets": targets,
            "steps": steps,
            "mcp_handoff_count": len(mcp.handoffs),
            "mcp_handoffs": list(mcp.handoffs),
            "back_to_baseline": returned_ok,
            "disclaimer": SAFETY_DISCLAIMER,
        }
    finally:
        mcp.close()
