# Odoo Sim — Game Clock Design

Status: draft / exploratory
Branch: `odoo-sim`
Target: Odoo 19.0

## 1. Problem

We want to run a game world on top of a live Odoo instance, where the Odoo
database is the authoritative record of the simulated business. The first
obstacle is time.

Requirements:

1. The game runs on an **accelerated wall clock** — a fixed multiplier `K`
   over real time, chosen once when the world is created.
2. Odoo must follow that clock. A manufacturing order created during the game
   must carry the **game** timestamp, not the real one. Every business-level
   "now" resolves to game time.
3. Odoo's **infrastructure** scheduling stays on real time. We do not want to
   destabilise worker lifetimes, HTTP timeouts, or connection management.
4. Because game time runs fast, we want deterministic control over when cron
   jobs fire, rather than relying on a background poller.

**Explicitly out of scope for v1:** changing the rate at runtime (speeding up,
slowing down, pausing). See §3.1 — this buys a large simplification and can be
added later without invalidating the design.

## 2. Findings from the 19.0 source

The investigation turned up better news than the raw grep counts suggest.

### 2.1 Odoo already ships a clock-override mechanism

`odoo/service/db.py:105` — `_check_faketime_mode()` replaces PostgreSQL's
`now()` with a SQL function in the `public` schema:

```python
cursor.execute("""
    CREATE OR REPLACE FUNCTION public.now()
        RETURNS timestamp with time zone AS $$
            SELECT pg_catalog.now() +  %s * interval '1 second';
        $$ LANGUAGE sql;
""", (int(time_offset), ))
```

`odoo/sql_db.py:376-378` makes the shadowing actually take effect, by putting
`public` ahead of `pg_catalog` on every cursor:

```python
if os.getenv('ODOO_FAKETIME_TEST_MODE') and self.dbname in tools.config['db_name']:
    self.execute("SET search_path = public, pg_catalog;")
    self.commit()  # ensure that the search_path remains after a rollback
```

This matters: PostgreSQL implicitly searches `pg_catalog` *first* unless it is
named explicitly in `search_path`. Without that `SET`, a `public.now()` would
never be reached. Odoo uses this in its own CI together with `libfaketime`.

**This is the mechanism we want.** Theirs is a pure offset; ours needs a
multiplier as well.

### 2.2 `create_date` / `write_date` have no SQL defaults

The magic columns are declared without a `default=` (`odoo/orm/models.py:283-292`)
and are populated in Python:

- `odoo/orm/models.py:4402` — `vals.setdefault('write_date', self.env.cr.now())`
- `odoo/orm/models.py:4809-4811` — `create_date` / `write_date` on create
- `odoo/orm/models.py:4542` — log vals

All of them go through `BaseCursor.now()` (`odoo/sql_db.py:271`), which is a
single method, cached per transaction:

```python
def now(self) -> datetime:
    """ Return the transaction's timestamp ``NOW() AT TIME ZONE 'UTC'``. """
    if self._now is None:
        self.execute("SELECT (now() AT TIME ZONE 'UTC')")
        ...
```

The cache is cleared on commit and rollback (`odoo/sql_db.py:565,576`).

**One hook covers the timestamps of every record in the system.**

### 2.3 The Python business-time surface is four functions

All in `odoo/orm/fields_temporal.py`:

| Function | Line | Addon call sites |
|---|---|---|
| `Datetime.now()` | 197 | ~668 |
| `Date.today()` | 112 | ~542 |
| `Datetime.today()` | 206 | — (delegates to `Datetime.now`) |
| `Date.context_today()` | 120 | — (uses `datetime.now()` internally) |

`Datetime.context_timestamp()` (line 211) only converts an existing value to
the user timezone; it needs no change.

### 2.4 Nothing in business code uses unshadowable SQL

`CURRENT_TIMESTAMP`, `LOCALTIMESTAMP`, `transaction_timestamp()` and
`statement_timestamp()` are SQL keywords/functions that `search_path` cannot
intercept. A full scan of `odoo/` and `addons/` found **zero** uses in business
code.

Two exceptions, both benign:

- `addons/website_slides/models/slide_channel.py:485` — `clock_timestamp()`
  used purely as a randomness source for access tokens.
- `odoo/addons/base/models/res_users.py:1542` — `res_users_apikeys.create_date`
  is created with a DDL-level `DEFAULT (now() at time zone 'utc')`. **Column
  DEFAULT expressions resolve the function OID at DDL time**, so this table
  stays bound to `pg_catalog.now()`. **Decided: leave as is** — API keys are
  infrastructure, not gameplay.

Raw SQL `now()` *does* appear in reports and gc queries — e.g.
`addons/im_livechat/models/discuss_channel.py:521`,
`addons/calendar/models/calendar_alarm_manager.py:68`. These are shadowed
correctly by the override and will follow game time, which is what we want.

### 2.5 Raw Python `datetime.now()` remains

~357 `datetime.now(`, ~222 `date.today()`, ~36 `utcnow()`, ~101 `time.time()`
across `addons/`. These bypass the field helpers entirely. See §5.4.

## 3. The clock

