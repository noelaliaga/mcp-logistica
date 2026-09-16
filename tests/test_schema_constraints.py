"""Database-level rules, exercised with raw SQL and no application code.

If these pass, the rules hold for any writer: a script, a notebook, a future UI.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from wms_mcp.domain import WRITABLE_ORDER_FIELDS, OrderStatus


def _last_audit(raw: sqlite3.Connection) -> sqlite3.Row:
    row = raw.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    return row  # type: ignore[no-any-return]


def test_status_check_matches_python_enum(raw: sqlite3.Connection) -> None:
    for status in OrderStatus:
        raw.execute("SAVEPOINT s")
        raw.execute(
            "UPDATE orders SET status = ?, status_reason = 'enum sync test', write_actor = 'script'"
            " WHERE id = 10434",
            (status.value,),
        )
        raw.execute("ROLLBACK TO s")
        raw.execute("RELEASE s")


@pytest.mark.parametrize("bad_status", ["lost_in_transit", "Shipped", "delivered ", ""])
def test_invented_status_is_rejected_by_check(raw: sqlite3.Connection, bad_status: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        raw.execute(
            "UPDATE orders SET status = ?, status_reason = 'x', write_actor = 'script' "
            "WHERE id = 10434",
            (bad_status,),
        )
    assert raw.execute("SELECT status FROM orders WHERE id = 10434").fetchone()[0] == "pending"


def test_every_column_outside_the_allowlist_is_blocked(raw: sqlite3.Connection) -> None:
    columns = {r["name"]: r["type"] for r in raw.execute("PRAGMA table_info(orders)")}
    protected = sorted(set(columns) - WRITABLE_ORDER_FIELDS - {"write_actor"})
    # The sensitive ones must be among them; the loop proves ALL of them abort.
    assert {"recipient_name", "ship_street", "total_price_cents", "customer_email"} <= set(
        protected
    )
    for column in protected:
        value: object = 99999 if columns[column] == "INTEGER" else "tampered"
        with pytest.raises(sqlite3.IntegrityError, match="column is not writable"):
            raw.execute(
                f"UPDATE orders SET {column} = ?, write_actor = 'ui' WHERE id = 10432",
                (value,),
            )


def test_allowlisted_columns_are_writable_with_an_actor(raw: sqlite3.Connection) -> None:
    raw.execute(
        "UPDATE orders SET status = 'stock_issue', status_reason = 'short', "
        "status_changed_at = '2026-03-02T09:00:00Z', notes = notes || 'x', write_actor = 'ui' "
        "WHERE id = 10432"
    )
    assert raw.execute("SELECT status FROM orders WHERE id = 10432").fetchone()[0] == "stock_issue"


@pytest.mark.parametrize(
    "assignment",
    ["quantity = 0", "quantity = 1", "unit_price_cents = 1", "sku = 'TDW-HLM-M'"],
)
def test_order_lines_are_immutable(raw: sqlite3.Connection, assignment: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="order_lines are immutable"):
        raw.execute(f"UPDATE order_lines SET {assignment} WHERE order_id = 10432")


def test_stock_ledger_is_append_only(raw: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("UPDATE stock_movements SET qty_delta = 100 WHERE sku = 'TDW-GLV-L'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("DELETE FROM stock_movements WHERE sku = 'TDW-GLV-L'")


def test_nothing_is_deleted(raw: sqlite3.Connection) -> None:
    for sql in (
        "DELETE FROM orders WHERE id = 10434",
        "DELETE FROM order_lines WHERE order_id = 10434",
        "DELETE FROM audit_log",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(sql)


@pytest.mark.parametrize("actor_sql", ["", ", write_actor = NULL", ", write_actor = 'admin'"])
def test_update_without_a_valid_actor_aborts(raw: sqlite3.Connection, actor_sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="write_actor is required"):
        raw.execute(
            f"UPDATE orders SET status = 'cancelled', status_reason = 'x'{actor_sql} "
            "WHERE id = 10434"
        )


@pytest.mark.parametrize("actor", [None, "robot"])
def test_insert_without_a_valid_actor_aborts(raw: sqlite3.Connection, actor: str | None) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="created_by is required"):
        raw.execute(
            "INSERT INTO stock_movements (sku, location, qty_delta, reason, created_at, "
            "created_by) VALUES ('TDW-GLV-L', 'A-02-01', 5, 'receipt', "
            "'2026-03-02T09:00:00Z', ?)",
            (actor,),
        )


def test_script_write_outside_the_server_is_audited_with_before_and_after(
    raw: sqlite3.Connection,
) -> None:
    raw.execute(
        "UPDATE orders SET status = 'cancelled', status_reason = 'Duplicate storefront retry', "
        "status_changed_at = '2026-03-02T09:00:00Z', write_actor = 'script' WHERE id = 10434"
    )
    audit = _last_audit(raw)
    assert (audit["table_name"], audit["row_id"], audit["op"], audit["actor"]) == (
        "orders",
        10434,
        "UPDATE",
        "script",
    )
    before = json.loads(audit["before_json"])
    after = json.loads(audit["after_json"])
    assert (before["status"], after["status"]) == ("pending", "cancelled")
    assert after["status_reason"] == "Duplicate storefront retry"
    # The declaration is statement-scoped: cleared once it has been recorded.
    assert raw.execute("SELECT write_actor FROM orders WHERE id = 10434").fetchone()[0] is None


def test_actor_is_not_inherited_by_the_next_write(raw: sqlite3.Connection) -> None:
    raw.execute("UPDATE orders SET notes = notes || 'a', write_actor = 'ui' WHERE id = 10440")
    with pytest.raises(sqlite3.IntegrityError, match="write_actor is required"):
        raw.execute("UPDATE orders SET notes = notes || 'b' WHERE id = 10440")


def test_status_change_requires_a_reason(raw: sqlite3.Connection) -> None:
    for reason in ("NULL", "''", "'   '"):
        with pytest.raises(sqlite3.IntegrityError, match="requires a new, non-empty status_reason"):
            raw.execute(
                f"UPDATE orders SET status = 'allocated', status_reason = {reason}, "
                "write_actor = 'script' WHERE id = 10440"
            )


def test_status_change_cannot_reuse_the_previous_reason(raw: sqlite3.Connection) -> None:
    # 10420 already has a reason (a breakage). A script that changes the status
    # without writing a new reason must not inherit that one as its justification.
    with pytest.raises(sqlite3.IntegrityError, match="requires a new, non-empty status_reason"):
        raw.execute(
            "UPDATE orders SET status = 'cancelled', write_actor = 'script' WHERE id = 10420"
        )
    row = raw.execute("SELECT status FROM orders WHERE id = 10420").fetchone()
    assert row[0] == "stock_issue"
    raw.execute(
        "UPDATE orders SET status = 'pending', status_reason = 'Supplier delivered new sets', "
        "write_actor = 'script' WHERE id = 10420"
    )


def test_notes_are_append_only(raw: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="notes are append-only"):
        raw.execute(
            "UPDATE orders SET notes = 'rewritten history', write_actor = 'ui' WHERE id = 10420"
        )


def test_audit_log_cannot_be_edited(raw: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="audit_log is append-only"):
        raw.execute("UPDATE audit_log SET actor = 'ui'")


def test_rules_hold_with_recursive_triggers_enabled(raw: sqlite3.Connection) -> None:
    raw.execute("PRAGMA recursive_triggers = ON")
    before = raw.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    raw.execute(
        "UPDATE orders SET status = 'allocated', status_reason = 'ok', write_actor = 'script' "
        "WHERE id = 10440"
    )
    assert raw.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before + 1
    assert raw.execute("SELECT write_actor FROM orders WHERE id = 10440").fetchone()[0] is None
