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

**Partly reversed by §3.3.** The rate stays fixed, but deriving game time from
wall time turned out to mean a world ages while nobody is playing (§5.6), which
is not liveable for a game. §3.3 keeps the fixed rate and moves the clock to an
accumulator owned by the game loop. Two of the four bullets above survive.

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

### 3.3 Pausing: the loop owns the clock

§3.1 fixed the rate to avoid a mutable clock, and that bought real simplicity.
What it did not anticipate is that a clock derived from wall time keeps running
when nobody is playing (§5.6). Measured on `simdb`: 3.2 game years across one
unattended night, coming back with 17 of 18 active crons overdue. A game needs
to be pausable, so this is not a wart to document — it has to be fixed.

**Decision (reverses part of §3.1): game time is accumulated by the game loop,
not derived from wall time.** `K` stays fixed for the lifetime of the world;
what changes is that the clock only advances while something ticks it.

```sql
CREATE TABLE game_clock (
    id             boolean PRIMARY KEY DEFAULT true CHECK (id),
    game_now       timestamptz NOT NULL,      -- game time as of the last tick
    last_tick_real timestamptz NOT NULL,      -- real instant of that tick
    rate           double precision NOT NULL,
    paused         boolean NOT NULL DEFAULT false
);
```

A tick is one statement:

```sql
UPDATE game_clock
   SET game_now       = game_now
                      + LEAST(pg_catalog.now() - last_tick_real, INTERVAL '<max_gap>') * rate,
       last_tick_real = pg_catalog.now()
 WHERE NOT paused;
```

and readers interpolate between ticks with the same expression:

```sql
CREATE OR REPLACE FUNCTION public.now() RETURNS timestamptz AS $$
    SELECT CASE WHEN paused THEN game_now
                ELSE game_now
                   + LEAST(pg_catalog.now() - last_tick_real, INTERVAL '<max_gap>') * rate
           END
      FROM public.game_clock;
$$ LANGUAGE sql STABLE;
```

Three properties make this work, all verified rather than argued:

**Interpolation is exactly continuous across a tick.** Immediately before tick
`n` a reader computes `game_now(n-1) + (t - last_tick(n-1)) * K`; immediately
after, `game_now(n) + (t - T(n)) * K`. Substituting the update rule makes those
algebraically identical, so there is no jump at a tick boundary and no
monotonicity guard to write. §3.1's "monotonicity is free" survives intact.

**A restart is just a tick.** Because the update clamps its own elapsed term,
recovery after a crash, a `kill -9` or a laptop suspend is the same statement,
and it moves time forward by at most `max_gap * K`. Nothing special-cases
startup.

**Pausing has two forms and we need both.** `paused` is the explicit control
the player gets, and it is instantaneous. `max_gap` is the implicit one: it
caps what a dead loop can accrue, so a world freezes on its own when the
process is killed or the machine sleeps — overshooting by at most `max_gap * K`
before it binds.

`max_gap` must sit comfortably above the worst tick overrun. §5.11's
ten-second floor means one cron can hold a tick for ten real seconds or more,
so advancing the clock from the thread that runs `_process_jobs` would trip the
clamp on a busy tick and silently lose game time. **The loop therefore runs two
threads: a clock thread doing nothing but the UPDATE above on a short interval,
and a cron thread calling `_process_jobs`.** Keeping them independent is what
lets `max_gap` be a small multiple of the clock interval instead of a guess
about how long crons take.

#### What this costs

§3.1 listed four things the inlined clock bought. Two survive, two are spent:

| §3.1 claim | after §3.3 |
|---|---|
| Monotonicity is free | **survives** — continuity across ticks, clamped elapsed |
| No cross-worker sync | **survives** — the table is the single source, and nothing caches across a transaction |
| No table read per call | **spent** — `public.now()` reads one row |
| Python caches the constants forever | **spent** — `game_now` / `last_tick_real` must be re-read |

