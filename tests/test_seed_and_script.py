from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from wms_mcp.seed import main as seed_main

REPO = Path(__file__).resolve().parents[1]


def test_seed_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    db = tmp_path / "wms.sqlite"
    assert seed_main(["--db", str(db)]) == 0
    assert seed_main(["--db", str(db)]) == 1
    assert seed_main(["--db", str(db), "--force"]) == 0


def test_seed_data_is_synthetic_and_shaped_for_the_demo(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        emails = [r[0] for r in conn.execute("SELECT customer_email FROM orders")]
        statuses = {r[0] for r in conn.execute("SELECT status FROM orders")}
        multi_location = conn.execute(
            "SELECT COUNT(*) FROM (SELECT sku FROM stock_movements GROUP BY sku "
            "HAVING COUNT(DISTINCT location) > 1)"
        ).fetchone()[0]
    assert all(e.endswith("@example.com") for e in emails)
    assert len(statuses) == 9
    assert multi_location >= 3


def test_example_script_writes_are_audited_as_script(db_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "examples" / "script_writer.py"),
            "--db",
            str(db_path),
            "--order",
            "10440",
            "--reason",
            "Nightly address check: postcode missing",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    with closing(sqlite3.connect(db_path)) as conn:
        actor, op = conn.execute(
            "SELECT actor, op FROM audit_log WHERE row_id = 10440 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert (actor, op) == ("script", "UPDATE")
    assert "script" in proc.stdout
