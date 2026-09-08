# Odoo Sim — Game Clock Design

Status: draft / exploratory
Branch: `odoo-sim`
Target: Odoo 19.0

## 1. Problem

We want to run a game world on top of a live Odoo instance, where the Odoo
database is the authoritative record of the simulated business. The first
obstacle is time.

Requirements:

1. The game runs on a **faster wall clock** than reality, and the rate can be
   changed at runtime (sped up, slowed down, paused).
2. Odoo must follow that clock. A manufacturing order created during the game
   must carry the **game** timestamp, not the real one. Every business-level
   "now" resolves to game time.
3. Odoo's **infrastructure** scheduling stays on real time. We do not want to
   destabilise worker lifetimes, HTTP timeouts, or connection management.
4. Because game time can run very fast, we want deterministic control over
   when cron jobs fire, rather than relying on a background poller.

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

**This is the mechanism we want.** The only thing missing is a rate: theirs is
a fixed offset.

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
  DEFAULT expressions resolve the function OID at DDL time**, so a table
  created before our override installs will stay bound to `pg_catalog.now()`.
  Acceptable (API keys are infrastructure, not gameplay), but noted.

Raw SQL `now()` *does* appear in reports and gc queries — e.g.
`addons/im_livechat/models/discuss_channel.py:521`,
`addons/calendar/models/calendar_alarm_manager.py:68`. These are shadowed
correctly by the override and will follow game time, which is what we want.

### 2.5 Raw Python `datetime.now()` remains

~357 `datetime.now(`, ~222 `date.today()`, ~36 `utcnow()`, ~101 `time.time()`
across `addons/`. These bypass the field helpers entirely. See §5.

## 3. The clock

A game clock is three values, not one:

```
game_now = anchor_game + (real_now - anchor_real) * rate
```

Changing speed means **re-anchoring**, never recomputing history:

```
anchor_game := game_now()
anchor_real := real_now()
rate        := new_rate
```

This keeps the clock continuous across rate changes. Pause is `rate = 0`.

### Invariant: the clock is monotonic non-decreasing

Never let game time move backwards. `write_date` ordering, optimistic
concurrency checks, and cron `nextcall` arithmetic all assume forward motion.
Rate changes as defined above are safe by construction; manual clock jumps
must be forward-only or the world needs a full reset.

With `rate = 0`, many operations produce identical timestamps. Where strict
ordering matters, either forbid writes while paused or advance by a small
epsilon per transaction.

### Storage

One row, so PostgreSQL and Python read the same source of truth:

```sql
CREATE TABLE game_clock (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    anchor_real timestamptz NOT NULL,
    anchor_game timestamptz NOT NULL,
    rate        double precision NOT NULL DEFAULT 1.0
);
```

Because both the SQL function and the Python helper read this row, multiple
workers agree automatically — no cross-process clock sync protocol needed.
Python may cache the row briefly; invalidate on write via the existing
registry signaling or a dedicated `NOTIFY` channel.

## 4. Design

### 4.1 PostgreSQL

```sql
CREATE OR REPLACE FUNCTION public.now() RETURNS timestamptz AS $$
    SELECT c.anchor_game + (pg_catalog.now() - c.anchor_real) * c.rate
    FROM game_clock c;
$$ LANGUAGE sql STABLE;
```

Notes:

- Use `pg_catalog.now()` (transaction start time), **not** `clock_timestamp()`.
  This preserves Odoo's documented "transaction's timestamp" semantics and
  keeps the function honestly `STABLE`.
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
sets `SLEEP_INTERVAL = 60` real seconds. At 100x that is 1.7 game hours of
scheduling latency.

**Approach:** run with `--max-cron-threads=0` to disable the built-in poller,
and have the game loop call `IrCron._process_jobs(db_name)` directly on each
real tick.

```python
# game loop, every real tick
from odoo.addons.base.models.ir_cron import IrCron
IrCron._process_jobs(db_name)
```

This gives deterministic ordering, pausability, and a single place to reason
about game progression — while still reusing Odoo's scheduling arithmetic
instead of reimplementing it. It also keeps the diff against upstream small.

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

At 1000x, a daily job fires once per ~86 real seconds rather than replaying 30
skipped runs. This is usually the desired game behaviour (no catch-up storm),
but it changes semantics: any cron written as *"runs daily, processes the last
24 hours"* will see a much wider window and may drop or double-count records.

**Action:** audit every gameplay-relevant cron for window assumptions.

### 5.3 Cron auto-deactivation fires quickly

`MIN_DELTA_BEFORE_DEACTIVATION = timedelta(days=7)` (`ir_cron.py:37`) is now
measured in game time. At 1000x that is roughly 10 real minutes before a
flaky cron is disabled. Consider making this threshold real-time.

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
raw `datetime.now()` calls, and supports rates (`FAKETIME="+0 x60"`, with
`FAKETIME_TIMESTAMP_FILE` + `FAKETIME_NO_CACHE=1` for runtime changes). This is
what Odoo's own `ODOO_FAKETIME_TEST_MODE` is built to pair with.

**Not chosen as the primary approach:**

- It does not cover PostgreSQL — the SQL function is still required.
- It warps *everything*: HTTP timeouts, session expiry, log timestamps, `bus`
  longpolling. Would need `FAKETIME_DONT_FAKE_MONOTONIC=1` at minimum.
- On macOS, SIP makes `DYLD_INSERT_LIBRARIES` fragile, pinning development to
  Linux/Docker.

**Kept as an escalation path** if per-module patching of raw `datetime.now()`
proves too costly.

## 7. Implementation order

1. `game_clock` table, `public.now()` function, and extend the `search_path`
   hook in `odoo/sql_db.py` to sim-enabled databases.
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

- A test that asserts `fields.Datetime.now()`, `cr.now()`, and SQL `now()`
  agree to within a tolerance at several rates.
- A test that the clock never moves backwards across a rate change.
- A cron test at high rate verifying `nextcall` advances correctly and jobs
  fire the expected number of times.

## 9. Open questions

- Should the clock be global to the database, or per-game-session (multiple
  worlds in one instance)? Current design assumes one world per database.
- How should long-running requests behave when the rate changes mid-request?
  `cr.now()` is cached per transaction, so a transaction sees a consistent
  instant — probably correct, needs confirmation against gameplay.
- Do we need a "game epoch" distinct from real dates, or is the game world
  simply set in the present/near future? Affects fiscal years, sequences, and
  demo data.
- Should `res_users_apikeys` (§2.4) be recreated post-override for
  consistency, or explicitly left on real time?
