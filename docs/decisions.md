# Design decisions

Each entry states the decision, why, what it costs and how it is verified.
"Test:" points at the test that fails if the decision is silently undone.

---

## 1. Official MCP SDK (FastMCP) instead of a hand-written JSON-RPC loop

**Decision.** The server uses the official Python SDK (`mcp`, `mcp.server.fastmcp`) over stdio.

**Where the pattern comes from.** The design is a port of a small MCP server I wrote in
August 2026 for a private CRM tool (Postgres behind PostgREST), published later in cleaned-up
form as [`agent-crm-mcp`](https://github.com/noelaliaga/agent-crm-mcp). That server was standard
library only: a JSON-RPC 2.0 loop over stdio with `initialize`, `tools/list` and
`tools/call`, a Python allowlist of writable columns, a resolver that refused to pick
between several name matches, and a database trigger that recorded every change with
the Postgres role that made it. Its data and credentials are not part of this repository.
Only the design carried over.

**Why the SDK this time.**

- **Ecosystem fit.** MCP clients such as Claude Code, Google ADK's `MCPToolset` and
  LiteLLM should be able to use the server (only the SDK's own `ClientSession` and a raw
  JSON-RPC client are exercised here). With the official SDK, protocol-version
  negotiation, capability advertisement and schema generation track the spec instead of
  my reading of it. The same SDK also provides the client used by the agent loop.
- **Schemas from type hints.** `Annotated[..., Field(...)]` gives the model enums,
  bounds and descriptions without hand-maintained JSON Schema.
- **Less protocol code to own.** The value of this project is the restrictions, not
  the framing layer.

**Cost.**

- A dependency, capped at the tested minor: `mcp>=1.30,<1.31`.
- Two SDK defaults had to be overridden:
  - FastMCP registers `call_tool` with `validate_input=False`;
  - its argument models ignore unknown fields.

  Left alone, a call carrying `"total_price_cents": 0` would be silently accepted with
  the extra field dropped. `server.py` sets `additionalProperties: false` on each schema
  and `extra="forbid"` on each argument model.
  Test: `test_protocol_stdio.py::test_unknown_arguments_are_rejected_not_ignored`.
- The second override reaches into an SDK internal
  (`tool.fn_metadata.arg_model.model_config`) because FastMCP 1.x has no public switch
  for it. That is the reason for the tight version cap. If a later SDK moves that
  attribute, the protocol test fails. The fix then is either a public option, if the
  SDK has added one, or registering tools through the low-level `Server` with
  `validate_input=True`. Only after that should the cap be widened.
- `server.py` also subclasses `FastMCP.call_tool` (a public method) to log calls that
  fail argument validation before they reach a tool function.

**Kept from the stdlib version.** The protocol test does not use an MCP client
library. It spawns the server and writes raw JSON-RPC lines, so the wire format stays
visible and a regression in the SDK integration shows up as a failing test.

---

## 2. Rules live in the database, not only in Python

**Decision.** Every restriction that matters exists twice:

| Rule | Python | SQLite |
|---|---|---|
| Closed status enum | `parse_status` | `CHECK (status IN (...))` |
| Writable columns | `WRITABLE_ORDER_FIELDS` | `orders_readonly_columns` trigger |
| Lines and stock are immutable | no code path writes them | `BEFORE UPDATE/DELETE` triggers that abort |
| Status change needs its own reason | `_clean_text` + "not the current reason" | `orders_status_change_needs_reason` (empty **or unchanged** reason aborts) |
| Notes are append-only | append-only code path | `orders_notes_append_only` |
| Nothing is deleted | no code path deletes | `BEFORE DELETE` triggers that abort |
| Every write has an actor | the service always sets one | `*_actor_required*` triggers |

**Why.** A WMS is written to by more than one program: integrations, batch jobs, a
back-office UI, someone with a SQL console at 7 a.m. A rule that only the agent's
server enforces protects only against the agent. Rules in the schema protect
against the next script too.

**Test.** `test_schema_constraints.py` uses a plain `sqlite3` connection with no project
code. `test_every_column_outside_the_allowlist_is_blocked` reads `PRAGMA table_info`,
so a new column added without protection fails CI.

**A gap found in review.** The reason trigger first checked only that `status_reason`
was non-empty. An `UPDATE` that changes `status` and leaves `status_reason` alone keeps
the previous value, so a script could cancel an order "because" of an old breakage note.
The trigger now also aborts when the reason is unchanged.
Test: `test_status_change_cannot_reuse_the_previous_reason`.

---

## 3. Actor attribution in SQLite, and why it is weaker than Postgres

**Decision.** `orders.write_actor` is a statement-scoped declaration:

1. Every `INSERT`/`UPDATE` must set it to `agent`, `script` or `ui`. If it is missing
   or unknown, a `BEFORE` trigger aborts.
2. An `AFTER` trigger copies the before and after values plus the actor into
   `audit_log`, then resets `write_actor` to `NULL`.
3. Because the column is `NULL` at rest, an `UPDATE` that forgets to declare an actor
   cannot silently inherit the previous writer's identity.
   Test: `test_actor_is_not_inherited_by_the_next_write`.

Append-only tables (`order_lines`, `stock_movements`) use a permanent `created_by`
column with the same validation.

**The honest limit.** SQLite has no users, roles or `GRANT`, so:

- **The actor is self-declared.** A script can write `write_actor = 'ui'` and the
  database cannot tell.
- **Anyone who can open the file can do anything:**
  - `DROP TRIGGER`;
  - rewrite `audit_log` after dropping its triggers;
  - edit the file directly.

This mechanism gives **traceability against mistakes and forgetful scripts**. It is
not a security boundary against a malicious local writer.

**What Postgres would do instead:**

- The agent connects as its own role, e.g. `wms_agent`, with
  `GRANT SELECT ON ...` and `GRANT UPDATE (status, status_reason, status_changed_at, notes) ON orders TO wms_agent`.
  The column allowlist becomes a permission, not a trigger.
- The audit trigger records `session_user` / `current_user` (or
  `current_setting('role')`). The identity comes from the authenticated session, so the
  writer cannot choose it without that role's credentials.
- The audit table is owned by a different role, with no `UPDATE`/`DELETE` granted to
  anyone who writes orders.
- Row-level security can additionally scope an agent to one merchant's orders.

The August CRM had the first half of this: its audit trigger stored the Postgres role
and session user for every change, which is what made it possible to show afterwards
that the agent's credential had not changed any record. It did not have the second
half: the server did not use a dedicated database role, and the column allowlist lived
only in Python.
Moving the allowlist into the database (here as triggers, there as column grants) is
the main lesson carried over.

---

## 4. The column allowlist applies to every actor in the database

**Decision.** The triggers block price, quantity and address edits for `ui` and
`script` as well, not only for `agent`.

**Why.** In SQLite the actor is self-declared (decision 3). A per-actor rule would
mean "the agent can't change prices unless it says it is the UI". That looks like
a control but isn't one. In this demo schema, corrections to lines or addresses are
modelled as operations outside the agent's scope: cancel and re-create the order, or
post a stock adjustment. With Postgres roles the rule could be per-role.

---

## 5. Disambiguation: never pick

**Decision.**

- A numeric reference is an exact order id.
- A name is matched against customer and recipient, accent- and case-insensitively, as a
  substring.
- Several matches return `outcome: needs_clarification` with up to 10 candidates, and
  **no write happens**. Zero matches is an error.
- For **reads**, one match resolves.
- For **writes**, a name never resolves, not even when it matches a single order. The
  result is `outcome: needs_confirmation` with that order, and nothing is written. The
  agent must confirm with the user and call again with the id.
- Names shorter than 3 characters are rejected.
- With `WMS_WRITE_MODE=off`, write tools are rejected before any lookup.

**Why accent folding.** "Martinez" and "Martínez" are the same surname typed on
different keyboards. Folding widens the match, which *raises* ambiguity instead of
hiding it: in the seed, "Martínez" matches four orders, including one spelled without
the accent.

**Why never pick "the most recent".** It is right most of the time, and the time it
is wrong ships a stock incident to the wrong customer.

**Why a unique name is not enough for a write.** Substring matching finds "Ana" inside
"Mariana" and "Soler" inside "Javier Martínez Soler". A single match can still be the
wrong person: someone not yet in the WMS, or a different spelling. For a read, a wrong
match only costs a follow-up question. For a write, it changes someone else's order. The
extra round trip is the price.

Tests: `test_ambiguous_name_never_writes` (in `on` and `dry_run`),
`test_unique_name_match_still_needs_the_order_id`,
`test_ambiguous_name_returns_candidates_not_an_order`, and the eval scenarios
`ambiguous_name_write` and `unique_name_needs_confirmation`.

---

## 6. Read by default, write opt-in, dry run that runs the real thing

**Decision.** `WMS_WRITE_MODE=off|dry_run|on`, default `off`:

- **`off`** rejects write tools with a message telling the agent not to retry.
- **`dry_run`** runs the actual `UPDATE` inside a transaction, reads the row back,
  computes the diff and **rolls back**. Triggers and constraints run, so a dry run
  cannot pass something a real run would reject.
  Test: `test_dry_run_still_runs_the_database_triggers`.
- **`on`** commits and returns the `audit_id`.
- **Any other value** (`ON`, `true`, `1`) aborts startup with exit code 2.

Read tools open the database with `mode=ro`, so their read-only status is a property
of the connection, not a promise in the code. A missing database file is an error,
never a silently created empty warehouse.

---

## 7. Reject, don't repair

**Decision.** Invalid input is rejected, never adjusted:

- an unknown status;
- a status with different case or stray spaces (`Stock_Issue`, `" pending"`);
- an empty or over-long reason (over 300 characters), or one that repeats the current
  reason;
- an unknown write mode;
- an extra tool argument.

Nothing is truncated or fuzzy-matched.

**Why.** Coercion hides the fact that the caller was wrong. If the model invents
`lost_in_transit`, mapping it to the "closest" real status writes a guess into the
system of record. An error sends the problem back to the agent, which can then ask the
user. The August CRM server truncated long reasons silently; this version doesn't.

---

## 8. Status transitions and physical events

**Decision.**

- Python validates transitions: a delivered or cancelled order has no next status, and
  a picking order cannot jump back to pending.
- The agent can never set `shipped` or `delivered`. Those are physical events
  confirmed by a dock scan or a person.
- The agent can never set `cancelled` either. Cancelling is a commercial decision, and
  `cancelled` is final, so it cannot be undone through this server.
- A `script` actor can record all three.
- The check also applies inside the low-level helper that tests use to reach the
  database checks (`_write_order_fields`). That helper is private and not exposed as a
  tool.

**Why `destructiveHint=False` stays on the write tools.** With `cancelled`,
`shipped` and `delivered` out of reach, every status the agent can set has a way back
(`stock_issue`/`address_issue` → `pending`), and notes only append. The hint describes
what the tool can actually do, not what the underlying table could do.

**Atomicity.** The order is resolved, the transition is validated and the `UPDATE` runs
inside one `BEGIN IMMEDIATE` transaction. The `UPDATE` is conditional on the status and
notes that were checked (`WHERE id = ? AND status = ? AND notes = ?`), and a row count
other than 1 is rejected. The first version read the row before opening the
transaction. A review reproduced a script cancelling an order in that window, after
which the agent moved the cancelled order to `stock_issue`.
Tests: `test_concurrent_writer_is_blocked_between_check_and_write`,
`test_conditional_update_refuses_a_stale_row`.

**Limit.** Transitions are **not** enforced in SQL (only the enum and the reason are),
so a script with raw SQL can make an invalid transition. It will, however, be recorded
in `audit_log` with its actor.

---

## 9. Untrusted text is returned inside an envelope

**Decision.** Free text written by people or other systems (notes, status reasons) is
returned as:

```json
{"trust": "untrusted", "source": "order_note",
 "content": "<untrusted-data>…</untrusted-data>"}
```

Any spelling of the markers inside the text is defused, so a note cannot close the
envelope early. The server's `instructions` tell the model to treat that content as
data.

Note metadata is self-declared by whoever wrote the note, so it is returned as
`claimed_at` / `claimed_by`. Values outside the timestamp format or the actor vocabulary
come back as `null` / `unknown`. A value inside the vocabulary is returned as claimed: a
script can write `"by": "ui"`. `audit_log` is the authority on who wrote what.
Test: `test_note_author_is_a_claim_and_audit_log_is_the_authority`.

**Limit.** This reduces prompt-injection risk; it does not remove it. A model can still
be persuaded by text inside an envelope. The real mitigation is structural: the write
surface is two narrow tools, off by default, with no shipping, pricing or deletion
capability. Customer and recipient names are not wrapped (they are short, structured
fields), which is a known gap.

---

## 10. Scope choices for a weekend build

- **Three business tables plus `audit_log`, as specified.** Notes are an append-only
  JSON-lines column on `orders` rather than a fourth table. JSON-encoding each entry
  means a note containing newlines cannot forge a second entry. A production schema
  would use an `order_notes` table.
- **Stock is a ledger.** On-hand per location is `SUM(qty_delta)`, and corrections are
  new rows. That is how the append-only rule stays simple.
- **stdio only.** No HTTP transport, so no auth surface to get wrong. A remote
  deployment would need a streamable-HTTP transport with authentication and per-client
  write modes.
- **Not included:**
  - a per-process write budget;
  - a Postgres port with real roles;
  - evaluation results against real models (the harness exists; see decision 11);
  - a UI.

  The `ui` actor exists in the schema and is exercised in tests through raw SQL only.

---

## 11. An agent loop and offline evals, with no model in CI

**Decision.** `src/wms_mcp/agent/` has a minimal loop (model → MCP tool calls → results
→ model) on top of the SDK's `ClientSession`, plus scenario-based evaluation
(`evals/scenarios.toml`, `wms-eval`).

- **The checks are model-agnostic.** They cover tools called or not called, outcomes
  seen, database state (full `orders` snapshot and `audit_log` actors) and phrases in
  the final answer. The same scenario grades a scripted run and a live one.
- **Offline runs use `ScriptedModel`.** It replays hand-written, synthetic turns and
  ignores tool results. Some scripts misbehave on purpose.
- **Live runs use `LiteLLMModel`.** One adapter covers the providers LiteLLM supports.
  LiteLLM is an optional extra and is not installed in CI.
- **The prompt is versioned** (`PROMPT_VERSION`) and written into every trace, so two
  live runs can be compared by prompt.

**Why.** A server-level guarantee ("an ambiguous name never writes") and an agent-level
behaviour ("the assistant asks which order") are different claims. The first one is
tested deterministically. The second one depends on the model, and the harness is how
it would be measured. Keeping API calls out of CI keeps the suite free, deterministic
and runnable without keys.

**Cost and limit.** An offline pass says nothing about a real model. Phrase checks on
the final answer are crude; a live evaluation would need several runs per scenario and
probably an LLM judge for the answer text. No live results are published here.

**Tests.** `test_agent_eval.py` runs every scenario through a real server subprocess. It
also runs two negative controls, which must fail: an agent that picks a Martínez order on
its own, and an agent that never answers. Finally, it checks the LiteLLM response parsing
with a fake `completion` function.

---

## 12. Tool-call log, and errors that don't leak

**Decision.**

- **Tool-call log.** With `WMS_TOOL_LOG` set, each call appends one JSON line: tool,
  outcome, error kind, latency, write mode and argument *names*. `wms-report`
  aggregates the log per tool.
- **Values are not logged.** They can contain customer names.
- **Early rejections are logged too.** A `FastMCP.call_tool` override records calls
  that FastMCP rejects before they reach a tool function (invalid arguments, unknown
  tool).
- **Two kinds of errors.** `WmsError` messages are written for the agent and returned
  verbatim. Any other exception is logged on stderr with a short incident id, and the
  client only receives a generic message. Before this change, FastMCP returned
  `str(exc)` for everything, including a `FileNotFoundError` carrying the absolute
  database path.

**Why.** "Analyse how the agent behaves" needs data. Outcome counts such as
`needs_clarification` versus `applied`, error rates per tool and latency are the first
numbers anyone asks for.

**Tests.** `test_tool_calls_are_logged_without_argument_values`,
`test_unexpected_errors_do_not_leak_internals`, `test_calllog.py`.
