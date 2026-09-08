# Driving Every Odoo Timestamp from a Game Clock

**Feasibility study — Odoo 19.0**

> Preliminary analysis for a game that attaches a small "world" to Odoo, where the
> game runs its own accelerated wall clock (target ratio **1 min = 1 day ≈ 1440×**).
> Goal: make every timestamp Odoo *writes* — and every deadline it *acts on* —
> follow the game clock, while keeping the Odoo source tree as untouched as possible
> (monkeypatch-first).

---

## Bottom line

**Yes — controlling every Odoo timestamp is feasible, and the cleanest route needs
zero changes to the Odoo source.**

Odoo draws time from three places. Two of them (the ORM helpers and the DB cursor)
are trivially monkeypatchable from Python. The third (raw `datetime.now()` calls and
the PostgreSQL server clock) is best handled by virtualizing the OS clock underneath
the whole stack with `libfaketime`.

**Recommended path:** run the Odoo *and* PostgreSQL processes under an accelerated
`libfaketime` clock, plus a small (~40-line) addon that exposes a game-control API and
overrides the browser clock. This satisfies "control every timestamp" without editing
a single core file.

---

## The core finding: Odoo reads "now" from three clocks

Every timestamp in the system ultimately resolves to one of three sources. Any
complete solution has to reach all three — controlling one and missing another
produces records that disagree with themselves (e.g. a row whose `create_date` lands
on a different game-day than its own business dates).

| Layer | Source | Notes |
|------|--------|-------|
| **1. Python wall clock** | `datetime.now()`, `date.today()`, `time.time()` | C-level stdlib, called directly all over the addons. Bypasses every Odoo abstraction. Hardest to patch cleanly from Python. |
| **2. ORM helpers** | `fields.Datetime.now`, `Date.today`, `Date.context_today` | Plain `@staticmethod`s on Python classes. The idiomatic way addons ask for "now". Reassignable in one line. |
| **3. PostgreSQL clock** | `cr.now()`, raw SQL `now()`, `DEFAULT now()` | Feeds `create_date`/`write_date` on every row. Server-side, so unreachable from Python monkeypatching. Drives cron's "which jobs are due" query. |

All three converge on **one timestamp written to (or compared in) the database.**

---

## Evidence: where the time actually enters

Traced through the 19.0 tree. Counts are occurrences across `addons/`; they show how
much surface bypasses the ORM helpers (and therefore any Python-only patch).

| Entry point | Layer | Sites | Patchable in Python? |
|-------------|:-----:|:-----:|----------------------|
| `fields.Datetime.now` & `Date.today` — `now()` just calls `datetime.now()`  <br><sub>`orm/fields_temporal.py:197,112,131`</sub> | 2 | ~1466 | **Yes** — 1 line |
| `cr.now()` → `create_date` / `write_date`; runs `SELECT now() AT TIME ZONE 'UTC'`, cached per-transaction  <br><sub>`sql_db.py:271` · `orm/models.py:4809-4811,4402`</sub> | 3→2 | core | **Yes** — patch cursor |
| Direct `datetime.now()` / `date.today()` in addon logic  <br><sub>e.g. `ir_cron.py:273,910,931`</sub> | 1 | ~811 | Only via `ctypes` |
| Raw SQL `now()` in queries & views (reports, GC, livechat, CRM)  <br><sub>e.g. `crm_lead.py:1729`</sub> | 3 | ~30 | No — server-side |
| `time.time()` / monotonic (sessions, bus, cron loop pacing, timeouts)  <br><sub>`service/server.py`</sub> | 1 | ~108 | Don't — leave real |
| Browser clock (luxon) — date widgets default to `DateTime.now()`  <br><sub>`web/…/core/l10n/dates.js`</sub> | — | client | **Yes** — `Settings.now` |

**Favorable surprise:** addons never use the `CURRENT_TIMESTAMP` keyword — only the
`now()` function (0 occurrences of `CURRENT_TIMESTAMP` in `addons/`). This matters for
whichever DB-level strategy you pick, because `now()` can be shadowed but the keyword
cannot.

