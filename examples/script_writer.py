"""A batch job that writes to the WMS WITHOUT going through the MCP server.

It uses only the standard library, on purpose: the audit trail must capture it
anyway, because the rules live in database triggers, not in the server.

    python examples/script_writer.py --db data/wms.sqlite --order 10440 \
        --reason "Nightly address check: postcode missing"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--order", type=int, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(sqlite3.connect(f"file:{args.db}?mode=rw", uri=True, isolation_level=None)) as db:
        try:
            db.execute(
                "UPDATE orders SET status = 'address_issue', status_reason = ?, "
                "status_changed_at = ?, write_actor = 'script' WHERE id = ?",
                (args.reason, now, args.order),
            )
        except sqlite3.IntegrityError as exc:
            print(f"rejected by the database: {exc}", file=sys.stderr)
            return 1
        rows = db.execute(
            "SELECT id, op, actor, changed_at, before_json, after_json FROM audit_log "
            "WHERE table_name = 'orders' AND row_id = ? ORDER BY id",
            (args.order,),
        ).fetchall()
    for row in rows:
        print(" | ".join(str(v) for v in row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
