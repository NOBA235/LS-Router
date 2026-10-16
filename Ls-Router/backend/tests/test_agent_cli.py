"""Tests for the Phase 3 agent CLI (`python -m app.agent.cli`)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import scenarios as SC  # noqa: E402
from app.agent import cli  # noqa: E402
from fake_graph import FakeGraph  # noqa: E402


def run_cli(argv: list[str], ds=None) -> tuple[int, str]:
    lines: list[str] = []
    graph = None if "--fixture" in argv or "--dataset" in argv else FakeGraph(ds or SC.scenario_a())
    code = cli.main(argv, graph=graph, out=lines.append)
    return code, "\n".join(lines)


def test_cli_ambiguous_prompt_surfaces_candidates():
    code, text = run_cli(["Landslide near NH29 kilometre 42.", "--format", "json"])
    assert code == 0
    data = json.loads(text)
    assert data["status"] == "clarification_required"
    assert [c["segment_id"] for c in data["resolution"]["candidate_segments"]] == ["s3", "s2", "s1"]


def test_cli_confirmed_segment_produces_complete_report():
    code, text = run_cli(["Landslide near NH29 kilometre 42.", "--target-segment", "s3", "--format", "json"])
    assert code == 0
    data = json.loads(text)
    assert data["status"] == "completed"
    assert data["closure"]["event"]["blocks_segments"] == ["s3"]
    assert data["affected_settlements"]["newly_unreachable"]["count"] == 3
    assert len(data["draft_alerts"]["alerts"]) == 3


def test_cli_fixture_demo_is_reproducible():
    code1, text1 = run_cli(["--fixture", "--demo", "--format", "json"])
    code2, text2 = run_cli(["--fixture", "--demo", "--format", "json"])
    assert code1 == 0 and code2 == 0
    d1, d2 = json.loads(text1), json.loads(text2)
    assert len(d1["steps"]) == 5 and len(d2["steps"]) == 5
    assert d1["steps"][0]["report"]["status"] == "clarification_required"
    assert d1["steps"][1]["report"]["routes"][0]["additional_travel_time_min"] == 20.0
    assert d1["steps"][2]["report"]["closure_audit"]["returned_to_baseline"] is True
    assert d1["steps"][3]["report"]["affected_settlements"]["newly_unreachable"]["count"] == 3
    assert d1["steps"][4]["report"]["closure_audit"]["returned_to_baseline"] is True

    # Markdown demo output
    code_md, text_md = run_cli(["--fixture", "--demo"])
    assert code_md == 0
    assert "STEP 1:" in text_md and "Planning aid, not emergency dispatch." in text_md


def test_cli_errors_return_exit_code_2():
    code, text = run_cli(["Landslide", "--target-segment", "unknown_segment"])
    assert code == 2
    assert json.loads(text)["error"]["code"] == "invalid_input"

    code_missing, text_missing = run_cli(["--dataset", "nonexistent_file.json", "Landslide"])
    assert code_missing == 2
    assert json.loads(text_missing)["error"]["code"] == "file_not_found"

