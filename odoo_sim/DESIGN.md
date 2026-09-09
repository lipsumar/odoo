# Odoo Sim — Game Clock Design

Status: steps 1, 2 and 4 implemented; step 3 outstanding (see §7)
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

### 2.2.1 ...but the test cursor never queries the database

`odoo/tests/test_cursor.py:129-133` — `TestCursor.now()` returns
`datetime.now()` directly instead of issuing `SELECT now()`, and its
`commit` / `rollback` (`test_cursor.py:95,106`) do not reset `_now` the way
`Cursor` does (`sql_db.py:565,576`).

So inside any `TransactionCase` the SQL override of §4.1 has **no effect at
all** on `create_date` / `write_date`. `TestCursor.now()` has to be repointed
alongside `BaseCursor.now()`, or every test passes for the wrong reason.

This is also independent support for §4.2's choice to make the Python clock
authoritative: doing so makes the real cursor and the test cursor agree by
construction, rather than leaving them on two different clocks as upstream does.

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

Three exceptions, all benign:

- `addons/website_slides/models/slide_channel.py:485` — `clock_timestamp()`
  used purely as a randomness source for access tokens.
- `odoo/addons/base/models/res_users.py:1542` — `res_users_apikeys.create_date`
  is created with a DDL-level `DEFAULT (now() at time zone 'utc')`. **Column
  DEFAULT expressions resolve the function OID at DDL time**, so this table
  stays bound to whichever `now()` was visible when it was created.
  **Decided: leave as is** — API keys are infrastructure, not gameplay.
- `odoo/addons/base/data/base_data.sql:82-83` — the same DDL-level default,
  on the hand-written bootstrap tables. Harmless in practice: the ORM always
  passes `create_date` and `write_date` explicitly from `cr.now()`
  (`odoo/orm/models.py:4809-4811`), so the column default is only ever reached
  by the raw SQL inserts that run during database initialisation.

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

Changing `K` *between* runs is supported, and is the escape hatch that makes a
fixed rate liveable. Stop the server, re-anchor at the current game instant,
restart:

```bash
GAME_NOW=$(psql -d <db> -qtAc "SET search_path=public,pg_catalog; SELECT now() AT TIME ZONE 'UTC';")
odoo-bin sim_init -d <db> --rate <new> --game-start "$GAME_NOW" --force
```

Passing `--game-start` is not optional here. Without it `--force` re-anchors
`anchor_game` to *real* now, which rewinds game time to the present and
violates the monotonicity §3.1 relies on. **Known wart:** that should be the
default behaviour of `--force` on an existing world, not something the operator
has to remember.

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

Installation is a CLI command rather than a hook in database creation, because
a world is created on an existing, already-populated database:

```
odoo-bin sim_init -d <db> --rate 1440 [--game-start <iso>] [--force]
```

The `search_path` hook in `odoo/sql_db.py:376` is extended to sim databases, as
for faketime. **Sim mode is detected from the database itself** — a database is
a game world iff it has a populated `game_clock` table — rather than from an
environment variable or a config flag. One fewer thing to keep in sync, and it
makes the tests cheap. The result is cached per database per process; the row
is immutable, so it never needs invalidating.

Consequence worth remembering when testing: a server that was already running
when `sim_init` ran has cached "not a game world" and keeps serving real time
until it is restarted.

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
SQL clocks cannot disagree. `TestCursor.now()` (`odoo/tests/test_cursor.py:129`)
must be repointed for the same reason — see §2.2.1.

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
sets `SLEEP_INTERVAL = 60` real seconds, so scheduling jitter is `60 * K` game
seconds. Measured against a real server:

| `K` | one game day takes | cron jitter | verdict |
|---|---|---|---|
| 60 | 24 real min | 1 game hour | fine |
| 240 | 6 real min | 4 game hours | usable ceiling |
| 1440 | 1 real min | 1 game day | interval crons meaningless |

At `K = 1440` an hourly cron comes due every 2.5 real seconds but is polled once
a minute, so all 24 occurrences of a game day collapse into a single run (§5.2).
Event-driven crons (`_trigger`, which wakes the poller through `pg_notify`) are
unaffected at any rate.

**Until step 3 below is built, `K` above roughly 240 is not usable for anything
that depends on interval crons.**

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

