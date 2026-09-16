from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from conftest import ServiceFactory, snapshot
from wms_mcp.domain import (
    Actor,
    ForbiddenFieldError,
    InvalidValueError,
    TransitionError,
    WriteDisabledError,
    WriteMode,
    WriteRejectedError,
)


def _order(path: Path, order_id: int) -> sqlite3.Row:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert row is not None
    return row  # type: ignore[no-any-return]


def _last_audit(path: Path) -> sqlite3.Row:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    return row  # type: ignore[no-any-return]


# ------------------------------------------------------------------ write modes


def test_write_mode_off_rejects_every_write(make_service: ServiceFactory, db_path: Path) -> None:
    service = make_service(WriteMode.OFF)
    before = snapshot(db_path)
    with pytest.raises(WriteDisabledError, match="WMS_WRITE_MODE=off"):
        service.set_order_status("10432", "stock_issue", "short on gloves")
    with pytest.raises(WriteDisabledError):
        service.add_order_note("10432", "hello")
    with pytest.raises(WriteDisabledError):
        service._write_order_fields(10432, {"status": "stock_issue"})
    assert snapshot(db_path) == before


def test_dry_run_returns_the_diff_and_writes_nothing(
    make_service: ServiceFactory, db_path: Path
) -> None:
    before = snapshot(db_path)
    result = make_service(WriteMode.DRY_RUN).set_order_status(
        "10432", "stock_issue", "Only 1 unit of TDW-GLV-L at A-02-01; order needs 3"
    )
    assert result["outcome"] == "dry_run"
    assert result["write_performed"] is False
    assert result["changes"]["status"] == {"before": "picking", "after": "stock_issue"}
    assert result["changes"]["status_reason"]["after"]["trust"] == "untrusted"
    assert snapshot(db_path) == before


def test_dry_run_still_runs_the_database_triggers(
    make_service: ServiceFactory, db_path: Path
) -> None:
    before = snapshot(db_path)
    # Bypasses the Python reason check on purpose: the trigger must still refuse it.
    with pytest.raises(WriteRejectedError, match="non-empty status_reason"):
        make_service(WriteMode.DRY_RUN)._write_order_fields(
            10432, {"status": "stock_issue", "status_reason": "   "}
        )
    assert snapshot(db_path) == before


def test_write_mode_on_applies_and_audits_as_agent(
    make_service: ServiceFactory, db_path: Path
) -> None:
    result = make_service(WriteMode.ON).set_order_status(
        "10432", "stock_issue", "Only 1 unit of TDW-GLV-L at A-02-01; order needs 3"
    )
    assert result["outcome"] == "applied"
    row = _order(db_path, 10432)
    assert (row["status"], row["write_actor"]) == ("stock_issue", None)
    audit = _last_audit(db_path)
    assert audit["id"] == result["audit_id"]
    assert (audit["actor"], audit["op"], audit["row_id"]) == ("agent", "UPDATE", 10432)
    assert json.loads(audit["before_json"])["status"] == "picking"
    assert json.loads(audit["after_json"])["status"] == "stock_issue"


def test_spec_example_mark_10432_as_stock_issue_and_note_the_reason(
    make_service: ServiceFactory, db_path: Path
) -> None:
    service = make_service(WriteMode.ON)
    service.set_order_status("10432", "stock_issue", "Gloves size L short at A-02-01")
    service.add_order_note("10432", "Asked purchasing for the next TDW-GLV-L receipt.")
    order = service.get_order("10432")["order"]
    assert order["status"] == "stock_issue"
    assert [n["claimed_by"] for n in order["notes"]] == ["agent"]
    history = service.get_audit_log("10432")["entries"]
    assert [e["actor"] for e in history] == ["agent", "agent", "script"]


# ----------------------------------------------------------- closed vocabulary


@pytest.mark.parametrize("status", ["lost_in_transit", "Stock_Issue", "stock issue"])
def test_invented_status_is_rejected_not_corrected(
    make_service: ServiceFactory, db_path: Path, status: str
) -> None:
    before = snapshot(db_path)
    with pytest.raises(InvalidValueError, match="valid statuses are"):
        make_service(WriteMode.ON).set_order_status("10432", status, "reason")
    assert snapshot(db_path) == before


# ------------------------------------------------------------ field allowlist


