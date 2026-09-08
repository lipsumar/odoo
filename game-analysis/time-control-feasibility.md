# Driving Business-Record Timestamps from a Game Clock

**Feasibility study — Odoo 19.0**

> Preliminary analysis for a game that attaches a small "world" to Odoo, where the
> game runs its own accelerated wall clock (target ratio **1 min = 1 day ≈ 1440×**).
> Goal: make the timestamps on **business records** (the data the game world cares
> about — orders, leads, tasks, etc.) follow the game clock, while everything Odoo
> uses to run *itself* — cron scheduling, sessions, security tokens, logs, outbound
> integrations — stays on real time. Keep the Odoo source tree as untouched as
> possible (monkeypatch-first).

---

## Bottom line — revised

**Controlling business-record timestamps only is feasible without touching Odoo's
source, but it rules out the OS-clock-virtualization approach this analysis
originally recommended.**

The first version of this document recommended running Odoo and PostgreSQL under an
accelerated `libfaketime` clock (Option A below) because it's the one technique that
reaches all three clock layers — Python, ORM, and the PostgreSQL server — at once.
That is exactly the problem now: "all three layers at once" fakes cron's own
scheduling clock, session and security-token expiry, and every outbound
integration's sense of "now" right along with the business data. Since the
requirement is that crons keep running in real time and *only* business records
move, whole-process clock virtualization is disqualified — see Option A under
"Three ways to hold the clock" below.

**Recommended path (revised):** leave the OS clock, the PostgreSQL server clock, and
Odoo's own bookkeeping columns (`create_date`/`write_date`) untouched. Add a small
addon that stamps game time onto a small, explicit allow-list of business models —
via dedicated fields, not by reassigning the global `fields.Datetime.now` /
`Date.today` helpers everyone else in the codebase also calls. See "Recommendation"
below.

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

### A — Virtualize the OS clock with `libfaketime`  *(ruled out)*

Launch both the Odoo and PostgreSQL processes under `LD_PRELOAD=libfaketime` with a
rate multiplier. Every `gettimeofday`/`clock_gettime` below Python and Postgres returns
accelerated time, so all three layers move together with no code aware of it.

- **For:** covers all three clocks at once, including raw SQL `now()` and stray
  `datetime.now()`; zero edits to Odoo (pure launch-environment change); pause / jump /
  re-rate at runtime via a re-read timestamp file.
- **Against — disqualifying, given the "crons stay real" requirement:** cron's own
  due-job query compares `nextcall <= cr.now()` (`ir_cron.py:287-294`), and `cr.now()`
  is exactly the DB-server clock this option fakes (`sql_db.py:271-275`, `SELECT now()
  AT TIME ZONE 'UTC'`). Under Option A, cron scheduling itself runs on game time — the
  opposite of what's needed. There is no way to fake "the OS clock" for business data
  only; it's below the ORM, so it can't distinguish a `sale.order` row from an
  `ir.cron` row.
- **Against — new risk, not in the original draft:** the whole process's wall clock
  would be skewed up to 1440× from the outside world. Anything that validates a
  timestamp against a real-time tolerance window breaks: payment-provider webhook
  signatures, OAuth/JWT `exp` claims, request-signing schemes (e.g. AWS SigV4), and TLS
  certificate validity checks. This isn't a tuning problem like the cron cadence
  caveat — it's outright incompatible with talking to any real external system.
- **Against (as before):** requires controlling how Postgres is started; accelerates
  WAL/vacuum/checkpoint timestamps too, which breaks correlating Odoo/Postgres logs
  with real-world incident timelines even in an "isolated" instance.

### B — Application-level monkeypatching  *(too broad as originally scoped)*

A tiny addon reassigns `fields.Datetime.now`, `Date.today`, `Date.context_today` and
`BaseCursor.now` globally to return game time computed from a stored anchor. Idiomatic,
surgical, fully inside Odoo's own patch conventions — but "globally" is the problem now.

- **For:** cleanly covers Layer 2 and the `create_date`/`write_date` path; explicit and
  inspectable; trivial to pause/rescale.
