"""WMS operations exposed to the agent.

This layer is transport-agnostic (no MCP imports) so every rule can be tested
without a client. The MCP adapter in server.py is a thin wrapper.

Rules implemented here, on top of the database triggers:

* Disambiguation: a name that matches several orders returns candidates and
  performs no write. The service never picks one. Write tools go further: a
  name never resolves to a write target, even when it matches a single order;
  the agent gets that order back and must repeat the call with its id.
* Atomic writes: resolution, transition check and UPDATE run inside one
  ``BEGIN IMMEDIATE`` transaction, and the UPDATE is conditional on the status
  that was checked, so a concurrent writer cannot slip in between.
* Write mode: ``off`` rejects writes, ``dry_run`` runs the real UPDATE inside a
  transaction, reports the diff and rolls back, ``on`` commits.
* Rejection over repair: unknown statuses, over-long reasons and empty notes
  raise. Nothing is truncated, lower-cased or fuzzy-matched into validity.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wms_mcp.db import connect, fold
from wms_mcp.domain import (
    AGENT_FORBIDDEN_TARGETS,
    ALLOWED_TRANSITIONS,
    AWAITING_PICK_STATUSES,
    MAX_NOTE_CHARS,
    MAX_REASON_CHARS,
    OPEN_STATUSES,
    WRITABLE_ORDER_FIELDS,
    Actor,
    ForbiddenFieldError,
    InvalidValueError,
    NotFoundError,
    OrderStatus,
    TransitionError,
    WriteDisabledError,
    WriteMode,
    WriteRejectedError,
    parse_status,
    parse_write_mode,
)
from wms_mcp.untrusted import wrap

Clock = Callable[[], datetime]
JsonDict = dict[str, Any]

DEFAULT_DB_PATH = Path("data/wms.sqlite")
MAX_CANDIDATES = 10
MIN_SEARCH_CHARS = 3
FREE_TEXT_FIELDS = frozenset({"notes", "status_reason"})
STALLED_SINCE: Mapping[str, str] = {"created": "created_at", "status_change": "status_changed_at"}
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def hours_between(earlier: str, now: datetime) -> float:
    return round((now - from_iso(earlier)).total_seconds() / 3600, 1)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    write_mode: WriteMode

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        raw_path = env.get("WMS_DB_PATH")
        return cls(
            db_path=Path(raw_path) if raw_path else DEFAULT_DB_PATH,
            write_mode=parse_write_mode(env.get("WMS_WRITE_MODE")),
        )


@dataclass(frozen=True)
class Clarification:
    """Returned instead of an order when a reference is ambiguous."""

    query: str
    candidates: list[JsonDict]
    truncated: bool

    def as_result(self, *, for_write: bool) -> JsonDict:
        action = "No changes were made. " if for_write else ""
        more = " (more exist; ask for a narrower reference)" if self.truncated else ""
        return {
            "outcome": "needs_clarification",
            "write_performed": False,
            "message": (
                f"{self.query!r} matches {len(self.candidates)} orders{more}. "
                f"{action}Ask the user which order they mean, "
                "using the order id; do not choose one yourself."
            ),
            "candidates": self.candidates,
        }


def _needs_confirmation(query: str, row: sqlite3.Row, summary: JsonDict) -> JsonDict:
    return {
        "outcome": "needs_confirmation",
        "write_performed": False,
        "message": (
            f"{query!r} matches one order ({row['id']}), but a write needs the order id. "
            "No changes were made. Confirm with the user that this is the order they mean, "
            "then repeat the call with order_ref set to the id."
        ),
        "candidates": [summary],
    }


def parse_notes(raw: str) -> list[JsonDict]:
    """Parse the JSON-lines notes column. Malformed lines are returned, not dropped.

    ``claimed_at`` and ``claimed_by`` are whatever the writer stored: any writer
    can put ``"by": "ui"`` in a note. Values outside the expected format or
    vocabulary come back as null / "unknown". audit_log is the authority on
    who actually wrote to the row.
    """
    entries: list[JsonDict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("text"), str):
            at = obj.get("at")
            by = obj.get("by")
            entries.append(
                {
                    "claimed_at": at if isinstance(at, str) and _ISO_RE.match(at) else None,
                    "claimed_by": by if by in {a.value for a in Actor} else "unknown",
                    "text": wrap(obj["text"], "order_note"),
                }
            )
        else:
            entries.append(
                {
                    "claimed_at": None,
                    "claimed_by": "unknown",
                    "text": wrap(line, "order_note_unparsed"),
                }
            )
    return entries


def _clean_text(value: str, field: str, max_chars: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise InvalidValueError(f"{field} must not be empty")
    if len(cleaned) > max_chars:
        raise InvalidValueError(
            f"{field} is {len(cleaned)} characters; the limit is {max_chars}. "
            "Shorten it yourself; the server does not truncate."
        )
    return cleaned


class WmsService:
    def __init__(
        self,
        db_path: Path,
        write_mode: WriteMode = WriteMode.OFF,
        *,
        actor: Actor = Actor.AGENT,
        clock: Clock = utc_now,
    ) -> None:
        self.db_path = db_path
        self.write_mode = write_mode
        self.actor = actor
        self.clock = clock

    # ------------------------------------------------------------ plumbing

    @contextmanager
    def _conn(self, *, read_only: bool) -> Iterator[sqlite3.Connection]:
        with closing(connect(self.db_path, read_only=read_only)) as conn:
            yield conn

    def _require_writes(self) -> None:
        if self.write_mode is WriteMode.OFF:
            raise WriteDisabledError(
                "writes are disabled on this server (WMS_WRITE_MODE=off). "
                "Tell the user; do not retry. An operator can restart the server "
                "with WMS_WRITE_MODE=dry_run or on."
            )

    def _summary(self, row: sqlite3.Row) -> JsonDict:
        now = self.clock()
        return {
            "order_id": row["id"],
            "merchant": row["merchant"],
            "customer_name": row["customer_name"],
            "recipient_name": row["recipient_name"],
            "ship_city": row["ship_city"],
            "status": row["status"],
            "created_at": row["created_at"],
            "age_hours": hours_between(row["created_at"], now),
        }

    def _resolve(self, conn: sqlite3.Connection, order_ref: str) -> sqlite3.Row | Clarification:
        ref = order_ref.strip().removeprefix("#")
        if not ref:
            raise InvalidValueError("order_ref must not be empty")
        if ref.isdigit():
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (int(ref),)).fetchone()
            if row is None:
                raise NotFoundError(f"order {ref} does not exist")
            return row  # type: ignore[no-any-return]
        needle = fold(ref) or ""
        if len(needle) < MIN_SEARCH_CHARS:
            raise InvalidValueError(
                f"name reference {order_ref!r} is too short; use at least "
                f"{MIN_SEARCH_CHARS} characters or the order id"
            )
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE instr(wms_fold(customer_name), :n) > 0
               OR instr(wms_fold(recipient_name), :n) > 0
            ORDER BY created_at DESC, id DESC
            LIMIT :lim
            """,
            {"n": needle, "lim": MAX_CANDIDATES + 1},
        ).fetchall()
        if not rows:
            raise NotFoundError(f"no order matches {order_ref!r}")
        if len(rows) == 1:
            return rows[0]  # type: ignore[no-any-return]
        return Clarification(
            query=order_ref,
            candidates=[self._summary(r) for r in rows[:MAX_CANDIDATES]],
            truncated=len(rows) > MAX_CANDIDATES,
        )

    # --------------------------------------------------------------- reads

    def find_orders(self, query: str, limit: int = 10) -> JsonDict:
        if not 1 <= limit <= 25:
            raise InvalidValueError("limit must be between 1 and 25")
        with self._conn(read_only=True) as conn:
            ref = query.strip().removeprefix("#")
            if ref.isdigit():
                rows = conn.execute("SELECT * FROM orders WHERE id = ?", (int(ref),)).fetchall()
            else:
                needle = fold(ref) or ""
                if len(needle) < MIN_SEARCH_CHARS:
                    raise InvalidValueError(
                        f"query must have at least {MIN_SEARCH_CHARS} characters"
                    )
                rows = conn.execute(
                    """
                    SELECT * FROM orders
                    WHERE instr(wms_fold(customer_name), :n) > 0
                       OR instr(wms_fold(recipient_name), :n) > 0
                    ORDER BY created_at DESC, id DESC
                    LIMIT :lim
                    """,
                    {"n": needle, "lim": limit},
                ).fetchall()
        return {
            "outcome": "ok",
            "count": len(rows),
            "orders": [self._summary(r) for r in rows],
        }

    def get_order(self, order_ref: str) -> JsonDict:
        with self._conn(read_only=True) as conn:
            resolved = self._resolve(conn, order_ref)
            if isinstance(resolved, Clarification):
                return resolved.as_result(for_write=False)
            lines = conn.execute(
                "SELECT sku, description, quantity, unit_price_cents FROM order_lines "
                "WHERE order_id = ? ORDER BY id",
                (resolved["id"],),
            ).fetchall()
        row = resolved
        reason = row["status_reason"]
        return {
            "outcome": "ok",
            "order": {
                **self._summary(row),
                "customer_email": row["customer_email"],
                "ship_street": row["ship_street"],
                "ship_postcode": row["ship_postcode"],
                "ship_country": row["ship_country"],
                "total_price_cents": row["total_price_cents"],
                "currency": row["currency"],
                "status_changed_at": row["status_changed_at"],
                "hours_in_status": hours_between(row["status_changed_at"], self.clock()),
                "status_reason": wrap(reason, "status_reason") if reason else None,
                "lines": [dict(line) for line in lines],
                "notes": parse_notes(row["notes"]),
            },
        }

    def list_stalled_orders(
        self, min_hours: int = 48, limit: int = 50, since: str = "created"
    ) -> JsonDict:
        """Open orders older than ``min_hours``.

        ``since="created"`` measures order age (the fulfilment SLA view: an order
        created three days ago is late even if it moved to picking an hour ago).
        ``since="status_change"`` measures time in the current status (the
        "nothing has happened to it" view).
        """
        if since not in STALLED_SINCE:
            raise InvalidValueError(
                f"since must be one of: {', '.join(sorted(STALLED_SINCE))}; got {since!r}"
            )
        column = STALLED_SINCE[since]
        if not 1 <= min_hours <= 720:
            raise InvalidValueError("min_hours must be between 1 and 720")
        if not 1 <= limit <= 200:
            raise InvalidValueError("limit must be between 1 and 200")
        now = self.clock()
        cutoff = to_iso(now - timedelta(hours=min_hours))
        open_statuses = sorted(s.value for s in OPEN_STATUSES)
        marks = ", ".join("?" for _ in open_statuses)
        with self._conn(read_only=True) as conn:
            rows = conn.execute(
                # column comes from the STALLED_SINCE allowlist, never from input.
                f"SELECT * FROM orders WHERE status IN ({marks}) AND {column} <= ? "
                f"ORDER BY {column}, id LIMIT ?",
                (*open_statuses, cutoff, limit),
            ).fetchall()
        return {
            "outcome": "ok",
            "as_of": to_iso(now),
            "threshold_hours": min_hours,
            "since": since,
            "definition": (
                "status not in shipped, delivered, cancelled and "
                + (
                    "created at least threshold_hours ago"
                    if since == "created"
                    else "in its current status for at least threshold_hours"
                )
            ),
            "count": len(rows),
            "orders": [
                {
                    **self._summary(r),
                    "hours_in_status": hours_between(r["status_changed_at"], now),
                }
                for r in rows
            ],
        }

    def get_stock(self, sku: str) -> JsonDict:
        code = sku.strip()
        if not code:
            raise InvalidValueError("sku must not be empty")
        awaiting = sorted(s.value for s in AWAITING_PICK_STATUSES)
        marks = ", ".join("?" for _ in awaiting)
        with self._conn(read_only=True) as conn:
            canonical = conn.execute(
                "SELECT sku FROM stock_movements WHERE upper(sku) = upper(?) LIMIT 1", (code,)
            ).fetchone()
            if canonical is None:
                suggestions = [
                    r["sku"]
                    for r in conn.execute(
                        "SELECT DISTINCT sku FROM stock_movements "
                        "WHERE instr(wms_fold(sku), ?) > 0 ORDER BY sku LIMIT 5",
                        (fold(code),),
                    )
                ]
                return {
                    "outcome": "not_found",
                    "message": f"no stock records for SKU {code!r}. Confirm the exact SKU "
                    "with the user; similar codes are listed only as suggestions.",
                    "suggestions": suggestions,
                }
            real_sku = canonical["sku"]
            locations = conn.execute(
                "SELECT location, SUM(qty_delta) AS on_hand FROM stock_movements "
                "WHERE sku = ? GROUP BY location HAVING SUM(qty_delta) <> 0 ORDER BY location",
                (real_sku,),
            ).fetchall()
            demand = conn.execute(
                f"SELECT o.id AS order_id, o.status, l.quantity FROM order_lines l "
                f"JOIN orders o ON o.id = l.order_id "
                f"WHERE l.sku = ? AND o.status IN ({marks}) ORDER BY o.created_at, o.id",
                (real_sku, *awaiting),
            ).fetchall()
        total = sum(int(r["on_hand"]) for r in locations)
        awaiting_qty = sum(int(r["quantity"]) for r in demand)
        return {
            "outcome": "ok",
            "sku": real_sku,
            "locations": [
                {"location": r["location"], "on_hand": int(r["on_hand"])} for r in locations
            ],
            "total_on_hand": total,
            "awaiting_pick": awaiting_qty,
            "available_after_open_orders": total - awaiting_qty,
            "shortage": awaiting_qty > total,
            "open_orders": [dict(r) for r in demand],
        }

    def get_audit_log(self, order_ref: str | None = None, limit: int = 20) -> JsonDict:
        if not 1 <= limit <= 100:
            raise InvalidValueError("limit must be between 1 and 100")
        with self._conn(read_only=True) as conn:
            if order_ref is None or not order_ref.strip():
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                scope: JsonDict = {"scope": "all"}
            else:
                resolved = self._resolve(conn, order_ref)
                if isinstance(resolved, Clarification):
                    return resolved.as_result(for_write=False)
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE table_name = 'orders' AND row_id = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (resolved["id"], limit),
                ).fetchall()
                scope = {"scope": "order", "order_id": resolved["id"]}
        return {"outcome": "ok", **scope, "entries": [_audit_entry(r) for r in rows]}

    # -------------------------------------------------------------- writes

    def set_order_status(self, order_ref: str, status: str, reason: str) -> JsonDict:
        self._require_writes()
        target = parse_status(status)
        clean_reason = _clean_text(reason, "reason", MAX_REASON_CHARS)
        self._check_agent_target(target)
        with self._write_transaction() as conn:
            resolved = self._resolve_write_target(conn, order_ref)
            if not isinstance(resolved, sqlite3.Row):
                return resolved
            current = OrderStatus(resolved["status"])
            if target is current:
                raise TransitionError(f"order {resolved['id']} is already {current.value!r}")
            if target not in ALLOWED_TRANSITIONS[current]:
                allowed = ", ".join(sorted(s.value for s in ALLOWED_TRANSITIONS[current]))
                raise TransitionError(
                    f"cannot move order {resolved['id']} from {current.value!r} to "
                    f"{target.value!r}; allowed next statuses: {allowed or 'none (final)'}"
                )
            if clean_reason == resolved["status_reason"]:
                raise InvalidValueError(
                    "reason repeats the order's current status_reason; describe why "
                    "this change is being made"
                )
            changes = {
                "status": target.value,
                "status_reason": clean_reason,
                "status_changed_at": to_iso(self.clock()),
            }
            return self._apply(conn, resolved, changes)

    def add_order_note(self, order_ref: str, note: str) -> JsonDict:
        self._require_writes()
        text = _clean_text(note, "note", MAX_NOTE_CHARS)
        with self._write_transaction() as conn:
            resolved = self._resolve_write_target(conn, order_ref)
            if not isinstance(resolved, sqlite3.Row):
                return resolved
            entry = json.dumps(
                {"at": to_iso(self.clock()), "by": self.actor.value, "text": text},
                ensure_ascii=False,
            )
            changes = {"notes": f"{resolved['notes']}{entry}\n"}
            return self._apply(conn, resolved, changes)

    def _write_order_fields(self, order_id: int, changes: Mapping[str, str]) -> JsonDict:
        """Low-level allowlisted write, used by tests to reach the database checks.

        Deliberately private and not exposed as a tool: it skips the transition
        and reason checks of set_order_status. The column allowlist and the
        agent's forbidden target statuses still apply.
        """
        self._require_writes()
        if "status" in changes:
            self._check_agent_target(parse_status(changes["status"]))
        with self._write_transaction() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"order {order_id} does not exist")
            return self._apply(conn, row, changes)

    def _check_agent_target(self, target: OrderStatus) -> None:
        if self.actor is Actor.AGENT and target in AGENT_FORBIDDEN_TARGETS:
            raise TransitionError(
                f"an agent cannot set status {target.value!r}; it must be confirmed "
                "by a scan or by a person in the WMS UI"
            )

    def _resolve_write_target(
        self, conn: sqlite3.Connection, order_ref: str
    ) -> sqlite3.Row | JsonDict:
        """Only an order id selects a write target. Names return candidates."""
        resolved = self._resolve(conn, order_ref)
        if isinstance(resolved, Clarification):
            return resolved.as_result(for_write=True)
        if not order_ref.strip().removeprefix("#").isdigit():
            return _needs_confirmation(order_ref, resolved, self._summary(resolved))
        return resolved

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """One BEGIN IMMEDIATE transaction around read, checks and write.

        Anything the body does not COMMIT is rolled back. Database errors become
        WriteRejectedError so the agent gets the trigger's message.
        """
        with self._conn(read_only=False) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise WriteRejectedError(f"the database is busy, try later: {exc}") from exc
            try:
                yield conn
            except sqlite3.DatabaseError as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise WriteRejectedError(f"the database rejected the write: {exc}") from exc
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            if conn.in_transaction:
                conn.execute("ROLLBACK")

    def _apply(
        self, conn: sqlite3.Connection, before: sqlite3.Row, changes: Mapping[str, str]
    ) -> JsonDict:
        """Run the UPDATE inside the caller's open transaction, then commit or roll back."""
        forbidden = sorted(set(changes) - WRITABLE_ORDER_FIELDS)
        if forbidden:
            raise ForbiddenFieldError(
                f"field(s) not writable by this server: {', '.join(forbidden)}. "
                f"Writable: {', '.join(sorted(WRITABLE_ORDER_FIELDS))}"
            )
        if not changes:
            raise InvalidValueError("no changes requested")
        order_id = int(before["id"])
        columns = sorted(changes)
        # Column names come only from WRITABLE_ORDER_FIELDS (checked above).
        assignments = ", ".join(f"{c} = ?" for c in columns)
        # Compare-and-set on the values the checks were made against. Inside
        # BEGIN IMMEDIATE no other writer can change them, so this is a second
        # line of defence, not the main one.
        cursor = conn.execute(
            f"UPDATE orders SET {assignments}, write_actor = ? "
            "WHERE id = ? AND status = ? AND notes = ?",
            (
                *[changes[c] for c in columns],
                self.actor.value,
                order_id,
                before["status"],
                before["notes"],
            ),
        )
        if cursor.rowcount != 1:
            raise WriteRejectedError(
                f"order {order_id} changed while this write was being prepared; "
                "read it again before retrying"
            )
        after = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        audit = conn.execute(
            "SELECT id FROM audit_log WHERE table_name = 'orders' AND row_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        diff = {c: _field_change(c, before[c], after[c]) for c in columns if before[c] != after[c]}
        if self.write_mode is WriteMode.DRY_RUN:
            conn.execute("ROLLBACK")
            return {
                "outcome": "dry_run",
                "write_performed": False,
                "order_id": order_id,
                "actor": self.actor.value,
                "changes": diff,
                "message": "Dry run: the update passed every check and was rolled back. "
                "Nothing was written.",
            }
        conn.execute("COMMIT")
        return {
            "outcome": "applied",
            "write_performed": True,
            "order_id": order_id,
            "actor": self.actor.value,
            "audit_id": audit["id"] if audit else None,
            "changes": diff,
        }


def _field_value(field: str, value: Any) -> Any:
    if field in FREE_TEXT_FIELDS and isinstance(value, str) and value:
        return wrap(value, field)
    return value


def _field_change(field: str, before: Any, after: Any) -> JsonDict:
    if field == "notes" and isinstance(before, str) and isinstance(after, str):
        appended = after[len(before) :] if after.startswith(before) else after
        return {"appended": parse_notes(appended)}
    return {"before": _field_value(field, before), "after": _field_value(field, after)}


def _audit_entry(row: sqlite3.Row) -> JsonDict:
    before = json.loads(row["before_json"]) if row["before_json"] else None
    after = json.loads(row["after_json"])
    entry: JsonDict = {
        "audit_id": row["id"],
        "table": row["table_name"],
        "row_id": row["row_id"],
        "op": row["op"],
        "actor": row["actor"],
        "changed_at": row["changed_at"],
    }
    if before is None:
        entry["values"] = {k: _field_value(k, v) for k, v in after.items() if k != "notes"}
    else:
        entry["changes"] = {
            k: _field_change(k, before.get(k), v) for k, v in after.items() if before.get(k) != v
        }
    return entry