### 5.1 `ir_cron.py:273` mixes clocks — hard bug, **fixed**

```python
if datetime.now() - oldest < MAX_FAIL_TIME:
```

`oldest` is derived from `nextcall` / `write_date`, which will be game time,
while `datetime.now()` is real time. Subtracting them is meaningless once the
clocks diverge: the delta goes negative, `BadModuleState` is raised
unconditionally, and every cron on the database stops silently.

Fixed by comparing against `cr.now()`, which is correct on real time too.

Two more instances of the same defect turned up in the same file and were fixed
with it — `_gc_cron_triggers` (`ir_cron.py:915`) and `_gc_cron_progress`
(`ir_cron.py:936`) both compared real `datetime.now()` against the game-time
columns `call_at` / `create_date`. Milder consequence: the garbage collectors
would silently never delete anything rather than stalling cron.

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

### 5.6 The clock never stops

Game time is derived from `pg_catalog.now()`, not from process uptime, so it
advances whether or not a server is running. At `K = 1440` a lunch break is two
game months and an overnight break is about 1.3 game years.

On the next start, everything that came due in the meantime fires within one
poll — collapsed to a single run each by §5.2, so it is a burst rather than a
storm, but it is startling the first time.

This makes §3.1's "no pause" decision materially more expensive than it reads
on paper. The only lever available today is to re-anchor at the current game
instant (§3.2) before resuming, which skips the jump at the cost of discarding
the elapsed game time. A real pause is the `rate = 0` case that §3.1
deliberately deferred, and it brings back the mutable-clock machinery and the
timestamp-tie problem with it.

**Worth deciding deliberately before the world holds anything valuable.**

### 5.7 `_reschedule_later` replays every skipped interval — measured, benign

The `while nextcall <= now` loop (`ir_cron.py:647`) performs one `relativedelta`
addition and two timezone conversions per skipped interval, in-transaction. At
a fast clock a long-idle cron can rack up a lot of them. Measured at roughly
5 µs per iteration:

| scenario | iterations | in-transaction |
|---|---|---|
| hourly cron, 1 game year behind | 8,761 | 0.04 s |
| minutely cron, 1 game month behind | 43,201 | 0.21 s |
| minutely cron, 1 game year behind | 525,601 | 2.6 s |

So the worst realistic case is a few seconds, not the stall the loop looks like
at first reading. **Not worth optimising** — recorded so the next person does
not have to measure it again.

### 5.8 `fields.Datetime.now()` cannot always resolve its database

It is a static method with no cursor, so `game_clock.current_clock()` resolves
the database from `threading.current_thread().dbname` — set by the HTTP
dispatcher (`odoo/http.py:2280`), the RPC layer (`odoo/service/model.py:116`),
the cron runner (`ir_cron.py:191`), `odoo-bin shell` and the server — and
otherwise falls back to the single entry in `config['db_name']`.

Where neither resolves — a bare thread, or a process pointed at several
databases — it **silently returns real time**. Harmless under §9's one world
per database with `-d <db>`, but it is the first thing that breaks if a sim
process is ever given more than one database. `cr.now()` is unaffected: it has
the cursor, and therefore the database.

### 5.9 DDL defaults bind at CREATE TABLE time

Verified on a live world: a table created while `search_path = public,
pg_catalog` binds a `DEFAULT now()` to `public.now()` permanently — game time —
whereas one created before the world existed stays on `pg_catalog.now()`.

So the bootstrap tables of §2.4 keep real-time defaults, but any table created
by a module installed *after* `sim_init` gets game-time defaults. Inconsistent,
and inert in practice: the ORM declares no SQL default for `create_date` /
`write_date` and always passes both explicitly. It matters only for raw SQL
inserts into tables carrying a hand-written default.

### 5.10 Per-cursor overhead on sim databases

`SET search_path` plus its `commit()` runs on every `Cursor` construction
against a game world, exactly as it does under faketime. The first cursor for a
given database in a process additionally pays one `to_regclass` lookup to decide
whether the database is a world at all. Both are negligible, noted for
completeness.

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

1. **Done.** `game_clock` table, generated `public.now()` function, `sim_init`
   CLI command, and the `search_path` hook in `odoo/sql_db.py` extended to sim
   databases.
2. **Done.** Core edits in `odoo/orm/fields_temporal.py`; `BaseCursor.now()`
   and `TestCursor.now()` repointed at the clock.