Three constants, fixed for the lifetime of the world:

| Constant | Meaning |
|---|---|
| `anchor_real` | real UTC instant at which the world was created |
| `anchor_game` | game UTC instant corresponding to `anchor_real` |
| `K` | game seconds per real second (e.g. `60`) |

```
game_now = anchor_game + (real_now - anchor_real) * K
```

The game world is set in the **present or near future**, so `anchor_game` is
`anchor_real` or slightly ahead of it. This keeps fiscal years, sequences, and
demo data in a plausible range and avoids needing a distinct game epoch.

### 3.1 Why fixing the rate simplifies so much

A changeable rate would require re-anchoring on every change, a mutable clock
row read by both Python and PostgreSQL, cross-worker invalidation of that row,
and explicit handling of `rate = 0` (paused) where timestamps tie. Making `K`
immutable removes all of it:

- **Monotonicity is free.** With `K > 0` and `pg_catalog.now()` advancing, game
  time cannot move backwards. No guard needed, and `write_date` ordering and
  optimistic concurrency checks are safe by construction.
- **No table read per call.** The constants are inlined into the SQL function.
- **No cross-worker sync.** Immutable values cannot drift between processes.
- **No cache invalidation.** Python reads the constants once per process and
  caches them forever.

Adding a variable rate later means reintroducing the anchor/rate row and
re-anchoring on change. Nothing in this design blocks that.

### 3.2 Storage

The authoritative values live in a one-row table, written once at world
creation:

```sql
CREATE TABLE game_clock (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    anchor_real timestamptz NOT NULL,
    anchor_game timestamptz NOT NULL,
    rate        double precision NOT NULL
);
```

This row is the source used to (a) generate the SQL function with the values
inlined, and (b) seed the Python-side constants at process start via a raw SQL
read (avoiding any dependency on the ORM during low-level `cr.now()`).

**The row is immutable.** Editing it does not take effect until the SQL
function is regenerated and workers are restarted. That awkwardness is
deliberate — it is not a runtime control surface.

One world per database. Multiple worlds means multiple databases.

## 4. Design

### 4.1 PostgreSQL

```sql
CREATE OR REPLACE FUNCTION public.now() RETURNS timestamptz AS $$
    SELECT TIMESTAMPTZ '<anchor_game>'
         + (pg_catalog.now() - TIMESTAMPTZ '<anchor_real>') * <K>;
$$ LANGUAGE sql STABLE;
```

Notes:

- Constants are inlined at world creation. No table lookup per call.
- Use `pg_catalog.now()` (transaction start time), **not** `clock_timestamp()`.
  This preserves Odoo's documented "transaction's timestamp" semantics and
  keeps the function honestly `STABLE`.
- `STABLE`, not `IMMUTABLE` — it depends on `pg_catalog.now()`.
- `interval * double precision` and `timestamptz + interval` are both valid.
- The override is **per database**. The `postgres` database that the cron
  runner connects to for `cron_database_list()` is untouched.

Installation mirrors `_check_faketime_mode`: extend the `search_path` hook in
`odoo/sql_db.py:376` to also trigger for sim-enabled databases, gated on our
own flag rather than `ODOO_FAKETIME_TEST_MODE`.

### 4.2 Python — edit core, do not monkeypatch

We are forking Odoo, so change `odoo/orm/fields_temporal.py` directly to route
`Datetime.now()`, `Datetime.today()`, `Date.today()` and `Date.context_today()`
through a clock hook.

**This is not a style preference — monkeypatching is unsafe here.**
`odoo/addons/base/models/ir_cron.py:118` does:

```python
nextcall = fields.Datetime(..., default=fields.Datetime.now, ...)
```

The `default` captures the *function object* at module import time. Any patch
applied after addon import is silently ignored by every field declared this
way. A core edit eliminates the ordering hazard entirely.

Also repoint `BaseCursor.now()` (`odoo/sql_db.py:271`) at the same Python clock
instead of issuing `SELECT now()`. Cheaper, and it guarantees the Python and
SQL clocks cannot disagree.

### 4.3 Crons — keep Odoo's arithmetic, take over the trigger

The original plan was to disable crons and fire them manually, reimplementing
scheduling. That is more work than necessary.

All cron date logic already flows through the two hooks above:

| Concern | Location | Source of time |
|---|---|---|
| readiness predicate | `ir_cron.py:294` | `cr.now()` |
| `_reschedule_later` | `ir_cron.py:640` | `cr.now()` |
| `_reschedule_asap` | `ir_cron.py:665` | `cr.now()` |
| `_clear_schedule` | `ir_cron.py:627` | `cr.now()` |
| interval arithmetic | `ir_cron.py:647-650` | `nextcall` + `context_timestamp` |

So with the clock in place, crons become due on game time **for free**.

What is genuinely broken is polling granularity. `odoo/service/server.py:68`
sets `SLEEP_INTERVAL = 60` real seconds. At `K = 100` that is 1.7 game hours of
scheduling latency.

**Approach:** run with `--max-cron-threads=0` to disable the built-in poller,
and have the game loop call `IrCron._process_jobs(db_name)` directly on each
real tick.