---

## Two useful facts about the plumbing

- **Everything is UTC internally.** On boot, `odoo/_monkeypatches/patch_init()` sets
  `os.environ['TZ']='UTC'` and calls `time.tzset()`. Datetimes are stored naive-UTC;
  only display converts to `env.tz`. So the game clock is a single UTC quantity — no
  timezone maze to fight.

- **The transaction timestamp is cached and consistent.** `cr.now()` resolves once per
  transaction and is reset to `None` on every `commit()` / `rollback()`
  (`sql_db.py:565,576`). All rows written in one transaction share one timestamp —
  convenient, and it means a patched cursor stays internally consistent.

- **Odoo ships a first-class monkeypatch framework.** `odoo/_monkeypatches/` registers
  import hooks that patch modules as they load — and `_cpython.py` even rewrites C-type
  method tables via `ctypes`. So patching builtins is a technique Odoo itself uses and
  blesses; it's available if you ever truly need to reach Layer 1 from Python.

---

## Three ways to hold the clock

### A — Virtualize the OS clock with `libfaketime`  *(recommended foundation)*

Launch both the Odoo and PostgreSQL processes under `LD_PRELOAD=libfaketime` with a
rate multiplier. Every `gettimeofday`/`clock_gettime` below Python and Postgres returns
accelerated time, so all three layers move together with no code aware of it.

- **For:** covers all three clocks at once, including raw SQL `now()` and stray
  `datetime.now()`; zero edits to Odoo (pure launch-environment change); pause / jump /
  re-rate at runtime via a re-read timestamp file.
- **Against:** requires controlling how Postgres is started (you do — custom instance);
  must *not* fake `CLOCK_MONOTONIC` or timeouts/keepalives distort; accelerates
  WAL/vacuum/log timestamps too (harmless in an isolated instance).

### B — Application-level monkeypatching  *(partial)*

A tiny addon reassigns `fields.Datetime.now`, `Date.today`, `Date.context_today` and
`BaseCursor.now` to return game time computed from a stored anchor. Idiomatic, surgical,
fully inside Odoo's own patch conventions.

- **For:** cleanly covers Layer 2 and the `create_date`/`write_date` path; explicit and
  inspectable; trivial to pause/rescale.
- **Against:** leaks — ~811 direct `datetime.now()` and ~30 raw SQL `now()` sites stay
  on real time; closing the leak means patching the C `datetime` type (invasive —
  touches logging/TLS/JWT); the Postgres server clock is simply unreachable this way.

### C — Shadow PostgreSQL `now()` in SQL  *(fallback only)*

Define a `game.now()` function reading an anchor table and put it ahead of `pg_catalog`
on the `search_path`, so unqualified `now()` calls resolve to game time. Viable because
addons never use `CURRENT_TIMESTAMP`.

- **For:** reaches the one layer Python can't, without `LD_PRELOAD`; catches raw SQL
  `now()` in reports and views.
- **Against:** fragile — shadowing a catalog builtin affects the whole DB session;
  doesn't touch any Python layer (still needs B alongside it); strictly worse than A,
  which solves the same layer more cleanly.

---

## Coverage at a glance

| Clock surface | A · libfaketime | B · monkeypatch | C · SQL shadow |
|---------------|:---------------:|:---------------:|:--------------:|
| `fields.Datetime.now` / `Date.today` | covered | covered | no |
| `create_date` / `write_date` (`cr.now()`) | covered | covered | covered |
| Direct `datetime.now()` in addons | covered | **leaks** | no |
| Raw SQL `now()` in reports/views | covered | **leaks** | covered |
| cron "which jobs are due" | covered | covered | covered |
| Browser date widgets | needs `Settings.now` | needs `Settings.now` | needs `Settings.now` |
| Odoo edits required | **none** | 1 addon | 1 addon + SQL |

---

## The one caveat that bites at 1440×: cadence, not correctness