The Python cost is bounded by the cache that already exists:
`BaseCursor.now()` memoises into `self._now` and resets it on commit and
rollback (`sql_db.py:271`, `:577`, `:588`), so this is **one extra single-row
SELECT per transaction**, against a one-row table that lives in shared buffers.
`game_clock._clocks` may go on caching `rate` and the fact that a database is a
world; it may no longer cache the moving parts.

The cache-invalidation problem §3.1 feared does not come back. It was a
consequence of caching mutable values *across* transactions, and the fix is to
stop caching them rather than to invalidate them.

#### Liveness: the clamp needs an observer

The clamp detects that *the loop* stopped ticking, so only a reader that can
see `last_tick_real` move is able to use it. Anything holding a basis it
fetched once cannot: with the clamp it freezes after `max_gap` even though the
world is running fine, and without it, it runs away when the world stops. The
UI session measured both failures at `K = 1440`, `max_gap = 5s`; the unclamped
error reaches twenty game hours after fifty-five real seconds of silence.

There is no cleverer formula. A detached reader has to be **told**, so the
clock thread publishes a pulse carrying `(game_now, last_tick_real, paused)`,
and a client refreshes its basis from it and then clamps exactly as the server
does. The one constraint is **pulse interval < `max_gap`**, which is free given
`max_gap` is already a multiple of the clock interval.

`max_gap` therefore governs both sides: how long the database keeps advancing
without a tick, *and* how quickly a client notices a dead world. The constant
carries a comment saying so.

#### A world can be down while the database is up

A dead loop stops game time and stops nothing else: PostgreSQL is up, the web
threads are alive, authentication still passes. A player can act into a world
that cannot process the consequences — the records land with a frozen
`create_date` and no cron ever runs on them. Freezing the clock does not
prevent that; it is what makes it silent.

So the check belongs on the write path, not only in the client:

```python
if not game_clock.is_running(game_clock.clock_for(request.db, request.env.cr)):
    raise UserError("The world is not running.")
```

`is_running()` lives in `game_clock` rather than being re-derived per endpoint,
and returns True for a database that is not a game world, so guarding an
ordinary Odoo path with it is a no-op.

#### Consequence for the UI

`UI_DESIGN.md` hands the browser a basis and computes game time locally. The
shape survives — the browser runs the same interpolation — but the payload
becomes `(game_now, last_tick_real, rate, paused)`, refreshed from the pulse
above rather than fetched once. The client still never polls; it listens. When
the pulse stops for `max_gap` the UI locks its controls rather than merely
stopping its clock, for the reason in the previous section.

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

**Approach — decided.** The game loop is **its own process**, not a thread
inside a web server: `odoo-bin game_run -d <db>`, run with
`--max-cron-threads=0` so `cron_spawn` iterates `range(0)`
(`server.py:620-633`) and our own threads are the only ones firing crons. Any
other `odoo-bin` pointed at the world — an admin backend, say — must carry the
same flag so it cannot fire a cron behind the game's back.

The command lives in the game addon as `cli/game_run.py`, not in core:
`load_addons_commands` (`odoo/cli/command.py:68-85`) globs the addons path for
`*/cli/<command>.py`, so this needs **no upstream diff at all**. (`sim_init`
stays in core, because it runs against a database before the game addon
exists; `game_run` needs the addon anyway.)

It bootstraps with the serving form, `server.start(preload=[db])` *without*
`stop=True` — `stop=True` is the `odoo-bin shell` form, which returns before
ever listening (`server.py:659`, `:716`) — because the same process also serves
the UI over Odoo's own dispatcher. The UI is a view onto game state and a way
to take actions; it does not drive the loop's design.

Three threads then: Odoo's HTTP threads, a clock thread (§3.3), and a cron
thread doing

```python
# cron thread, every tick
from odoo.addons.base.models.ir_cron import IrCron
IrCron._process_jobs(db_name)
threading.current_thread().dbname = db_name   # _process_jobs deleted it (§5.8)
```