- **Against — leaks into system internals, not just business records:**
  `ir.cron.nextcall` itself defaults via `fields.Datetime.now` (`ir_cron.py:118`), so a
  blanket patch would stamp *newly scheduled cron jobs* with game time. The same
  helpers back portal/password-reset token expiry, `ir.attachment` access-token expiry,
  and login-throttling windows — none of which should move. Reassigning the function
  globally can't be scoped to "business records only"; only reassigning it per-model
  (see Recommendation) can.
- **Against (as before):** ~811 direct `datetime.now()` and ~30 raw SQL `now()` sites
  stay on real time regardless; the Postgres server clock is unreachable this way.

### C — Shadow PostgreSQL `now()` in SQL  *(ruled out, same reason as A)*

Define a `game.now()` function reading an anchor table and put it ahead of `pg_catalog`
on the `search_path`, so unqualified `now()` calls resolve to game time. Viable because
addons never use `CURRENT_TIMESTAMP`.

- **For:** reaches the one layer Python can't, without `LD_PRELOAD`; catches raw SQL
  `now()` in reports and views.
- **Against:** shadows `now()` for the whole DB session — same "can't scope to
  business records" problem as Option A, just at the SQL layer instead of the OS
  layer; cron's own `nextcall <= now()` query would be affected identically to A;
  doesn't touch the Python layer (still needs B alongside it).

---

## Coverage at a glance

| Clock surface | A · libfaketime | B · global monkeypatch | C · SQL shadow | D · scoped model overrides |
|---------------|:---------------:|:-----------------------:|:---------------:|:---------------------------:|
| `fields.Datetime.now` / `Date.today` | covered | covered | no | covered, **only on allow-listed models** |
| `create_date` / `write_date` (`cr.now()`) | covered | covered | covered | **untouched (by design)** |
| Direct `datetime.now()` in addons | covered | leaks | no | not targeted (out of scope) |
| Raw SQL `now()` in reports/views | covered | leaks | covered | not targeted (out of scope) |
| cron "which jobs are due" (`nextcall <= cr.now()`) | **moves to game time — fails requirement** | **leaks into new `nextcall` defaults** | **moves to game time — fails requirement** | **stays real ✓** |
| Sessions / password-reset / attachment tokens | **moves to game time** | **leaks** | untouched | **stays real ✓** |
| External integrations (webhooks, OAuth, TLS) | **breaks — outside real-time tolerance** | unaffected | unaffected | unaffected |
| Browser date widgets | needs `Settings.now` | needs `Settings.now` | needs `Settings.now` | needs `Settings.now`, scoped to game views |
| Odoo edits required | none | 1 addon | 1 addon + SQL | 1 addon |

Option D (scoped model overrides) is the only column with no row that fails the
"business records only, system internals stay real" requirement.

---

## Cron, revisited: it must not see game time at all

