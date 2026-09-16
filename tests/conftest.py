from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wms_mcp.db import create_database
from wms_mcp.domain import Actor, WriteMode
from wms_mcp.seed import seed
from wms_mcp.service import WmsService

# Arbitrary fixed clock for deterministic tests. Not a real event date.
FIXED_NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)

ServiceFactory = Callable[..., WmsService]


def _seeded(path: Path, now: datetime) -> Path:
    with closing(create_database(path)) as conn:
        seed(conn, now)
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _seeded(tmp_path / "wms.sqlite", FIXED_NOW)


@pytest.fixture
def live_db_path(tmp_path: Path) -> Path:
    """Seeded relative to the real clock, for tests that run the server as a subprocess."""
    return _seeded(tmp_path / "wms-live.sqlite", datetime.now(UTC))


@pytest.fixture
def make_service(db_path: Path) -> ServiceFactory:
    def factory(mode: WriteMode = WriteMode.OFF, actor: Actor = Actor.AGENT) -> WmsService:
        return WmsService(db_path, mode, actor=actor, clock=lambda: FIXED_NOW)

    return factory


@pytest.fixture
def raw(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A plain sqlite3 connection with no wms_mcp code involved: stands in for a script."""
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def snapshot(path: Path) -> tuple[list[tuple[object, ...]], int]:
    """All order rows plus the audit row count, to prove that nothing changed."""
    with closing(sqlite3.connect(path)) as conn:
        orders = [tuple(r) for r in conn.execute("SELECT * FROM orders ORDER BY id")]
        audit = int(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
    return orders, audit
