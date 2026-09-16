"""SQLite connection helpers.

Read paths open the file with ``mode=ro``, so a read tool cannot write even if
its SQL were wrong. Neither path creates a missing database: a typo in
WMS_DB_PATH fails loudly instead of silently serving an empty warehouse.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from importlib import resources
from pathlib import Path


def fold(value: object) -> str | None:
    """Case- and accent-insensitive key: 'Martínez' and 'MARTINEZ' fold equal."""
    if value is None:
        return None
    decomposed = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def schema_sql() -> str:
    return resources.files("wms_mcp").joinpath("schema.sql").read_text(encoding="utf-8")


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("wms_fold", 1, fold, deterministic=True)
    return conn


def connect(db_path: Path, *, read_only: bool) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(
            f"WMS database not found at {db_path}; run `make seed` or set WMS_DB_PATH"
        )
    mode = "ro" if read_only else "rw"
    uri = f"{db_path.resolve().as_uri()}?mode={mode}"
    # isolation_level=None: we issue BEGIN/COMMIT/ROLLBACK explicitly.
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    return _configure(conn)


def create_database(db_path: Path) -> sqlite3.Connection:
    """Create a new database file with the schema. Refuses to reuse an existing file."""
    if db_path.exists():
        raise FileExistsError(f"refusing to overwrite existing database at {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(schema_sql())
    return _configure(conn)