3. **Outstanding.** Run with `--max-cron-threads=0`; drive
   `IrCron._process_jobs` from the game loop. This is what lifts the `K <= 240`
   ceiling of §4.3, and is the obvious next piece of work.
4. **Partly done.** `ir_cron.py:273` fixed, along with two more instances of the
   same defect (§5.1). `MIN_DELTA_BEFORE_DEACTIVATION` not yet reconsidered
   (§5.3).
5. Audit gameplay crons for the batch-window issue (§5.2).
6. Sweep raw `datetime.now()` per module as each domain enters the game.
7. Decide what to do about the clock never stopping (§5.6). Either accept that
   a world ages while unattended, or reopen the variable-rate design for a
   `rate = 0` pause. **Decide before the world holds anything worth keeping.**
8. Make `--force` re-anchor at the current *game* instant by default when a
   world already exists (§3.2), instead of requiring `--game-start`.

Ordered by what actually blocks play: 3 lifts the `K <= 240` ceiling, 7 decides
whether a world can be left alone, 8 is a half-hour of ergonomics. 5 and 6 are
driven by observed behaviour, not worth doing speculatively.

Code as built: `odoo/game_clock.py` (the clock, the per-database cache and
`install()`), `odoo/cli/sim_init.py`, and edits to `odoo/sql_db.py`,
`odoo/tests/test_cursor.py`, `odoo/orm/fields_temporal.py` and
`odoo/addons/base/models/ir_cron.py` — 36 inserted lines across the five
existing files.

## 8. Testing

`freezegun` is already integrated at `odoo/tests/common.py:2737` and Odoo
wraps it with its own `freeze_time` class supporting test-class decoration.
`ODOO_FAKETIME_TEST_MODE` provides a working reference implementation of the
SQL-side override to model ours on.

Built, in `odoo/addons/base/tests/test_game_clock.py` (14 tests, registered in
`odoo/addons/base/tests/__init__.py`):

```bash
odoo-bin -d <db> --test-tags /base:TestGameClockMath,/base:TestGameClockDatabase,/base:TestGameClockCron --stop-after-init
```

- `TestGameClockMath` — the mapping itself under `freeze_time`, at
  `K ∈ {0.5, 1, 60, 1000}`: the multiplier, anchor offsets, monotonicity, and
  that a non-sim process is exactly real time.
- `TestGameClockDatabase` — that the generated `public.now()` implements the
  same formula as Python, that `create_date` / `write_date` carry game time,
  and that both cursor classes read the clock.
- `TestGameClockCron` — readiness on game time at `K = 3600`, and a regression
  test for §5.1.

Two things learned writing them:

- The SQL and Python clocks must be compared **at the same real instant**, by
  selecting `pg_catalog.now()` and `public.now()` in one query and feeding the
  former to `GameClock.game_at()`. PostgreSQL's `now()` is the *transaction*
  start time, and a `TransactionCase` transaction opens at `setUpClass`, so
  letting each side read its own wall clock compares two different instants.
- `freezegun` drives the real-time input of the clock, which is why
  `GameClock.now()` reads `datetime.now()` and never `time.monotonic()`. It
  cannot reach PostgreSQL, so the SQL agreement test must not run under it.

## 9. Decisions

Resolved during design review:

| Question | Decision |
|---|---|
| One world per database, or several? | **One world per database.** Multiple worlds means multiple databases. |
| Runtime rate changes? | **No.** `K` is fixed at world creation. See §3.1. |
| Distinct game epoch, or present-day? | **Present / near future.** `anchor_game ≈ anchor_real`. No separate epoch. |
| `res_users_apikeys` DDL default (§2.4)? | **Leave as is**, bound to real time. Infrastructure, not gameplay. |
| How is sim mode enabled? | **Detected from the database** — a populated `game_clock` table, cached per process. No env var, no config flag (§4.1). |
| Which clock is authoritative for `cr.now()`? | **Python.** The SQL function stays, for raw SQL in business queries, but it no longer stamps records. Forced by §2.2.1: the test cursor never queries the database. |

Note that with rate changes gone, the question of a rate changing mid-request
disappears. `cr.now()` remains cached per transaction (`odoo/sql_db.py:271`),
so a transaction still observes a single consistent instant — which is the
behaviour we want.