Odoo's cron worker only *looks* for due jobs about once a real minute. It sleeps on
`SLEEP_INTERVAL = 60` real seconds between scans (`service/server.py:68`), then asks
the DB `WHERE nextcall <= now()` (`ir_cron.py:287`). Marking "now" is game time, so
*which* jobs run is correct — but at 1440×, a whole game-day can elapse between two
scans. A day's worth of scheduled actions will fire in one batch, all stamped within
the same game-minute.

**Mitigations:**

- lower `SLEEP_INTERVAL` (config-patchable), or
- drive cron by `NOTIFY` from the game engine when it advances the clock, or
- simply choose a gentler ratio for automated-worker realism.

This is a scheduling-granularity decision, not a blocker — decide it once you know how
fine-grained the in-game automation needs to feel.

---

## Recommendation: A + a thin addon (borrowing from B)

Use `libfaketime` as the foundation so no clock leaks, and add one small addon for the
two things the OS clock can't give you: a game-facing API to set/pause/rescale time, and
the browser override.

1. **Anchor + rate live in one place.** A timestamp file the game engine rewrites; the
   game's authoritative clock. Everything else derives from it.
2. **Launch Postgres and Odoo under libfaketime.** Fake the wall clock at the chosen
   rate; explicitly leave monotonic real so timeouts and keepalives behave.
3. **Ship a small `game_time` addon.** Optionally re-assert the ORM helpers as
   defense-in-depth, expose a controller/ORM method for the engine to jump or pause
   time, and inject `luxon.Settings.now` into the web client.
4. **Tune cron cadence to the ratio.** Pick the `SLEEP_INTERVAL` / `NOTIFY` strategy per
   the caveat above.

```bash
# 1 · Launch env — accelerate the wall clock, keep monotonic real
export LD_PRELOAD=/usr/lib/faketime/libfaketime.so.1
export FAKETIME_TIMESTAMP_FILE=/game/clock          # game engine rewrites this
export FAKETIME_NO_CACHE=1
export FAKETIME_DONT_FAKE_MONOTONIC=1                # timeouts stay on real time
# /game/clock e.g.  "@2035-01-01 00:00:00 x1440"  — edit to pause (x0) or jump
# start BOTH postgres and odoo-bin inside this environment
```

```python
# 2 · addons/game_time/__init__.py — defense-in-depth + engine hook
from odoo import fields
from odoo.sql_db import BaseCursor

# With libfaketime running, datetime.now() already returns game time,
# so these helpers are correct automatically. Re-assert only if you
# ever run Odoo WITHOUT the preload (tests, tooling):
def patch_module():
    pass  # no-op under libfaketime; add explicit overrides here if needed
```

Under libfaketime the Python and SQL layers need no patching at all — the addon exists
for the game-control API and the browser clock, not to chase timestamps.

---

## Open questions to settle next

- **Deployment shape.** Can you wrap the Postgres process in `LD_PRELOAD` in your
  target deploy (container entrypoint / systemd drop-in)? If Postgres is managed/hosted
  and you can't, fall back to B + C.
- **Does time ever need to rewind or pause?** libfaketime handles pause and
  jump-forward well; jumping *backward* is legal for the clock but can confuse cron's
  `nextcall` bookkeeping. Worth a spike.
- **Automation granularity.** How fine must in-game automated actions feel? That single
  answer decides the cron-cadence work and possibly the whole ratio.
- **Scope of "every timestamp."** Do mail/bus message ordering, session expiry, and JWT
  lifetimes need to be on game time too, or only business records? Those ride
  `time.time()`/monotonic and are best left real.
- **Validation harness.** Create a record, advance the clock a game-week, assert
  `create_date`, report dates, and a fired cron all agree. Cheap to build, and the
  definitive proof of "every timestamp."

---

<sub>Preliminary analysis · Odoo 19.0 source (branch `19.0`) · line references are
indicative and may shift across point releases. Verify libfaketime's monotonic and
sleep-scaling behavior against your build before relying on cadence numbers.</sub>
