"""Structured record of every tool call, and a small report over those records.

One JSON line per call: tool, outcome (ok, needs_clarification, dry_run,
applied, error...), error kind, latency and the argument *names*. Argument
values are not logged because they can contain customer names.

    WMS_TOOL_LOG=logs/tool_calls.jsonl make serve
    wms-report logs/tool_calls.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any


@dataclass(frozen=True)
class CallRecord:
    tool: str
    outcome: str
    error_kind: str | None
    latency_ms: float
    write_mode: str
    arg_keys: list[str]
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds"))


class CallLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: CallRecord) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def read_records(lines: Iterable[str]) -> list[dict[str, Any]]:
    records = []
    for line in lines:
        if line.strip():
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per tool: call count, outcome counts, error rate, median and max latency."""
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_tool[str(rec["tool"])].append(rec)
    summary: dict[str, dict[str, Any]] = {}
    for tool in sorted(by_tool):
        recs = by_tool[tool]
        latencies = [float(r["latency_ms"]) for r in recs]
        outcomes = Counter(str(r["outcome"]) for r in recs)
        summary[tool] = {
            "calls": len(recs),
            "outcomes": dict(sorted(outcomes.items())),
            "error_rate": round(outcomes.get("error", 0) / len(recs), 3),
            "p50_ms": round(median(latencies), 2),
            "max_ms": round(max(latencies), 2),
        }
    return summary


def format_report(summary: dict[str, dict[str, Any]]) -> str:
    header = f"{'tool':<22}{'calls':>6}{'err%':>7}{'p50 ms':>9}{'max ms':>9}  outcomes"
    rows = [header, "-" * len(header)]
    for tool, s in summary.items():
        outcomes = ", ".join(f"{k}={v}" for k, v in s["outcomes"].items())
        rows.append(
            f"{tool:<22}{s['calls']:>6}{s['error_rate'] * 100:>6.1f}%"
            f"{s['p50_ms']:>9.2f}{s['max_ms']:>9.2f}  {outcomes}"
        )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize a WMS_TOOL_LOG file.")
    parser.add_argument("log", type=Path)
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    log: Path = args.log
    if not log.is_file():
        print(f"no tool-call log at {log}", file=sys.stderr)
        return 1
    summary = summarize(read_records(log.read_text(encoding="utf-8").splitlines()))
    print(json.dumps(summary, indent=2) if args.json else format_report(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