`_process_jobs` collects all ready jobs and runs each once per call
(`ir_cron.py:187-215`), which is exactly the tick semantics we want, so we
reuse Odoo's scheduling arithmetic rather than reimplementing it.

Two details that are easy to miss. The cron thread must set `type = 'cron'` and
a `start_time` on itself, or it never inherits the `limit_time_real_cron`
watchdog in `process_limit` (`server.py:509-535`) and a runaway game cron runs
forever. And with `max_cron_threads = 0` nothing runs `LISTEN cron_trigger`, so
`_trigger()`-based crons wait out a tick unless the loop keeps that
subscription itself — worth doing, since it is the mechanism event-driven game
logic will lean on.

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

### 5.6 The clock never stops — **resolved by §3.3**

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

**Decided:** the game needs a pause, so §3.3 moves the clock to a loop-owned
accumulator. The paragraphs above describe the behaviour that decision removes;
they are kept because they are what motivated it.

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

**And `_process_jobs` deletes it.** `ir_cron.py:191` sets
`threading.current_thread().dbname`, and the `finally` at `:211-213` removes it
unconditionally — it does not restore a previous value. So a game loop that
sets `thread.dbname` once at startup, the way `odoo/cli/shell.py:136` does,
loses it after the first tick; from then on `current_clock()` falls through to
`config['db_name']` and, if that is not exactly one database, silently returns
**real** time for the rest of the run.

Mitigation: always run the loop with `-d <db>`, and re-set `thread.dbname`
after each `_process_jobs` call. This fails silently and no existing test would
catch it, so the loop's test suite must assert that the clock still reads game
time *after* a tick. (Found by the UI design session.)

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

### 5.11 A cron can hold its worker for ten real seconds

`_run_job` (`ir_cron.py:499-503`) loops on `time.monotonic()`:

```python
while status is None and (
    loop_count < MIN_RUNS_PER_JOB                       # 10
    or time.monotonic() < env.context['cron_end_time']  # start + 10 real seconds
):
```

It exits when the job reports completion, or when **both** bounds are met
(`MIN_RUNS_PER_JOB` / `MIN_TIME_PER_JOB`, `ir_cron.py:33-34`). So any cron
using the progress API that still reports remaining work holds its worker for
at least ten real seconds — four game hours at `K = 1440`. Not an exotic path:
20 non-test files call `_commit_progress`, including `sale`, `account`,
`stock`, `mail_mail`, `sms` and `base_automation`.

Two consequences. The tick interval is a **lower bound** on cron resolution
rather than a guarantee — an overrunning tick starts the next one late, and no
smaller interval beats the floor. And the clock must not be advanced from the
thread that runs `_process_jobs` (§3.3).

### 5.12 Duration constants are denominated in game time

§5.3 flagged `MIN_DELTA_BEFORE_DEACTIVATION` as a one-off. It is not: **any
constant subtracted from a game instant and compared against a game-time column
is measured in game seconds**, and divides by `K` to become a real-world
window. The code is usually correct — it is using `fields.Datetime.now()`
exactly as it should — and the outcome is still surprising.

The one that bites first is the bus, found by the UI session while costing out
the pulse. `bus.bus._gc_messages` (`addons/bus/models/bus.py:97-108`):

```python
timeout_ago = fields.Datetime.now() - timedelta(seconds=gc_retention_seconds)
cr.execute("DELETE FROM bus_bus WHERE create_date < %s", (timeout_ago,))
```

`DEFAULT_GC_RETENTION_SECONDS` is 24 hours (`bus.py:26`), so the backlog a
reconnecting client can replay is **sixty real seconds at `K = 1440`**, six
real minutes at `K = 240`. A client offline longer than that has lost game
events permanently, which makes reconnection a resync rather than a replay.

`game_run` warns at startup when that window is under a real hour, and prints
the value to set for the window you actually want. It does not set it: which
worlds want which backlog is a game-design question, not something a loop
should decide.

Known members of this family so far:

