"""Scenario-based evaluation of the agent loop against a freshly seeded WMS.

Each scenario is a user request plus model-agnostic checks on what happened:
which tools were called, which outcomes came back, whether the database
changed, and what the final answer mentions.

    wms-eval                          # offline: replays each scenario's script
    wms-eval --model gpt-4o-mini      # live: needs `pip install litellm` and your keys

Offline runs check the server, the loop and the graders. They say nothing about
how well a real model behaves; that is what the live mode is for, and no live
results are shipped with this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wms_mcp.agent.loop import Trace, run_agent
from wms_mcp.agent.models import AssistantTurn, ChatModel, LiteLLMModel, ScriptedModel, ToolCall
from wms_mcp.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from wms_mcp.calllog import format_report, read_records, summarize
from wms_mcp.db import create_database
from wms_mcp.seed import seed

DEFAULT_SCENARIOS = Path("evals/scenarios.toml")
WRITE_TOOLS = frozenset({"set_order_status", "add_order_note"})


@dataclass(frozen=True)
class Expect:
    no_write: bool = False
    status_after: dict[str, str] = field(default_factory=dict)
    audit_actors_added: list[str] | None = None
    must_call: list[str] = field(default_factory=list)
    must_not_call: list[str] = field(default_factory=list)
    outcomes_include: list[str] = field(default_factory=list)
    tool_error_contains: list[str] = field(default_factory=list)
    final_mentions_all: list[str] = field(default_factory=list)
    final_mentions_any: list[str] = field(default_factory=list)
    final_not_mentions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    user: str
    write_mode: str
    expect: Expect
    script: list[AssistantTurn]


def _turns(raw: list[dict[str, Any]], scenario_id: str) -> list[AssistantTurn]:
    turns = []
    for i, step in enumerate(raw):
        if "tool" in step:
            call = ToolCall(f"{scenario_id}-{i}", str(step["tool"]), dict(step.get("args", {})))
            turns.append(AssistantTurn(tool_calls=[call]))
        elif "say" in step:
            turns.append(AssistantTurn(text=str(step["say"])))
        else:
            raise ValueError(f"{scenario_id}: script step {i} needs 'tool' or 'say'")
    return turns


def load_scenarios(path: Path) -> list[Scenario]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    scenarios = []
    for raw in data["scenario"]:
        unknown = set(raw.get("expect", {})) - set(Expect.__dataclass_fields__)
        if unknown:
            raise ValueError(f"{raw['id']}: unknown expectation(s) {sorted(unknown)}")
        scenarios.append(
            Scenario(
                id=raw["id"],
                description=raw.get("description", ""),
                user=raw["user"],
                write_mode=raw.get("write_mode", "on"),
                expect=Expect(**raw.get("expect", {})),
                script=_turns(raw.get("script", []), raw["id"]),
            )
        )
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario ids must be unique")
    return scenarios


# ------------------------------------------------------------------ database


@dataclass(frozen=True)
class DbState:
    orders: list[tuple[Any, ...]]
    statuses: dict[int, str]
    audit_actors: list[str]


def read_state(db: Path) -> DbState:
    with closing(sqlite3.connect(db)) as conn:
        orders = [tuple(r) for r in conn.execute("SELECT * FROM orders ORDER BY id")]
        statuses = dict(conn.execute("SELECT id, status FROM orders").fetchall())
        actors = [r[0] for r in conn.execute("SELECT actor FROM audit_log ORDER BY id")]
    return DbState(orders, statuses, actors)


def seeded_db(directory: Path) -> Path:
    path = directory / "wms.sqlite"
    with closing(create_database(path)) as conn:
        seed(conn, datetime.now(UTC))
    return path


# ------------------------------------------------------------------- grading


def grade(scenario: Scenario, trace: Trace, before: DbState, after: DbState) -> list[str]:
    """Return the failed checks; an empty list means the scenario passed."""
    e = scenario.expect
    failures: list[str] = []
    called = [s.tool for s in trace.steps]
    outcomes = {s.outcome for s in trace.steps}
    errors = " | ".join(s.text for s in trace.steps if s.is_error)
    final = trace.final_text.casefold()

    if trace.stopped != "answered":
        failures.append(f"agent did not answer ({trace.stopped})")
    if e.no_write and (after.orders != before.orders or after.audit_actors != before.audit_actors):
        failures.append("database changed but no write was expected")
    for order_id, status in e.status_after.items():
        if after.statuses.get(int(order_id)) != status:
            got = after.statuses.get(int(order_id))
            failures.append(f"order {order_id} is {got!r}, expected {status!r}")
    if e.audit_actors_added is not None:
        added = after.audit_actors[len(before.audit_actors) :]
        if added != e.audit_actors_added:
            failures.append(f"audit rows added by {added}, expected {e.audit_actors_added}")
    failures += [f"tool {t!r} was not called" for t in e.must_call if t not in called]
    failures += [f"tool {t!r} must not be called" for t in e.must_not_call if t in called]
    failures += [f"no step returned outcome {o!r}" for o in e.outcomes_include if o not in outcomes]
    failures += [f"no tool error mentions {m!r}" for m in e.tool_error_contains if m not in errors]
    failures += [
        f"final answer does not mention {m!r}"
        for m in e.final_mentions_all
        if m.casefold() not in final
    ]
    if e.final_mentions_any and not any(m.casefold() in final for m in e.final_mentions_any):
        failures.append(f"final answer mentions none of {e.final_mentions_any}")
    failures += [
        f"final answer mentions {m!r}" for m in e.final_not_mentions if m.casefold() in final
    ]
    return failures


# ------------------------------------------------------------------- running


@dataclass
class Result:
    scenario: Scenario
    trace: Trace
    failures: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures


ModelFactory = Callable[[Scenario], ChatModel]


async def run_scenario(
    scenario: Scenario, model: ChatModel, workdir: Path, tool_log: Path
) -> Result:
    db = seeded_db(workdir)
    before = read_state(db)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "wms_mcp.server"],
        env={
            **os.environ,
            "WMS_DB_PATH": str(db),
            "WMS_WRITE_MODE": scenario.write_mode,
            "WMS_TOOL_LOG": str(tool_log),
        },
    )
    with (workdir / "server.stderr").open("w", encoding="utf-8") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            trace = await run_agent(
                model,
                session,
                scenario.user,
                system_prompt=SYSTEM_PROMPT,
                prompt_version=PROMPT_VERSION,
            )
    return Result(scenario, trace, grade(scenario, trace, before, read_state(db)))


def run_all(
    scenarios: Sequence[Scenario], make_model: ModelFactory, out_dir: Path | None = None
) -> tuple[list[Result], list[dict[str, Any]]]:
    results = []
    with tempfile.TemporaryDirectory(prefix="wms-eval-") as tmp:
        tool_log = (out_dir or Path(tmp)) / "tool_calls.jsonl"
        tool_log.parent.mkdir(parents=True, exist_ok=True)
        tool_log.unlink(missing_ok=True)
        for scenario in scenarios:
            workdir = Path(tmp) / scenario.id
            workdir.mkdir()
            model = make_model(scenario)
            results.append(anyio.run(run_scenario, scenario, model, workdir, tool_log))
        records = read_records(tool_log.read_text(encoding="utf-8").splitlines())
    if out_dir is not None:
        with (out_dir / "traces.jsonl").open("w", encoding="utf-8") as fh:
            for r in results:
                row = {"scenario": r.scenario.id, "passed": r.passed, "failures": r.failures}
                fh.write(json.dumps({**row, "trace": r.trace.to_json()}, ensure_ascii=False))
                fh.write("\n")
    return results, records


def scripted_factory(scenario: Scenario) -> ChatModel:
    if not scenario.script:
        raise ValueError(f"{scenario.id}: no script for offline mode")
    return ScriptedModel(scenario.script)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the WMS agent on scenarios.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument(
        "--model",
        default="scripted",
        help="'scripted' (offline, default) or a LiteLLM model id (live, uses your keys)",
    )
    parser.add_argument("--only", action="append", default=[], help="run only this scenario id")
    parser.add_argument("--out", type=Path, default=None, help="write traces and tool log here")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    if args.only:
        scenarios = [s for s in scenarios if s.id in set(args.only)]
    model_id: str = args.model
    if model_id == "scripted":
        factory: ModelFactory = scripted_factory
    else:
        print(f"live run with {model_id}: this calls a paid API with your keys", file=sys.stderr)

        def factory(_: Scenario) -> ChatModel:
            return LiteLLMModel(model_id)

    results, records = run_all(scenarios, factory, args.out)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.scenario.id}")
        for failure in r.failures:
            print(f"      - {failure}")
    passed = sum(r.passed for r in results)
    print(
        f"\n{passed}/{len(results)} scenarios passed  (model={model_id}, prompt={PROMPT_VERSION})"
    )
    print("\nTool calls during the run:")
    print(format_report(summarize(records)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
