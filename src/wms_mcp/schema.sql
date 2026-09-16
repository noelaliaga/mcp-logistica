-- WMS demo schema: three business tables plus an audit log.
--
-- The rules that matter are enforced HERE, not only in Python, so they also
-- apply to scripts, ad-hoc SQL and any future UI that writes to this file.
--
--   * Closed status enum (CHECK). An invented status is rejected, never coerced.
--   * Column allowlist on orders: only status, status_reason, status_changed_at
--     and notes can change. Prices, quantities, recipient and address abort.
--   * order_lines and stock_movements are append-only (no UPDATE, no DELETE).
--   * Nothing is ever deleted.
--   * A status change needs a new, non-empty reason.
--   * Every write must declare its actor (agent | script | ui). A missing or
--     unknown actor aborts. AFTER triggers copy before/after into audit_log.
--
-- SQLite has no roles, so the actor is SELF-DECLARED by the writer. See
-- docs/decisions.md for why this is weaker than a Postgres session role.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- orders

CREATE TABLE orders (
    id                 INTEGER PRIMARY KEY,
    merchant           TEXT    NOT NULL,
    customer_name      TEXT    NOT NULL,
    customer_email     TEXT    NOT NULL,
    recipient_name     TEXT    NOT NULL,
    ship_street        TEXT    NOT NULL,
    ship_postcode      TEXT    NOT NULL,
    ship_city          TEXT    NOT NULL,
    ship_country       TEXT    NOT NULL,
    total_price_cents  INTEGER NOT NULL CHECK (total_price_cents >= 0),
    currency           TEXT    NOT NULL CHECK (currency IN ('EUR', 'USD')),
    status             TEXT    NOT NULL CHECK (status IN (
                           'pending', 'allocated', 'picking', 'packed', 'shipped',
                           'delivered', 'cancelled', 'stock_issue', 'address_issue')),
    status_reason      TEXT,
    status_changed_at  TEXT    NOT NULL,
    notes              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL,
    -- Statement-scoped actor declaration. It must be set by every INSERT and
    -- UPDATE, is copied to audit_log, and is cleared back to NULL by the AFTER
    -- trigger. Because it is NULL at rest, an UPDATE that forgets to declare an
    -- actor cannot silently inherit the previous writer's identity.
    write_actor        TEXT    CHECK (write_actor IS NULL OR write_actor IN ('agent', 'script', 'ui'))
);

CREATE INDEX orders_status_created ON orders (status, created_at);

CREATE TRIGGER orders_actor_required_on_insert
BEFORE INSERT ON orders
WHEN NEW.write_actor IS NULL OR NEW.write_actor NOT IN ('agent', 'script', 'ui')
BEGIN
    SELECT RAISE(ABORT, 'write_actor is required and must be one of: agent, script, ui');
END;

-- OLD.write_actor IS NULL means the row is at rest, so this is a fresh UPDATE
-- statement and it must declare an actor. OLD NOT NULL -> NEW NULL is only the
-- reset issued by the AFTER triggers below.
CREATE TRIGGER orders_actor_required_on_update
BEFORE UPDATE ON orders
WHEN (NEW.write_actor IS NULL AND OLD.write_actor IS NULL)
  OR (NEW.write_actor IS NOT NULL AND NEW.write_actor NOT IN ('agent', 'script', 'ui'))
BEGIN
    SELECT RAISE(ABORT, 'write_actor is required and must be one of: agent, script, ui');
END;

CREATE TRIGGER orders_readonly_columns
BEFORE UPDATE ON orders
WHEN NEW.id                IS NOT OLD.id
  OR NEW.merchant          IS NOT OLD.merchant
  OR NEW.customer_name     IS NOT OLD.customer_name
  OR NEW.customer_email    IS NOT OLD.customer_email
  OR NEW.recipient_name    IS NOT OLD.recipient_name
  OR NEW.ship_street       IS NOT OLD.ship_street
  OR NEW.ship_postcode     IS NOT OLD.ship_postcode
  OR NEW.ship_city         IS NOT OLD.ship_city
  OR NEW.ship_country      IS NOT OLD.ship_country
  OR NEW.total_price_cents IS NOT OLD.total_price_cents
  OR NEW.currency          IS NOT OLD.currency
  OR NEW.created_at        IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'orders: column is not writable (writable: status, status_reason, status_changed_at, notes)');
END;

-- A status change must come with its OWN reason. Checking only for a
-- non-empty value is not enough: an UPDATE that does not touch status_reason
-- keeps the previous one, so a script could cancel an order "because" of an
-- old breakage note. The reason must be non-empty AND differ from the old one.
CREATE TRIGGER orders_status_change_needs_reason
BEFORE UPDATE OF status ON orders
WHEN NEW.status IS NOT OLD.status
 AND (NEW.status_reason IS NULL
      OR trim(NEW.status_reason) = ''
      OR NEW.status_reason IS OLD.status_reason)
BEGIN
    SELECT RAISE(ABORT, 'orders: a status change requires a new, non-empty status_reason');
END;

CREATE TRIGGER orders_notes_append_only
BEFORE UPDATE OF notes ON orders
WHEN NEW.notes IS NOT OLD.notes
 AND substr(NEW.notes, 1, length(OLD.notes)) IS NOT OLD.notes
BEGIN
    SELECT RAISE(ABORT, 'orders: notes are append-only');
END;

CREATE TRIGGER orders_no_delete
BEFORE DELETE ON orders
BEGIN
    SELECT RAISE(ABORT, 'orders are never deleted; set status to cancelled instead');
END;

