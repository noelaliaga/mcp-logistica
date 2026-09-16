from __future__ import annotations

import json
from pathlib import Path

import pytest

from wms_mcp.calllog import CallLog, CallRecord, format_report, main, read_records, summarize


def _record(tool: str, outcome: str, ms: float, kind: str | None = None) -> CallRecord:
    return CallRecord(tool, outcome, kind, ms, "on", ["order_ref"])


def test_summary_counts_outcomes_errors_and_latency(tmp_path: Path) -> None:
    log = CallLog(tmp_path / "nested" / "calls.jsonl")
    for rec in (
        _record("get_order", "ok", 2.0),
        _record("get_order", "needs_clarification", 4.0),
        _record("get_order", "error", 9.0, "NotFoundError"),
        _record("set_order_status", "applied", 5.0),
    ):
        log.write(rec)
    summary = summarize(read_records(log.path.read_text(encoding="utf-8").splitlines()))
    assert summary["get_order"] == {
        "calls": 3,
        "outcomes": {"error": 1, "needs_clarification": 1, "ok": 1},
        "error_rate": 0.333,
        "p50_ms": 4.0,
        "max_ms": 9.0,
    }
    assert summary["set_order_status"]["error_rate"] == 0.0
    assert "needs_clarification=1" in format_report(summary)


def test_report_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "calls.jsonl"
    CallLog(log).write(_record("get_stock", "not_found", 1.5))
    assert main([str(log), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["get_stock"]["outcomes"] == {"not_found": 1}
    assert main([str(tmp_path / "missing.jsonl")]) == 1