| constant | where | real window at `K = 1440` |
|---|---|---|
| `bus.gc_retention_seconds` (24h) | `bus.py:26` | 60 seconds |
| `MIN_DELTA_BEFORE_DEACTIVATION` (7d) | `ir_cron.py:37` | 7 minutes |
| cron batch windows (§5.2) | per cron | varies |

Worth checking for this shape whenever a module enters the game, alongside the
raw `datetime.now()` sweep of §5.4 — which would not have caught any of
these, since none of them uses raw `datetime.now()`.

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
3. **Done.** `addons/odoo_sim/cli/game_run.py`: forces `max_cron_threads = 0`,
   loads the registry in the main thread, then runs a clock thread and a cron
   thread under Odoo's serving bootstrap. Lifts the `K <= 240` ceiling of §4.3.
4. **Partly done.** `ir_cron.py:273` fixed, along with two more instances of the
   same defect (§5.1). `MIN_DELTA_BEFORE_DEACTIVATION` not yet reconsidered
   (§5.3).
5. Audit gameplay crons for the batch-window issue (§5.2).
6. Sweep raw `datetime.now()` per module as each domain enters the game.
7. **Done.** §3.3's accumulator, in `game_clock.py` (`GameClock`, `tick()`,
   `set_paused()`, `is_running()`, TTL'd readings), the new table in
   `sim_init.py`, and `addons/odoo_sim/cli/sim_pause.py` as the control
   surface. The pulse is built too: the clock thread publishes
   `odoo_sim.pulse` on `odoo_sim.world` every tick, unconditionally.
8. **Done, as a consequence of §3.3.** `sim_init --force` carries the existing
   world's game instant across unless `--game-start` overrides it, so changing
   the rate between runs no longer rewinds the world.

