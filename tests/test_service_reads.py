from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from conftest import ServiceFactory
from wms_mcp.db import connect
from wms_mcp.domain import InvalidValueError, NotFoundError


def test_stalled_orders_older_than_48_hours(make_service: ServiceFactory) -> None:
    result = make_service().list_stalled_orders()
    ids = [o["order_id"] for o in result["orders"]]
    # Oldest first; shipped/delivered/cancelled excluded; 10432 is only 26 h old.
    assert ids == [10420, 10409, 10423, 10412, 10417, 10426]
    assert result["orders"][0]["age_hours"] == 96.0


def test_stalled_orders_threshold_is_respected(make_service: ServiceFactory) -> None:
    ids = [o["order_id"] for o in make_service().list_stalled_orders(min_hours=72)["orders"]]
    assert ids == [10420, 10409]


def test_stalled_orders_by_time_in_status(make_service: ServiceFactory) -> None:
    # 10417 is 50 h old but moved to picking 3 h ago: late by age, not idle.
    by_age = make_service().list_stalled_orders(min_hours=48)
    idle = make_service().list_stalled_orders(min_hours=48, since="status_change")
    assert 10417 in [o["order_id"] for o in by_age["orders"]]
    assert [o["order_id"] for o in idle["orders"]] == [10409, 10423]
    assert idle["since"] == "status_change"
    assert "current status" in idle["definition"]


def test_stalled_orders_rejects_unknown_since(make_service: ServiceFactory) -> None:
    with pytest.raises(InvalidValueError, match="since must be one of"):
        make_service().list_stalled_orders(since="updated_at")


@pytest.mark.parametrize("hours", [0, -5, 721])
def test_stalled_orders_rejects_out_of_range_threshold(
    make_service: ServiceFactory, hours: int
) -> None:
    with pytest.raises(InvalidValueError):
        make_service().list_stalled_orders(min_hours=hours)


def test_stock_per_location_with_shortage(make_service: ServiceFactory) -> None:
    stock = make_service().get_stock("TDW-GLV-L")
    assert stock["locations"] == [{"location": "A-02-01", "on_hand": 1}]
    assert (stock["total_on_hand"], stock["awaiting_pick"]) == (1, 4)
    assert stock["shortage"] is True
    assert [o["order_id"] for o in stock["open_orders"]] == [10432, 10438]


def test_stock_across_several_locations(make_service: ServiceFactory) -> None:
    stock = make_service().get_stock("lmn-tea-erl")
    assert stock["sku"] == "LMN-TEA-ERL"
    assert stock["locations"] == [
        {"location": "C-01-01", "on_hand": 20},
        {"location": "C-01-02", "on_hand": 14},
        {"location": "R-11-01", "on_hand": 60},
    ]
    assert stock["available_after_open_orders"] == 94 - 7


def test_partial_sku_is_not_guessed(make_service: ServiceFactory) -> None:
    result = make_service().get_stock("TDW-GLV")
    assert result["outcome"] == "not_found"
    assert "TDW-GLV-L" in result["suggestions"]
    assert "locations" not in result


def test_name_search_is_accent_insensitive(make_service: ServiceFactory) -> None:
    result = make_service().find_orders("martinez")
    names = {o["customer_name"] for o in result["orders"]}
    assert names == {"Lucía Martínez", "Carmen Martinez", "Javier Martínez Soler"}
    assert result["count"] == 4


def test_ambiguous_name_returns_candidates_not_an_order(make_service: ServiceFactory) -> None:
    result = make_service().get_order("Martínez")
    assert result["outcome"] == "needs_clarification"
    assert "order" not in result
    assert sorted(c["order_id"] for c in result["candidates"]) == [10401, 10409, 10412, 10432]


def test_unique_name_resolves(make_service: ServiceFactory) -> None:
    order = make_service().get_order("navarro")["order"]
    assert order["order_id"] == 10423
    assert order["status_reason"]["trust"] == "untrusted"
    assert [line["sku"] for line in order["lines"]] == ["LMN-KTL-GLS", "LMN-TEA-ERL"]


def test_recipient_name_is_searchable(make_service: ServiceFactory) -> None:
    assert make_service().get_order("Ana García")["order"]["order_id"] == 10405


def test_unknown_order_id(make_service: ServiceFactory) -> None:
    with pytest.raises(NotFoundError):
        make_service().get_order("99999")


def test_too_short_name_is_rejected(make_service: ServiceFactory) -> None:
    with pytest.raises(InvalidValueError, match="too short"):
        make_service().get_order("Li")


def test_read_connections_cannot_write(db_path: Path) -> None:
    with (
        closing(connect(db_path, read_only=True)) as conn,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        conn.execute(
            "INSERT INTO audit_log (table_name, row_id, op, actor, after_json) "
            "VALUES ('orders', 1, 'UPDATE', 'agent', '{}')"
        )


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "typo.sqlite"
    with pytest.raises(FileNotFoundError):
        connect(missing, read_only=True)
    assert not missing.exists()


def test_audit_log_for_an_order(make_service: ServiceFactory) -> None:
    result = make_service().get_audit_log("10420")
    assert result["scope"] == "order"
    assert [e["op"] for e in result["entries"]] == ["INSERT"]
    assert result["entries"][0]["actor"] == "script"
