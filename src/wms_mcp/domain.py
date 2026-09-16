"""Closed vocabularies and write policy.

Everything here is a *rejection* rule. Nothing in this module tries to repair
or guess a value: an unknown status, actor or write mode raises.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING = "pending"
    ALLOCATED = "allocated"
    PICKING = "picking"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    STOCK_ISSUE = "stock_issue"
    ADDRESS_ISSUE = "address_issue"


class Actor(StrEnum):
    AGENT = "agent"
    SCRIPT = "script"
    UI = "ui"


class WriteMode(StrEnum):
    OFF = "off"
    DRY_RUN = "dry_run"
    ON = "on"


class WmsError(Exception):
    """Base class for expected, agent-facing errors.

    server.py returns the message of these errors to the client verbatim. Any
    other exception is replaced by a generic message and logged on stderr, so
    file paths and SQLite internals do not reach the model.
    """


class InvalidValueError(WmsError):
    """A value outside a closed vocabulary. Rejected, never corrected."""


class ForbiddenFieldError(WmsError):
    """An attempt to write a column outside the allowlist."""


class WriteDisabledError(WmsError):
    """Writes are not enabled for this server process."""


class NotFoundError(WmsError):
    """The referenced entity does not exist."""


class TransitionError(WmsError):
    """The requested status change is not a valid transition."""


class WriteRejectedError(WmsError):
    """The database refused the write (constraint or trigger)."""


# Statuses in which the parcel has not left the building.
OPEN_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.ALLOCATED,
        OrderStatus.PICKING,
        OrderStatus.PACKED,
        OrderStatus.STOCK_ISSUE,
        OrderStatus.ADDRESS_ISSUE,
    }
)

# Statuses whose stock has not been picked yet (demand against on-hand stock).
AWAITING_PICK_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.ALLOCATED,
        OrderStatus.PICKING,
        OrderStatus.STOCK_ISSUE,
        OrderStatus.ADDRESS_ISSUE,
    }
)

ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.ALLOCATED,
            OrderStatus.CANCELLED,
            OrderStatus.STOCK_ISSUE,
            OrderStatus.ADDRESS_ISSUE,
        }
    ),
    OrderStatus.ALLOCATED: frozenset(
        {
            OrderStatus.PICKING,
            OrderStatus.CANCELLED,
            OrderStatus.STOCK_ISSUE,
            OrderStatus.ADDRESS_ISSUE,
        }
    ),
    OrderStatus.PICKING: frozenset(
        {
            OrderStatus.PACKED,
            OrderStatus.CANCELLED,
            OrderStatus.STOCK_ISSUE,
            OrderStatus.ADDRESS_ISSUE,
        }
    ),
    OrderStatus.PACKED: frozenset(
        {OrderStatus.SHIPPED, OrderStatus.CANCELLED, OrderStatus.ADDRESS_ISSUE}
    ),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.STOCK_ISSUE: frozenset(
        {OrderStatus.PENDING, OrderStatus.ALLOCATED, OrderStatus.CANCELLED}
    ),
    OrderStatus.ADDRESS_ISSUE: frozenset(
        {OrderStatus.PENDING, OrderStatus.ALLOCATED, OrderStatus.CANCELLED}
    ),
}

# Physical events are confirmed by a scan at the dock or by the carrier, and a
# cancellation is a commercial decision with no way back (cancelled is final).
# None of them is declared by a language model reading a chat message.
AGENT_FORBIDDEN_TARGETS: frozenset[OrderStatus] = frozenset(
    {OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.CANCELLED}
)

# Python-side column allowlist. The same list is enforced by the
# orders_readonly_columns trigger in schema.sql.
WRITABLE_ORDER_FIELDS: frozenset[str] = frozenset(
    {"status", "status_reason", "status_changed_at", "notes"}
)

MAX_REASON_CHARS = 300
MAX_NOTE_CHARS = 1000


def parse_status(value: str) -> OrderStatus:
    """Return the status for an exact value or raise. No case folding, no fuzzy match."""
    try:
        return OrderStatus(value)
    except ValueError:
        valid = ", ".join(s.value for s in OrderStatus)
        raise InvalidValueError(
            f"unknown order status {value!r}; valid statuses are: {valid}"
        ) from None


def parse_write_mode(value: str | None) -> WriteMode:
    """Parse WMS_WRITE_MODE. Unset means off; anything unknown is an error."""
    if value is None or value == "":
        return WriteMode.OFF
    try:
        return WriteMode(value)
    except ValueError:
        valid = ", ".join(m.value for m in WriteMode)
        raise InvalidValueError(
            f"invalid WMS_WRITE_MODE {value!r}; expected one of: {valid}"
        ) from None
