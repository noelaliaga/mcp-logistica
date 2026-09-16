"""Offline agent evaluation: scripted trajectories against the real stdio server.

No model API is called. These tests prove that the loop, the server and the
graders work together, and that the graders fail when they should.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from wms_mcp.agent.evals import (
    DEFAULT_SCENARIOS,
    load_scenarios,
    main,
    run_all,
    scripted_factory,
)
from wms_mcp.agent.models import AssistantTurn, LiteLLMModel, ScriptedModel, ToolCall

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = load_scenarios(REPO / DEFAULT_SCENARIOS)


def test_scenario_file_is_well_formed() -> None:
    assert len(SCENARIOS) >= 10
    assert all(s.script for s in SCENARIOS)
    assert {s.write_mode for s in SCENARIOS} == {"off", "dry_run", "on"}


def test_every_scripted_scenario_passes(tmp_path: Path) -> None:
    results, records = run_all(SCENARIOS, scripted_factory, tmp_path)
    failed = {r.scenario.id: r.failures for r in results if not r.passed}
    assert failed == {}
    # The tool-call log was written by the server processes and covers every step.
    assert len(records) == sum(len(r.trace.steps) for r in results)
    traces = (tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(traces) == len(SCENARIOS)
    assert json.loads(traces[0])["trace"]["prompt_version"].startswith("wms-assistant/")


def test_graders_catch_an_agent_that_picks_a_candidate_itself(tmp_path: Path) -> None:
    """Negative control: a model that 'helpfully' chooses 10432 must fail the scenario."""
    ambiguous = next(s for s in SCENARIOS if s.id == "ambiguous_name_write")
    guessing = dataclasses.replace(
        ambiguous,
        script=[
            AssistantTurn(tool_calls=[ToolCall("1", "find_orders", {"query": "Martínez"})]),
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        "2",
                        "set_order_status",
                        {"order_ref": "10432", "status": "stock_issue", "reason": "Gloves"},
                    )
                ]
            ),
            AssistantTurn(text="Done: the most recent Martínez order, 10432, is now stock_issue."),
        ],
    )
    [result], _ = run_all([guessing], scripted_factory)
    assert not result.passed
    assert "database changed but no write was expected" in result.failures
    assert any("needs_clarification" in f for f in result.failures)


def test_graders_catch_a_run_that_never_answers(tmp_path: Path) -> None:
    looping = dataclasses.replace(
        SCENARIOS[0],
        script=[
            AssistantTurn(tool_calls=[ToolCall(str(i), "list_stalled_orders", {})])
            for i in range(6)
        ],
    )
    [result], _ = run_all([looping], scripted_factory)
    assert "agent did not answer (max_steps)" in result.failures


def test_cli_offline_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--scenarios", str(REPO / DEFAULT_SCENARIOS), "--only", "agent_cannot_ship"])
    out = capsys.readouterr().out
    assert code == 0
    assert "PASS  agent_cannot_ship" in out
    assert "1/1 scenarios passed" in out


def test_scripted_model_stops_when_the_script_ends() -> None:
    model = ScriptedModel([AssistantTurn(text="hi")])
    assert model.complete([], []).text == "hi"
    with pytest.raises(RuntimeError, match="no turns left"):
        model.complete([], [])


def test_litellm_adapter_parses_openai_style_tool_calls() -> None:
    """Response parsing only. A fake completion stands in for LiteLLM; no network."""
    seen: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_order",
                                    "arguments": '{"order_ref": "10432"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }

    model = LiteLLMModel("provider/some-model", completion=fake_completion)
    tools = [{"type": "function", "function": {"name": "get_order", "parameters": {}}}]
    turn = model.complete([{"role": "user", "content": "hi"}], tools)
    assert turn.tool_calls == [ToolCall("call_1", "get_order", {"order_ref": "10432"})]
    assert (seen["model"], seen["tools"], seen["temperature"]) == (
        "provider/some-model",
        tools,
        0.0,
    )
    assert turn.as_message()["tool_calls"][0]["function"]["name"] == "get_order"


def test_unknown_expectation_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[[scenario]]\nid = "x"\nuser = "u"\n[scenario.expect]\nno_writes = true\n')
    with pytest.raises(ValueError, match="unknown expectation"):
        load_scenarios(bad)