@pytest.mark.parametrize(
    "changes",
    [
        {"recipient_name": "Someone Else"},
        {"total_price_cents": "0"},
        {"status": "stock_issue", "status_reason": "x", "ship_street": "Other Street 1"},
    ],
)
def test_fields_outside_the_allowlist_are_rejected_in_python(
    make_service: ServiceFactory, db_path: Path, changes: dict[str, str]
) -> None:
    before = snapshot(db_path)
    with pytest.raises(ForbiddenFieldError, match="not writable"):
        make_service(WriteMode.ON)._write_order_fields(10432, changes)
    assert snapshot(db_path) == before


# --------------------------------------------------------------- transitions


def test_agent_cannot_mark_an_order_shipped(make_service: ServiceFactory) -> None:
    with pytest.raises(TransitionError, match="confirmed by a scan"):
        make_service(WriteMode.ON).set_order_status("10437", "shipped", "looks ready")


@pytest.mark.parametrize(("order_ref", "status"), [("10426", "cancelled"), ("10437", "delivered")])
def test_agent_cannot_cancel_or_deliver(
    make_service: ServiceFactory, db_path: Path, order_ref: str, status: str
) -> None:
    before = snapshot(db_path)
    with pytest.raises(TransitionError, match="an agent cannot set status"):
        make_service(WriteMode.ON).set_order_status(order_ref, status, "customer asked")
    assert snapshot(db_path) == before


def test_low_level_write_still_refuses_forbidden_targets_for_the_agent(
    make_service: ServiceFactory, db_path: Path
) -> None:
    before = snapshot(db_path)
    with pytest.raises(TransitionError, match="an agent cannot set status 'shipped'"):
        make_service(WriteMode.ON)._write_order_fields(
            10412, {"status": "shipped", "status_reason": "looks ready"}
        )
    assert snapshot(db_path) == before


def test_low_level_write_is_not_part_of_the_public_surface() -> None:
    from wms_mcp.service import WmsService

    assert not hasattr(WmsService, "write_order_fields")


def test_repeating_the_current_reason_is_rejected(make_service: ServiceFactory) -> None:
    with pytest.raises(InvalidValueError, match="repeats the order's current status_reason"):
        make_service(WriteMode.ON).set_order_status(
            "10420",
            "pending",
            "BRS-MUG-SET4 short at B-03-04 after breakage; replenishment date unknown",
        )


def test_a_script_actor_can_record_a_shipment(make_service: ServiceFactory, db_path: Path) -> None:
    result = make_service(WriteMode.ON, Actor.SCRIPT).set_order_status(
        "10437", "shipped", "Carrier scan at dock 2"
    )
    assert result["actor"] == "script"
    assert _last_audit(db_path)["actor"] == "script"


def test_invalid_transition_is_rejected(make_service: ServiceFactory) -> None:
    with pytest.raises(TransitionError, match="allowed next statuses: none"):
        make_service(WriteMode.ON).set_order_status("10401", "picking", "reopen")


def test_same_status_is_rejected(make_service: ServiceFactory) -> None:
    with pytest.raises(TransitionError, match="already"):
        make_service(WriteMode.ON).set_order_status("10432", "picking", "again")


@pytest.mark.parametrize("reason", ["", "   ", "x" * 301])
def test_reason_is_validated_not_truncated(
    make_service: ServiceFactory, db_path: Path, reason: str
) -> None:
    before = snapshot(db_path)
    with pytest.raises(InvalidValueError):
        make_service(WriteMode.ON).set_order_status("10432", "stock_issue", reason)
    assert snapshot(db_path) == before


# ----------------------------------------------------------- disambiguation


@pytest.mark.parametrize("mode", [WriteMode.ON, WriteMode.DRY_RUN])
def test_ambiguous_name_never_writes(
    make_service: ServiceFactory, db_path: Path, mode: WriteMode
) -> None:
    before = snapshot(db_path)
    # "el pedido de Martínez": four orders match, with and without the accent.
    result = make_service(mode).set_order_status("Martínez", "stock_issue", "x")
    assert result["outcome"] == "needs_clarification"
    assert result["write_performed"] is False
    assert sorted(c["order_id"] for c in result["candidates"]) == [10401, 10409, 10412, 10432]
    assert snapshot(db_path) == before


