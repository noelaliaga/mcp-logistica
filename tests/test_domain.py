from __future__ import annotations

import pytest

from wms_mcp.domain import (
    ALLOWED_TRANSITIONS,
    InvalidValueError,
    OrderStatus,
    WriteMode,
    parse_status,
    parse_write_mode,
)


@pytest.mark.parametrize("value", ["lost_in_transit", "Shipped", "STOCK_ISSUE", " pending", ""])
def test_parse_status_rejects_instead_of_correcting(value: str) -> None:
    with pytest.raises(InvalidValueError, match="valid statuses are"):
        parse_status(value)


def test_parse_status_accepts_exact_values() -> None:
    assert parse_status("stock_issue") is OrderStatus.STOCK_ISSUE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, WriteMode.OFF),
        ("", WriteMode.OFF),
        ("off", WriteMode.OFF),
        ("dry_run", WriteMode.DRY_RUN),
        ("on", WriteMode.ON),
    ],
)
def test_write_mode_defaults_to_off(raw: str | None, expected: WriteMode) -> None:
    assert parse_write_mode(raw) is expected


@pytest.mark.parametrize("raw", ["ON", "true", "1", "dry-run", "yes"])
def test_unknown_write_mode_is_an_error(raw: str) -> None:
    with pytest.raises(InvalidValueError):
        parse_write_mode(raw)


def test_transition_table_covers_every_status_and_final_states_are_final() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)
    assert ALLOWED_TRANSITIONS[OrderStatus.DELIVERED] == frozenset()
    assert ALLOWED_TRANSITIONS[OrderStatus.CANCELLED] == frozenset()
    for status, targets in ALLOWED_TRANSITIONS.items():
        assert status not in targets
