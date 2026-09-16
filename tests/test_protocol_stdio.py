"""End-to-end protocol test: spawn the server and speak raw JSON-RPC 2.0 over stdio.

No MCP client library is used, so this also documents the wire format:
initialize -> notifications/initialized -> tools/list -> tools/call.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from wms_mcp.domain import OrderStatus

TIMEOUT_S = 20
EXPECTED_TOOLS = {
    "find_orders",
    "get_order",
    "list_stalled_orders",
    "get_stock",
    "get_audit_log",
    "set_order_status",
    "add_order_note",
}


class StdioServer:
    def __init__(self, db_path: Path, write_mode: str, extra_env: dict[str, str]) -> None:
        env = {
            **os.environ,
            "WMS_DB_PATH": str(db_path),
            "WMS_WRITE_MODE": write_mode,
            **extra_env,
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "wms_mcp.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        while True:
            message = json.loads(self._lines.get(timeout=TIMEOUT_S))
            if message.get("id") == msg_id:
                return message  # type: ignore[no-any-return]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in response, response
        return response["result"]  # type: ignore[no-any-return]

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()


def _open(
    db_path: Path, write_mode: str, extra_env: dict[str, str] | None = None
) -> Iterator[StdioServer]:
    server = StdioServer(db_path, write_mode, extra_env or {})
    try:
        init = server.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest-raw-client", "version": "0"},
            },
        )
        assert init["result"]["serverInfo"]["name"] == "wms"
        server.notify("notifications/initialized")
        yield server
    finally:
        server.close()


@pytest.fixture
def server_off(live_db_path: Path) -> Iterator[StdioServer]:
    yield from _open(live_db_path, "off")


@pytest.fixture
def server_on(live_db_path: Path) -> Iterator[StdioServer]:
    yield from _open(live_db_path, "on")


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


def _status(db_path: Path, order_id: int) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    return str(row[0])


def test_initialize_list_and_read_call(server_off: StdioServer) -> None:
    tools = {t["name"]: t for t in server_off.request("tools/list")["result"]["tools"]}
    assert set(tools) == EXPECTED_TOOLS

    status_tool = tools["set_order_status"]
    schema = status_tool["inputSchema"]
    assert set(schema["properties"]) == {"order_ref", "status", "reason"}
    assert schema["properties"]["status"]["enum"] == [s.value for s in OrderStatus]
    assert schema["additionalProperties"] is False
    assert status_tool["annotations"]["readOnlyHint"] is False
    assert tools["get_stock"]["annotations"]["readOnlyHint"] is True

    forbidden_words = ("quantity", "price", "recipient", "street", "address")
    for tool in tools.values():
        for prop in tool["inputSchema"]["properties"]:
            assert not any(word in prop for word in forbidden_words), (tool["name"], prop)

    since = tools["list_stalled_orders"]["inputSchema"]["properties"]["since"]
    assert since["enum"] == ["created", "status_change"]

    stalled = _payload(server_off.call("list_stalled_orders", {}))
    assert [o["order_id"] for o in stalled["orders"]] == [10420, 10409, 10423, 10412, 10417, 10426]


def test_write_is_rejected_when_write_mode_is_off(
    server_off: StdioServer, live_db_path: Path
) -> None:
    result = server_off.call(
        "set_order_status", {"order_ref": "10432", "status": "stock_issue", "reason": "short"}
    )
    assert result["isError"] is True
    assert "WMS_WRITE_MODE=off" in result["content"][0]["text"]
    assert _status(live_db_path, 10432) == "picking"


def test_invented_status_is_rejected_over_the_wire(
    server_on: StdioServer, live_db_path: Path
) -> None:
    result = server_on.call(
        "set_order_status", {"order_ref": "10432", "status": "lost_in_transit", "reason": "x"}
    )
    assert result["isError"] is True
    assert _status(live_db_path, 10432) == "picking"


def test_unknown_arguments_are_rejected_not_ignored(
    server_on: StdioServer, live_db_path: Path
) -> None:
    result = server_on.call(
        "add_order_note",
        {"order_ref": "10432", "note": "hi", "total_price_cents": 0},
    )
    assert result["isError"] is True
    with closing(sqlite3.connect(live_db_path)) as conn:
        assert conn.execute("SELECT notes FROM orders WHERE id = 10432").fetchone()[0] == ""


def test_ambiguity_and_audited_write_over_the_wire(
    server_on: StdioServer, live_db_path: Path
) -> None:
    ambiguous = _payload(
        server_on.call(
            "set_order_status",
            {"order_ref": "Martínez", "status": "stock_issue", "reason": "short"},
        )
    )
    assert ambiguous["outcome"] == "needs_clarification"
    unique = _payload(
        server_on.call("add_order_note", {"order_ref": "Navarro", "note": "call back"})
    )
    assert unique["outcome"] == "needs_confirmation"

    applied = _payload(
        server_on.call(
            "set_order_status",
            {"order_ref": "10432", "status": "stock_issue", "reason": "TDW-GLV-L short"},
        )
    )
    assert applied["outcome"] == "applied"
    with closing(sqlite3.connect(live_db_path)) as conn:
        actor = conn.execute("SELECT actor FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert actor == "agent"


@pytest.mark.parametrize(
    ("env_mode", "use_missing_db", "message"),
    [("yes", False, "invalid WMS_WRITE_MODE"), ("off", True, "database not found")],
)
def test_bad_configuration_fails_at_startup(
    live_db_path: Path, tmp_path: Path, env_mode: str, use_missing_db: bool, message: str
) -> None:
    db = tmp_path / "missing.sqlite" if use_missing_db else live_db_path
    env = {**os.environ, "WMS_DB_PATH": str(db), "WMS_WRITE_MODE": env_mode}
    proc = subprocess.run(
        [sys.executable, "-m", "wms_mcp.server"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 2
    assert message in proc.stderr


def test_unexpected_errors_do_not_leak_internals(live_db_path: Path) -> None:
    for server in _open(live_db_path, "off"):
        live_db_path.unlink()  # the file disappears under a running server
        result = server.call("get_order", {"order_ref": "10432"})
        text = result["content"][0]["text"]
        assert result["isError"] is True
        assert "internal error in the WMS server" in text
        assert str(live_db_path.parent) not in text
        assert "sqlite" not in text.lower()
        # A WmsError is still returned verbatim.
        refused = server.call(
            "set_order_status", {"order_ref": "10432", "status": "stock_issue", "reason": "x"}
        )
        assert "WMS_WRITE_MODE=off" in refused["content"][0]["text"]


def test_tool_calls_are_logged_without_argument_values(live_db_path: Path, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "calls.jsonl"
    for server in _open(live_db_path, "dry_run", {"WMS_TOOL_LOG": str(log)}):
        server.call("get_order", {"order_ref": "Martínez"})
        server.call(
            "set_order_status", {"order_ref": "10432", "status": "stock_issue", "reason": "short"}
        )
        server.call("set_order_status", {"order_ref": "10432", "status": "nope", "reason": "x"})
        server.call("add_order_note", {"order_ref": "10432", "note": "n", "total_price_cents": 0})
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["tool"], r["outcome"], r["error_kind"]) for r in records] == [
        ("get_order", "needs_clarification", None),
        ("set_order_status", "dry_run", None),
        ("set_order_status", "error", "InvalidValueError"),
        ("add_order_note", "error", "InvalidArguments"),
    ]
    assert all(r["write_mode"] == "dry_run" and r["latency_ms"] >= 0 for r in records)
    assert records[1]["arg_keys"] == ["order_ref", "reason", "status"]
    assert "Martínez" not in log.read_text(encoding="utf-8")
