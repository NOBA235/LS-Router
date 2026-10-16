"""The development CLI, exercised against the in-memory graph."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data"))

import scenarios as SC
from app.graph import cli
from fake_graph import FakeGraph


def run(argv, ds=None):
    lines = []
    code = cli.main(argv, graph=FakeGraph(ds or SC.scenario_a()), out=lines.append)
    return code, "\n".join(lines)


def test_ids_lists_what_a_user_needs_to_call_the_tools():
    code, text = run(["ids"])
    assert code == 0 and "dimapur=D" in text and "kohima=K" in text and "s2 |" in text and "BR1 |" in text and "HJ |" in text and "Alpha" in text


def test_tool_runs_and_prints_the_envelope_as_json():
    code, text = run(["reroute", '{"from_place_id": "D", "to_place_id": "K"}'])
    env = json.loads(text)
    assert code == 0 and env["tool"] == "reroute" and env["result"]["route"]["total_travel_time_min"] == 40 and env["cypher"]


def test_errors_are_reported_not_raised():
    code, text = run(["reroute", '{"from_place_id": "D", "to_place_id": "nowhere"}'])
    assert code == 2 and json.loads(text)["error"]["code"] == "invalid_input"
    code, text = run(["reroute", "[1]"])
    assert code == 2 and json.loads(text)["error"]["code"] == "bad_arguments"
    code, text = run(["reroute", '{"wrong": 1}'])
    assert code == 2 and json.loads(text)["error"]["code"] == "bad_arguments"