CREATE TRIGGER orders_audit_insert
AFTER INSERT ON orders
WHEN NEW.write_actor IS NOT NULL
BEGIN
    INSERT INTO audit_log (table_name, row_id, op, actor, before_json, after_json)
    VALUES ('orders', NEW.id, 'INSERT', NEW.write_actor, NULL,
        json_object(
            'status', NEW.status, 'status_reason', NEW.status_reason,
            'status_changed_at', NEW.status_changed_at, 'notes', NEW.notes,
            'recipient_name', NEW.recipient_name, 'total_price_cents', NEW.total_price_cents));
    UPDATE orders SET write_actor = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER orders_audit_update
AFTER UPDATE ON orders
WHEN NEW.write_actor IS NOT NULL
BEGIN
    INSERT INTO audit_log (table_name, row_id, op, actor, before_json, after_json)
    VALUES ('orders', NEW.id, 'UPDATE', NEW.write_actor,
        json_object(
            'status', OLD.status, 'status_reason', OLD.status_reason,
            'status_changed_at', OLD.status_changed_at, 'notes', OLD.notes),
        json_object(
            'status', NEW.status, 'status_reason', NEW.status_reason,
            'status_changed_at', NEW.status_changed_at, 'notes', NEW.notes));
    UPDATE orders SET write_actor = NULL WHERE id = NEW.id;
END;

-- ----------------------------------------------------------- order_lines

CREATE TABLE order_lines (
    id                INTEGER PRIMARY KEY,
    order_id          INTEGER NOT NULL REFERENCES orders (id),
    sku               TEXT    NOT NULL,
    description       TEXT    NOT NULL,
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents  INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    created_by        TEXT    NOT NULL CHECK (created_by IN ('agent', 'script', 'ui'))
);

CREATE INDEX order_lines_order ON order_lines (order_id);
CREATE INDEX order_lines_sku ON order_lines (sku);

CREATE TRIGGER order_lines_actor_required
BEFORE INSERT ON order_lines
WHEN NEW.created_by IS NULL OR NEW.created_by NOT IN ('agent', 'script', 'ui')
BEGIN
    SELECT RAISE(ABORT, 'created_by is required and must be one of: agent, script, ui');
END;

CREATE TRIGGER order_lines_no_update
BEFORE UPDATE ON order_lines
BEGIN
    SELECT RAISE(ABORT, 'order_lines are immutable: quantities and prices cannot be changed');
END;

CREATE TRIGGER order_lines_no_delete
BEFORE DELETE ON order_lines
BEGIN
    SELECT RAISE(ABORT, 'order_lines are never deleted');
END;

CREATE TRIGGER order_lines_audit_insert
AFTER INSERT ON order_lines
BEGIN
    INSERT INTO audit_log (table_name, row_id, op, actor, before_json, after_json)
    VALUES ('order_lines', NEW.id, 'INSERT', NEW.created_by, NULL,
        json_object('order_id', NEW.order_id, 'sku', NEW.sku,
                    'quantity', NEW.quantity, 'unit_price_cents', NEW.unit_price_cents));
END;

-- ------------------------------------------------------- stock_movements

-- Stock is a ledger: on-hand quantity per (sku, location) is SUM(qty_delta).
-- Corrections are new rows (reason = 'adjustment'), never edits.
CREATE TABLE stock_movements (
    id          INTEGER PRIMARY KEY,
    sku         TEXT    NOT NULL,
    location    TEXT    NOT NULL,
    qty_delta   INTEGER NOT NULL CHECK (qty_delta <> 0),
    reason      TEXT    NOT NULL CHECK (reason IN (
                    'receipt', 'pick', 'adjustment', 'return', 'transfer_in', 'transfer_out')),
    order_id    INTEGER REFERENCES orders (id),
    created_at  TEXT    NOT NULL,
    created_by  TEXT    NOT NULL CHECK (created_by IN ('agent', 'script', 'ui'))
);

CREATE INDEX stock_movements_sku ON stock_movements (sku, location);

CREATE TRIGGER stock_movements_actor_required
BEFORE INSERT ON stock_movements
WHEN NEW.created_by IS NULL OR NEW.created_by NOT IN ('agent', 'script', 'ui')
BEGIN
    SELECT RAISE(ABORT, 'created_by is required and must be one of: agent, script, ui');
END;

CREATE TRIGGER stock_movements_no_update
BEFORE UPDATE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'stock_movements are append-only; post an adjustment instead');
END;

CREATE TRIGGER stock_movements_no_delete
BEFORE DELETE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'stock_movements are append-only; post an adjustment instead');
END;

CREATE TRIGGER stock_movements_audit_insert
AFTER INSERT ON stock_movements
BEGIN
    INSERT INTO audit_log (table_name, row_id, op, actor, before_json, after_json)
    VALUES ('stock_movements', NEW.id, 'INSERT', NEW.created_by, NULL,
        json_object('sku', NEW.sku, 'location', NEW.location,
                    'qty_delta', NEW.qty_delta, 'reason', NEW.reason, 'order_id', NEW.order_id));
END;

-- ------------------------------------------------------------- audit_log

CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    table_name   TEXT NOT NULL CHECK (table_name IN ('orders', 'order_lines', 'stock_movements')),
    row_id       INTEGER NOT NULL,
    op           TEXT NOT NULL CHECK (op IN ('INSERT', 'UPDATE')),
    actor        TEXT NOT NULL CHECK (actor IN ('agent', 'script', 'ui')),
    changed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    before_json  TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
    after_json   TEXT NOT NULL CHECK (json_valid(after_json))
);

CREATE INDEX audit_log_row ON audit_log (table_name, row_id);

CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