```python
# game loop, every real tick
from odoo.addons.base.models.ir_cron import IrCron
IrCron._process_jobs(db_name)
```

This gives deterministic ordering and a single place to reason about game
progression — while still reusing Odoo's scheduling arithmetic instead of
reimplementing it. It also keeps the diff against upstream small.

`_process_jobs` collects all ready jobs and runs each once per call
(`ir_cron.py:187-215`), which is exactly the tick semantics we want.

## 5. Known issues and risks

### 5.1 `ir_cron.py:273` mixes clocks — hard bug

```python
if datetime.now() - oldest < MAX_FAIL_TIME:
```

`oldest` is derived from `nextcall` / `write_date`, which will be game time,
while `datetime.now()` is real time. Subtracting them is meaningless once the
clocks diverge. **Must patch.**

### 5.2 Missed cron occurrences are collapsed

`ir_cron.py:647`:

```python
while nextcall <= now:
    nextcall += interval
```

A daily job fires once per `86400 / K` real seconds rather than replaying every
skipped run. This is usually the desired game behaviour (no catch-up storm),
but it changes semantics: any cron written as *"runs daily, processes the last
24 hours"* will see a much wider window and may drop or double-count records.

**Action:** audit every gameplay-relevant cron for window assumptions. This
risk scales with `K` and does not go away with a fixed rate.

### 5.3 Cron auto-deactivation fires quickly

`MIN_DELTA_BEFORE_DEACTIVATION = timedelta(days=7)` (`ir_cron.py:37`) is now
measured in game time — real-world `604800 / K` seconds. At `K = 1000` that is
roughly 10 real minutes before a flaky cron is disabled. Consider making this
threshold real-time.

### 5.4 Raw `datetime.now()` in addons

~580 call sites bypass the field helpers. **Deliberately left on real time
initially.** Most live in `mail` / `bus` / `http` plumbing where real time is
arguably correct. Fix per module as each business domain is brought into the
game, driven by observed behaviour rather than a blanket sweep.

### 5.5 Infrastructure that must stay on real time

- Worker age limits use `time.monotonic()` (`odoo/service/server.py:569-570`) —
  already safe.
- `limit_time_real`, HTTP timeouts, connection pooling — untouched.
- Log timestamps — should remain real time for debuggability.

## 6. Alternative considered: libfaketime

`LD_PRELOAD`-ing libfaketime catches everything in Python, including all the
raw `datetime.now()` calls, and supports a rate multiplier
(`FAKETIME="+0 x60"`). This is what Odoo's own `ODOO_FAKETIME_TEST_MODE` is
built to pair with.

**Not chosen as the primary approach:**

- It does not cover PostgreSQL — the SQL function is still required.
- It warps *everything*: HTTP timeouts, session expiry, log timestamps, `bus`
  longpolling. Would need `FAKETIME_DONT_FAKE_MONOTONIC=1` at minimum.
- On macOS, SIP makes `DYLD_INSERT_LIBRARIES` fragile, pinning development to
  Linux/Docker.

**Kept as an escalation path** if per-module patching of raw `datetime.now()`
proves too costly.

## 7. Implementation order

1. `game_clock` table, generated `public.now()` function, and extend the
   `search_path` hook in `odoo/sql_db.py` to sim-enabled databases.
2. Core edit in `odoo/orm/fields_temporal.py`; repoint `BaseCursor.now()`.
3. Run with `--max-cron-threads=0`; drive `IrCron._process_jobs` from the game
   loop.
4. Fix `ir_cron.py:273`; reconsider `MIN_DELTA_BEFORE_DEACTIVATION`.
5. Audit gameplay crons for the batch-window issue (§5.2).
6. Sweep raw `datetime.now()` per module as each domain enters the game.

## 8. Testing

`freezegun` is already integrated at `odoo/tests/common.py:2737` and Odoo
wraps it with its own `freeze_time` class supporting test-class decoration.
`ODOO_FAKETIME_TEST_MODE` provides a working reference implementation of the
SQL-side override to model ours on.

Worth building early:

- A test that `fields.Datetime.now()`, `cr.now()`, and SQL `now()` agree to
  within a tolerance, at several values of `K`.
- A test that game time advances by approximately `K` seconds per real second.
- A cron test at high `K` verifying `nextcall` advances correctly and jobs fire
  the expected number of times.

## 9. Decisions

Resolved during design review:

| Question | Decision |
|---|---|
| One world per database, or several? | **One world per database.** Multiple worlds means multiple databases. |
| Runtime rate changes? | **No.** `K` is fixed at world creation. See §3.1. |
| Distinct game epoch, or present-day? | **Present / near future.** `anchor_game ≈ anchor_real`. No separate epoch. |
| `res_users_apikeys` DDL default (§2.4)? | **Leave as is**, bound to real time. Infrastructure, not gameplay. |

Note that with rate changes gone, the question of a rate changing mid-request
disappears. `cr.now()` remains cached per transaction (`odoo/sql_db.py:271`),
so a transaction still observes a single consistent instant — which is the
behaviour we want.