def test_ambiguous_note_target_never_writes(make_service: ServiceFactory, db_path: Path) -> None:
    before = snapshot(db_path)
    result = make_service(WriteMode.ON).add_order_note("López", "call the customer")
    assert result["outcome"] == "needs_clarification"
    assert {c["order_id"] for c in result["candidates"]} == {10420, 10428}
    assert snapshot(db_path) == before


@pytest.mark.parametrize("mode", [WriteMode.ON, WriteMode.DRY_RUN])
def test_unique_name_match_still_needs_the_order_id(
    make_service: ServiceFactory, db_path: Path, mode: WriteMode
) -> None:
    # One substring match is not proof of intent: "Soler" could be a customer
    # who is not in the WMS yet, or a different spelling of someone else.
    before = snapshot(db_path)
    service = make_service(mode)
    result = service.add_order_note("Navarro", "Customer confirmed postcode.")
    assert result["outcome"] == "needs_confirmation"
    assert result["write_performed"] is False
    assert [c["order_id"] for c in result["candidates"]] == [10423]
    status = service.set_order_status("Soler", "stock_issue", "Tent short")
    assert (status["outcome"], status["candidates"][0]["order_id"]) == ("needs_confirmation", 10412)
    assert snapshot(db_path) == before


def test_write_by_id_after_confirmation_goes_through(make_service: ServiceFactory) -> None:
    result = make_service(WriteMode.ON).add_order_note("#10423", "Customer confirmed postcode.")
    assert (result["outcome"], result["order_id"]) == ("applied", 10423)


def test_ambiguous_name_with_writes_off_is_rejected_before_lookup(
    make_service: ServiceFactory,
) -> None:
    with pytest.raises(WriteDisabledError):
        make_service(WriteMode.OFF).set_order_status("Martínez", "stock_issue", "x")


# ----------------------------------------------------------------- atomicity


def test_concurrent_writer_is_blocked_between_check_and_write(
    make_service: ServiceFactory, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script tries to cancel 10432 right after the agent has read it.

    Before the fix the read happened outside the transaction, the script's
    cancel landed, and the agent then moved a cancelled (final) order to
    stock_issue. Now the read runs inside BEGIN IMMEDIATE, so the script is
    locked out until the agent's write is committed.
    """
    service = make_service(WriteMode.ON)
    original = service._resolve
    attempts: list[str] = []

    def resolve_then_race(conn: sqlite3.Connection, order_ref: str) -> object:
        resolved = original(conn, order_ref)
        with closing(sqlite3.connect(db_path, timeout=0, isolation_level=None)) as other:
            try:
                other.execute(
                    "UPDATE orders SET status = 'cancelled', status_reason = 'race', "
                    "write_actor = 'script' WHERE id = 10432"
                )
                attempts.append("applied")
            except sqlite3.OperationalError as exc:
                attempts.append(str(exc))
        return resolved

    monkeypatch.setattr(service, "_resolve", resolve_then_race)
    result = service.set_order_status("10432", "stock_issue", "Gloves short")
    assert result["outcome"] == "applied"
    assert attempts == ["database is locked"]
    row = _order(db_path, 10432)
    assert row["status"] == "stock_issue"


def test_conditional_update_refuses_a_stale_row(
    make_service: ServiceFactory, db_path: Path, raw: sqlite3.Connection
) -> None:
    """Second line of defence: the UPDATE only matches the status that was checked."""
    stale = _order(db_path, 10432)
    raw.execute(
        "UPDATE orders SET status = 'cancelled', status_reason = 'race', write_actor = 'script' "
        "WHERE id = 10432"
    )
    service = make_service(WriteMode.ON)
    with (
        pytest.raises(WriteRejectedError, match="changed while this write was being prepared"),
        service._write_transaction() as conn,
    ):
        service._apply(conn, stale, {"status": "stock_issue", "status_reason": "late"})
    assert _order(db_path, 10432)["status"] == "cancelled"


def test_notes_are_appended_and_history_is_kept(
    make_service: ServiceFactory, db_path: Path
) -> None:
    original = _order(db_path, 10434)["notes"]
    make_service(WriteMode.ON).add_order_note("10434", "Neighbour delivery is not supported.")
    notes = _order(db_path, 10434)["notes"]
    assert notes.startswith(original)
    assert len(notes.splitlines()) == 2