With 3, 7 and 8 built, what remains is 4 (§5.3's deactivation threshold) and
5 and 6, all driven by observed behaviour rather than worth doing
speculatively — now widened by §5.12, which says what shape to look for.

**Test coverage:** 32 tests on the clock and `sim_init`, 18 on the loop and
the pulse (§8). What remains uncovered is the **threading itself** — tick
cadence, the `select`/`LISTEN` wiring, and the bootstrap in `game_run.run()`.
A clock thread that stalled or drifted would still emit well-formed pulses and
pass everything, so a client's lock is currently the only check that the loop
keeps time. Worth a harness if the threads grow any logic beyond "call this
every N seconds".

Code as built: `odoo/game_clock.py` (the clock, the per-database cache and
`install()`), `odoo/cli/sim_init.py`, and edits to `odoo/sql_db.py`,
`odoo/tests/test_cursor.py`, `odoo/orm/fields_temporal.py` and
`odoo/addons/base/models/ir_cron.py` — 36 inserted lines across the five
existing files. `MAIL.md` §3 adds one more: a guard in
`odoo/addons/base/models/ir_mail_server.py`, so that a world never opens an
SMTP connection, game addon or not.

## 8. Testing

`freezegun` is already integrated at `odoo/tests/common.py:2737` and Odoo
wraps it with its own `freeze_time` class supporting test-class decoration.
`ODOO_FAKETIME_TEST_MODE` provides a working reference implementation of the
SQL-side override to model ours on.

Built, in `odoo/addons/base/tests/test_game_clock.py` (27 tests, registered in
`odoo/addons/base/tests/__init__.py`):

```bash
odoo-bin -d <db> --test-tags /base:TestGameClockMath,/base:TestGameClockDatabase,/base:TestGameClockCron --stop-after-init
odoo-bin -d <db> -i odoo_sim --test-tags /odoo_sim --stop-after-init
```

The suite is run against **both** an ordinary database and one that is itself a
game world, for the reason in the last bullet below.

- `TestGameClockMath` — the mapping itself under `freeze_time`, at
  `K ∈ {0.5, 1, 60, 1000}`: the multiplier, game offsets, monotonicity, and
  that a non-sim process is exactly real time. Plus the §3.3 properties: that
  an untended clock freezes, that a paused one does not move, that clock skew
  cannot rewind it, that interpolation is continuous across a tick, and
  `is_running()`.
- `TestGameClockDatabase` — that the generated `public.now()` implements the
  same formula as Python — including when clamped and when paused — that
  `create_date` / `write_date` carry game time, that both cursor classes read
  the clock, and that `tick()` advances, clamps, respects a pause, and does not
  accrue the paused stretch on resume.
- `TestGameClockCron` — readiness on game time at `K = 3600`, a regression
  test for §5.1, and one for §5.8's `thread.dbname` deletion.
- `TestSimInitStartingInstant` — which instant a world is created at, and
  above all that `--force` carries an existing world's instant across instead
  of rewinding it to the present (§7-8).
- `TestWorldPulse` (in `addons/odoo_sim/tests/`) — that a pulse carries a
  basis a client can interpolate from, that `running` is decided server-side,
  and above all **that a paused world keeps pulsing**. That last one is the
  property the UI's lock rests on, and precisely what a plausible "only send on
  change" optimisation would delete, so it is a test and not only a comment.
- `TestTickHeadroom`, `TestRetentionWarning`, `TestCronTickBookkeeping` — the
  loop's arithmetic and, most importantly, that a cron tick puts back the
  `thread.dbname` that `_process_jobs` deleted (§5.8).

Behaviour worth testing was moved out of the CLI commands into
`addons/odoo_sim/pulse.py`, `addons/odoo_sim/loop.py` and
`sim_init.starting_instant()`, so that reaching it does not mean importing a
`Command` or starting a thread. What is left in the commands is argument
parsing and bootstrap.

Three things learned writing them:

- The SQL and Python clocks must be compared **at the same real instant**, by
  selecting `pg_catalog.now()` and `public.now()` in one query and feeding the
  former to `GameClock.game_at()`. PostgreSQL's `now()` is the *transaction*
  start time, and a `TransactionCase` transaction opens at `setUpClass`, so
  letting each side read its own wall clock compares two different instants.
- `freezegun` drives the real-time input of the clock, which is why
  `GameClock.now()` reads `datetime.now()` and never `time.monotonic()`. It
  cannot reach PostgreSQL, so the SQL agreement test must not run under it.
  (§3.3's `CACHE_TTL` is measured on `time.monotonic()` precisely so that
  freezegun does not expire readings under a test.)
- **Assert the regression, then reintroduce the bug and watch it fail.** The
  first version of `TestCronTickBookkeeping` passed an injected stand-in thread
  to `run_cron_tick`, and passed just as happily with the `thread.dbname`
  re-set deleted: `_process_jobs` reaches for
  `threading.current_thread()`, so the stand-in was never the object under
  test. Every regression test here has since been checked by putting its bug
  back. One of them — "the clock still reads game time after a tick" — only
  discriminates once `config['db_name']` is emptied, because §5.8's fallback
  would otherwise rescue it.
- **A test may not assume its own database is not a game world.** Developing
  this feature means having one to hand and running the suite against it, and
  three tests asserting *unchanged upstream* behaviour passed only because the
  database happened to be ordinary. They now say so explicitly, and the real
  cursor's SQL fallback skips on a world, because `search_path` sends even that
  fallback through `public.now()` (`sql_db.py:383-390`). The suite is run both
  ways.

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
| Game loop: a thread in the server, or its own process? | **Its own process** — `odoo-bin game_run`, hosted in the game addon so the upstream diff stays at zero (§4.3). |
| Does that process also serve the UI? | **Yes**, over Odoo's own dispatcher. The UI shows game state and takes player actions; it is a client of the loop, not a driver of its design. |
| Can a world be paused? | **Yes** (§3.3), reversing part of §3.1. Explicitly via a `paused` flag, and implicitly via a staleness clamp so a killed process or a sleeping laptop freezes the world instead of ageing it. |

Note that with rate changes gone, the question of a rate changing mid-request
disappears. `cr.now()` remains cached per transaction (`odoo/sql_db.py:271`),
so a transaction still observes a single consistent instant — which is the
behaviour we want.