The first draft treated cron's relationship to game time as a cadence-tuning problem
(a day's worth of jobs batching into one poll at 1440×). That framing assumed cron
would run *on* game time, which is no longer the goal. Under Option D, cron is left
completely alone: `SLEEP_INTERVAL = 60` real seconds (`service/server.py:68`) and the
`nextcall <= cr.now()` due-job query (`ir_cron.py:287-294`) both keep reading the real
PostgreSQL clock, because nothing about Option D touches `cr.now()` or the OS clock.
Cron polls every real minute and fires jobs at their real scheduled time, exactly as
it does today — no batching artifact to mitigate.

**The remaining nuance is one-directional, not a clock problem:** once a business
record can carry a game-time value (e.g. a deadline field on a `sale.order`), any
cron-driven automation that reads that field and compares it to "now" needs to compare
it against *game* now, not real now, to decide whether it's due — otherwise a
game-time deadline is measured against the wrong clock and never lines up. That
comparison has to happen explicitly inside the business logic that reads the field
(e.g. call the addon's `game_now()` helper), not by changing what cron itself
considers "now". This is a per-feature integration detail, not a platform-level
blocker.

---

## Recommendation: D — scoped model overrides, addon-only

Nothing about the OS clock, the PostgreSQL server clock, or Odoo's own bookkeeping
columns (`create_date`/`write_date`) gets touched. Game time is stamped only onto an
explicit allow-list of business models, through fields the addon owns.

1. **Anchor + rate live in one place.** A `game.clock` singleton model (or config
   parameters) holding the anchor timestamp and rate; a `game_now()` helper computes
   game time from it on demand. Nothing patches global time functions.
2. **Define the allow-list explicitly.** A mixin, e.g. `game.time.mixin`, added only to
   the models the game world actually needs (`sale.order`, `crm.lead`, `project.task`,
   …). Everything not on the list — `ir.cron`, `res.users`, `mail.message`,
   `ir.attachment`, sessions — is structurally unable to see game time.
3. **Stamp game time into dedicated fields, not `create_date`/`write_date`.** The mixin
   adds `game_create_date` / `game_write_date` (or whatever the game logic needs) set in
   `create()`/`write()` overrides. The real audit columns stay real, so anything in core
   that depends on `write_date` for its own bookkeeping is unaffected.
4. **Any cron-driven logic that reads a game-time field must convert explicitly.**
   Compare against `game_now()`, not `fields.Datetime.now()`, at the point of use — see
   "Cron, revisited" above.
5. **Browser/game-facing views only.** Inject a game-time display (e.g. via
   `luxon.Settings.now` scoped to specific views/widgets, or simply render the
   `game_*` fields directly) rather than touching the client's global clock.

```python
# addons/game_time/models/game_time_mixin.py
from odoo import models, fields, api
from .game_clock import game_now  # anchor + rate live here; no global patches

class GameTimeMixin(models.AbstractModel):
    _name = "game.time.mixin"
    _description = "Stamps game-clock timestamps on opted-in business models"

    game_create_date = fields.Datetime(readonly=True)
    game_write_date = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.write({"game_create_date": game_now(), "game_write_date": game_now()})
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("skip_game_time_stamp"):
            super(GameTimeMixin, self).write({"game_write_date": game_now()})
        return res
```

```python
# addons/sale/models/sale_order.py — opt-in, one line per model
class SaleOrder(models.Model):
    _name = "sale.order"
    _inherit = ["sale.order", "game.time.mixin"]
```

`ir.cron`, sessions, tokens, `create_date`/`write_date`, and every external
integration never inherit `game.time.mixin`, so they never see game time. No core
file changes; the whole thing is one addon.

---

## Open questions to settle next

- **Which models/fields are actually "business records."** Needs a product decision
  (orders? leads? tasks? which date fields on each?) — drives the allow-list in step 2
  above.
- **Do game-time fields shadow existing business date fields, or live alongside
  them?** E.g. does `sale.order.date_order` itself move to game time (bigger blast
  radius, touches existing reports/domains that filter on it), or does the game only
  read a new `game_*` field (safer, but means existing Odoo UI still shows real
  dates)? This changes the shape of step 3 significantly and should be pinned down
  before implementation.
- **Does time ever need to rewind or pause?** Straightforward with an anchor+rate
  model (rewrite the anchor row) since nothing OS-level is involved; jumping backward
  just needs the game-facing code to tolerate a decreasing `game_now()`.
- **Automation granularity.** For any cron job that reads a game-time field (per
  "Cron, revisited"), how often does it need to poll to feel responsive in-game? This
  is now a per-feature decision, not a platform-wide cadence tune.
- **Validation harness.** Create a record on an allow-listed model, advance the game
  clock a game-week, assert `game_create_date`/`game_write_date` moved while
  `create_date`/`write_date` and cron's own `nextcall` did not. That inequality *is*
  the proof the design holds "business records only."

---

<sub>Preliminary analysis · Odoo 19.0 source (branch `19.0`) · line references are
indicative and may shift across point releases. Revised after initial review to
scope time control to business records only, per the requirement that cron and other
system internals must keep running on real time — see review discussion on this
PR.</sub>
