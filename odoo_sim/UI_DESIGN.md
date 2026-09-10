# Odoo Sim — Materialising the Game

Status: design; the clock it builds on is implemented, the UI is not
Branch: `odoo-sim`
Target: Odoo 19.0
Companion to `DESIGN.md` (the game clock), which is assumed throughout.

> **Revised.** The first draft recommended running the game inside the Odoo
> HTTP server as an addon. The game loop is now a **separate process**, and the
> UI backend rides in it. §3 and §4 are rewritten accordingly.
>
> The short version of what changed: the objection that ruled out a separate
> service was *transactional*, and it applies to a service that reaches Odoo
> over RPC. It does not apply to a process that **embeds the ORM**. A separate
> process that imports Odoo keeps every advantage of being an addon and adds
> lifecycle isolation and a deterministic tick. It is a better answer than the
> one this document originally gave.
>
> **Second revision — pause, the pulse, and the lock.** `DESIGN.md` §3.3 makes
> game time an accumulator owned by the game loop rather than a function of wall
> time, so a world can be paused and does not age unattended. That reverses the
> premise §5.5 was built on: the local-interpolation design survives, the
> payload does not, and the clock now needs a pulse it did not need before.
> Fetching the time is fine — treating that as something to avoid was purism —
> but the pulse is what makes the client's clamp usable, and **when it stops,
> the UI locks**. §2.5, §5.5, §5.6 and §6.3 are rewritten.
>
> The clock half is no longer a proposal: `odoo/game_clock.py` on `odoo-sim`
> now implements it, and §2.5 records the API the UI backend builds on.
>
> The premise question of the first revision is settled: the separate process
> is confirmed, and the UI is "only a way to show the game state to the user and
> for the user to take some actions" — a client of the loop, not a driver of its
> design.
>
> The pulse is built, tested and verified live. The UI is not built.

## 1. Problem

The clock runs, but nothing shows it. We want a UI that renders the game
world — eventually a graphical environment in the Farmville mould, initially
just the game time ticking on a page.

What the thing we build has to be, in the end:

1. A **frontend** that renders game state, with real graphics ambitions.
2. A **backend** that reads and writes the Odoo database through the ORM, so
   that a game action and the business records it produces are one operation.
3. **Its own state** — inventory of the *game*, not of the warehouse — in
   tables that are the game's, not Odoo's.
4. All of it on a **single database**, alongside the Odoo tables.

Requirement 4 is not a constraint we have to work around; it is the whole
premise of the project. The Odoo database is the authoritative record of the
simulated business (`DESIGN.md` §1), so the game's own state belongs next to
it, in the same transaction scope.

The question this document answers is where the code lives.

## 2. Findings from the 19.0 source

### 2.1 Odoo serves static files raw, with no pipeline in the way

`odoo/http.py:2219` — any URL of the form `/<module>/static/<path>` is read
straight off disk and streamed back:

```python
def _serve_static(self):
    module, _, path = self.httprequest.path[1:].partition('/static/')
    directory = root.static_path(module)
    filepath = werkzeug.security.safe_join(directory, path)
    res = Stream.from_path(filepath, public=True).get_response(...)
```

No asset bundle, no database, no authentication. And the JS transpiler that
rewrites ES modules into Odoo's module system is gated on the path
(`odoo/tools/js_transpiler.py:725-728`):

```python
addon = url.split('/')[1]
if url.startswith(f'/{addon}/static/src') or url.startswith(f'/{addon}/static/tests'):
    return True
return bool(result)   # ...or an @odoo-module comment
```

So a bundle built by any toolchain we like, dropped in `static/dist/`, is
served byte-for-byte untouched. **We can host a completely foreign frontend
inside an Odoo addon without touching Odoo's asset pipeline at all.**

### 2.2 POS is the precedent for a standalone app

`addons/point_of_sale/controllers/main.py:36` routes `/pos/ui` outside the web
client, and `addons/point_of_sale/views/pos_assets_index.xml:5` is a
hand-written `<!DOCTYPE html>` that boots one app and nothing else — including
the trick of injecting server state as a JSON blob into a global before the
scripts load:

```xml
<script type="text/javascript">
    var odoo = <t t-out="json.dumps({
        'csrf_token': request.csrf_token(None),
        '__session_info__': session_info,
        'pos_session_id': pos_session_id,
        ...
    })"/>;
    odoo.loadMenusPromise = Promise.resolve();
</script>
```

That last line — stubbing out the web client's menu loading so it cannot pull
the backend in behind you — is a good summary of how much of Odoo's frontend
POS is trying *not* to be. We want to be even less of it.

### 2.3 There is a plain-JSON dispatcher, not just JSON-RPC

`odoo/http.py:2638` — `type='json2'` takes the request body as the parameters,
returns the endpoint's value as the response body, and maps exceptions onto
real HTTP status codes:

```python
class Json2Dispatcher(Dispatcher):
    routing_type = 'json2'
    ...
    def handle_error(self, exc):
        if isinstance(exc, (UserError, SessionExpiredException)):
            status = exc.http_status
```

This is the REST-shaped dispatcher. `type='jsonrpc'` (`http.py:2544`) is the
JSON-RPC 2.0 envelope the web client's RPC layer speaks — always HTTP 200,
errors in the body. A frontend written against `fetch` wants `json2`.

### 2.4 The bus is reachable without any Odoo JavaScript, and crosses processes

`addons/bus/controllers/websocket.py:11`:

```python
@route('/websocket', type="http", auth="public", cors='*', websocket=True)
```

Public, CORS-open, and the protocol is plain JSON — `addons/bus/websocket.py:909`
reads `jsonrequest['event_name']`, and `:941` handles `subscribe` with
`{"channels": [...], "last": <id>}`. Server side, one call pushes
(`addons/bus/models/bus.py:111`):

```python
def _sendone(self, target, notification_type, message):
```

Crucially for a two-process design, the bus fans out through PostgreSQL:
`bus.py:164` issues `NOTIFY imbus`, and the dispatcher listens for it on the
`postgres` database (`bus.py:240-243`). **A `_sendone` from the game process
reaches a browser connected to the web-client process, and vice versa.** No
extra plumbing needed to cross the process boundary.

Note the cost per message: `_sendone` queues a `bus.bus` row at precommit and
issues the `NOTIFY` at postcommit (`bus.py:121-166`). A pulse is therefore an
`INSERT`, not just a notification — fine at one per second, but a reason not to
raise the pulse rate casually.

### 2.4.1 The bus forgets K times faster than it should

A consequence of the game clock reaching a module nobody was thinking about.
`_gc_messages` (`bus.py:97-108`) deletes on **game** time:

```python
timeout_ago = fields.Datetime.now() - datetime.timedelta(seconds=gc_retention_seconds)
self.env.cr.execute("DELETE FROM bus_bus WHERE create_date < %s", (timeout_ago,))
```

`fields.Datetime.now()` is game time and so is `create_date`, so the retention
window is denominated in game seconds. `DEFAULT_GC_RETENTION_SECONDS` is 24
hours (`bus.py:26`), which in real time is:

| `K` | real catch-up window |
|---|---|
| 60 | 24 minutes |
| 240 | 6 minutes |
| 1440 | **60 seconds** |

That window is how far back a reconnecting client can replay missed
notifications using its `last` id. At `K = 1440` a client offline for more than
a minute has lost game events permanently — and "offline for more than a
minute" is exactly the situation a locked UI is in (§5.6).

**So a game world should set `bus.gc_retention_seconds` explicitly**, sized in
game seconds for the real window wanted: one real hour at `K = 1440` is
`3600 * 1440`. `game_run` now warns at startup when the window falls below a
real hour, quoting the number to set — deliberately warning rather than setting
it, since which worlds want which backlog is a game-design question. And the UI
must treat reconnection as *resync*, not *replay* — see §6.9.

This turned out to be an instance rather than a one-off, and `DESIGN.md` §5.12
now names the shape: **any duration constant subtracted from a game instant and
compared against a game-time column is denominated in game seconds, and so
divides by `K` in real terms.** `MIN_DELTA_BEFORE_DEACTIVATION` (7 days → 7 real
minutes at `K = 1440`) and cron batch windows are the same shape. None of them
uses raw `datetime.now()`, so `DESIGN.md` §5.4's sweep would never have found
any of them — this is *correct* code with a surprising outcome, which is the
harder kind to go looking for.

### 2.5 The clock is an accumulator owned by the game loop

**Changed by `DESIGN.md` §3.3.** Game time used to be derived from wall time
via immutable anchors, which meant the browser could compute it locally forever
and could not possibly disagree with the server. That is no longer true, and
the difference is the single most important input to §5.5.

The clock is now `(game_now, last_tick_real, rate, paused, max_gap)`, advanced
by the loop:

```sql
UPDATE public.game_clock
   SET game_now = CASE
                    WHEN paused THEN game_now
                    ELSE game_now
                       + LEAST(
                             GREATEST(pg_catalog.now() - last_tick_real, INTERVAL '0'),
                             max_gap
                         ) * rate
                  END,
       last_tick_real = pg_catalog.now(),
       paused = COALESCE(%s, paused);
```

Note that `last_tick_real` advances **even while paused** — the pause is
expressed in the `CASE`, not in a `WHERE`. That is deliberate and the UI
depends on it: were the row simply skipped while paused, `last_tick_real` would
stay frozen for the length of the pause and resuming would credit the world
with a clamped `max_gap * rate` jump. As written, a paused stretch is never
accrued and a resume is continuous.

Readers interpolate between ticks with the same clamped expression. Two
properties of that design carry straight into the UI:

**Interpolation is exactly continuous across a tick** — verified here, not just
taken from `DESIGN.md`: a reader's value immediately before and immediately
after a tick is identical, and it stays identical when the `LEAST` clamp binds,
because both the update and the read clamp the same way. So a client
interpolating locally never sees a jump when a tick lands.

**But game time now only advances when something ticks it.** A wall-clock
formula ran whether or not anyone was home; an accumulator does not. `paused`
stops it deliberately, and `max_gap` stops it accidentally when the loop dies.
Neither is observable by a browser holding a snapshot — which is why the UI
needs a pulse (§5.6).

This is built. `odoo/game_clock.py` now exposes what the UI backend needs:

| | |
|---|---|
| `clock_for(dbname, cr=None)` | a reading — `game_now`, `last_tick_real`, `rate`, `paused`, `max_gap` |
| `tick(cr)` | the clock thread's whole job, and crash recovery with it |
| `set_paused(cr, bool)` | settles game time, then flips `paused` |
| `GameClock.game_at(real)` | the clamped interpolation, mirroring `public.now()` |

Two constants to design against: `DEFAULT_MAX_GAP` is **5 real seconds** and
`CACHE_TTL` is **1 real second**, so a process notices a pause, a resume or a
dead loop up to a second late. At `K = 1440` that second is 24 game minutes —
fine for a clock, and a reason the UI should treat the pulse rather than a
fetched reading as authoritative for *liveness*.

`game_at` also clamps negative elapsed to zero (`GREATEST(..., INTERVAL '0')`),
because PostgreSQL's wall clock and the reading process's can disagree. The
browser is a third clock and needs the same guard (§5.5).

### 2.6 A second process on one database has a contract to keep

This is the finding that matters most for the decision, and it cuts both ways.

**Cache coherence is not automatic.** `odoo/orm/registry.py:1103,1137` —
processes stay consistent by writing to signalling sequences and polling them:

```python
def check_signaling(self, cr=None):
    """ Check whether the registry has changed, and performs all necessary
        operations to update the registry. Return an up-to-date registry. """
```

Every entry point into Odoo calls it: the HTTP dispatcher (`http.py:2277`), the
RPC layer (`service/model.py:118`, `:134`), the cron thread
(`service/server.py:1039`). A process that skips it serves stale ORM caches
after anyone changes a field or installs a module elsewhere, and its own writes
never invalidate the other process's caches.

**The cron half of that is already handled for us.**
`ir_cron.py:235` — `_process_jobs` calls `Registry(db_name).check_signaling()`
around each job, and `_callback` calls `self.pool.signal_changes()`
(`ir_cron.py:695`). So a loop that only drives `_process_jobs` is correct for
free. It is the *HTTP* half that has to keep the contract by hand.

**And one sharp edge.** `_process_jobs` sets the thread's database and then
deletes it (`ir_cron.py:191`, `:211-213`):

```python
threading.current_thread().dbname = db_name
...
finally:
    if hasattr(threading.current_thread(), 'dbname'):
        del threading.current_thread().dbname
```

A game process that sets `thread.dbname` at startup the way `odoo-bin shell`
does (`odoo/cli/shell.py:136`) **loses it on the first tick**. After that,
`game_clock.current_clock()` falls back to `config['db_name']`, and if that is
not exactly one database it returns `None` — meaning `fields.Datetime.now()`
silently reverts to **real time** (`DESIGN.md` §5.8). See §6.7.

### 2.7 An addon can ship a CLI command

`odoo/cli/command.py:68-85` — the command loader globs the addons path for
`*/cli/<command>.py`:

```python
def load_addons_commands(command=None):
    """ Search the addons path for modules with a ``cli/{command}.py`` file. """
    for path in odoo.addons.__path__:
        for fullpath in Path(path).glob(f'*/cli/{command}.py'):
```

So the game process can be `odoo-bin game_run -d world`, shipped entirely
inside the game addon, with **zero addition to the fork's core diff** — which
`DESIGN.md` has kept to 36 lines across five files.

`sim_init` stays in core, and the split is principled rather than incidental:
`sim_init` runs against a database *before* the game addon exists, so it cannot
live in it; `game_run` needs the addon loaded anyway.

### 2.8 The tick blocks for at least ten real seconds

The most consequential finding for a process that runs the tick *and* serves
the UI. `ir_cron.py:33-34`:

```python
MIN_RUNS_PER_JOB = 10
MIN_TIME_PER_JOB = 10  # seconds
```

and `_run_job` (`ir_cron.py:499-503`) keeps going until **both** are satisfied:

```python
while status is None and (
    loop_count < MIN_RUNS_PER_JOB
    or time.monotonic() < env.context['cron_end_time']   # start + 10 REAL seconds
):
```

`status` stays `None` while the job reports remaining work, so a cron using the
progress API holds the tick for ≥10 runs **and** ≥10 real seconds. That is not
an exotic path: **21 files call `_commit_progress`**, including `sale`,
`account`, `stock`, `mail_mail`, `sms` and `base_automation`.

Note the units. `MIN_TIME_PER_JOB` is compared against `time.monotonic()` —
**real** seconds, correctly, since it is a throughput control and not game
logic. But at `K = 1440` those ten real seconds are **four game hours** spent
inside a single job.

Two consequences, both in §5.3: the tick can never share a thread with request
handling, and `TICK * K` is a lower bound on cron resolution rather than a
guarantee.

## 3. The options

The axis that matters is not *how many processes* but **whether the game code
embeds the Odoo ORM**. Getting those two questions confused is what made the
first draft of this document rule out the right answer.

### A. A client action inside the Odoo web client

An OWL component registered as `ir.actions.client`, assets in
`web.assets_backend`, reached from a menu item.

- **For:** the least new machinery of any option. Free ORM, session, RPC.
- **Against:** the entire backend bundle loads around what is meant to be a
  canvas. The web client owns the viewport, the keyboard, the URL and the
  navigation, and every one of those is something a game eventually wants. OWL
  offers nothing to a WebGL renderer and gets in its way.
- **Verdict:** right for a debug panel, wrong for the destination.

### B. An addon inside the Odoo HTTP server

Models, controllers and a game-loop thread, all inside `odoo-bin`.

- **For:** everything works with no new lifecycle.
- **Against:** the game's tick is now a thread inside a web server tuned for
  request/response. Restarting the game restarts the web client. Under
  `--workers > 0` there are several server processes and the tick must run in
  exactly one of them, which is a coordination problem with no good answer.
- **Verdict:** superseded by E, which is the same thing with its own lifecycle.

### C. A separate service over RPC

A FastAPI/Node backend beside Odoo, reaching it through JSON-RPC.

- **Against:** the objection is transactional. A game action is naturally
  *"spend 100 coins **and** confirm the sale order"*. Split across an RPC
  boundary, those are two transactions with no shared commit, and every
  interesting action becomes a saga. Session bridging and per-record
  round-trips are the smaller costs.
- **Verdict:** rejected. **Note carefully that this objection is about RPC, not
  about processes** — see E.

### D. A separate service, direct SQL against the Odoo tables

- **Against:** writing past the ORM skips computed fields, constraints,
  `ir.rule`, tracking and cache invalidation. The database's invariants live in
  Python, not in the schema.
- **Verdict:** non-starter. `game_clock` is raw SQL because it is read from
  *below* the ORM (`odoo/sql_db.py`) and must not depend on a registry — a
  bootstrap constraint, not a precedent for gameplay.

### E. A separate process that embeds the ORM — **chosen**

A process that imports Odoo, builds a `Registry`, opens cursors, and runs its
own loop. `odoo-bin shell` is the nearest existing example
(`odoo/cli/shell.py:130-140`):

```python
config.parse_config(args, setup_logging=True)
server.start(preload=[], stop=True)
threading.current_thread().dbname = dbname
registry = Registry(dbname)
with registry.cursor() as cr:
    env = api.Environment(cr, api.SUPERUSER_ID, ctx)
```

**But do not copy that line literally — `stop=True` serves nothing.** It gates
the HTTP daemon off (`server.py:659`, `if config['test_enable'] or
(config['http_enable'] and not stop)`) and returns immediately after loading
the registry (`server.py:716-727`, `if stop: ... self.stop(); return rc`),
never reaching `cron_spawn()`. The shell shape embeds the ORM *and serves no
HTTP*, which is right for a REPL and wrong for us.

`game_run` wants the **serving** variant — `server.start(preload=[db])`,
without `stop=True` — plus `--max-cron-threads=0` so that `cron_spawn`
(`server.py:620-633`) iterates `range(0)` and starts none of Odoo's own cron
threads, leaving ours as the only one. See §5.3.

- **For:** every advantage of B — full ORM, real transactions, one commit
  spanning `game_plot` and `stock_move` — **plus** its own lifecycle. Restart
  the game without restarting Odoo. A deterministic tick that is the process's
  main loop rather than a thread inside a web server. And the tick interval
  becomes a knob we own (§5.3), which is what lifts `DESIGN.md` §4.3's
  `K <= 240` ceiling.
- **Against:** the contract of §2.6 — signalling on the HTTP half, the
  `thread.dbname` edge, and concurrency against `odoo-bin` (§6.6).
- **Verdict:** the recommendation.

### Summary

| | A: client action | B: addon in odoo-bin | **E: embedded process** | C: RPC service | D: direct SQL |
|---|---|---|---|---|---|
| ORM access | direct | direct | **direct** | round-trip | none |
| One transaction with Odoo | yes | yes | **yes** | no | unsafe |
| Own lifecycle / restart | no | no | **yes** | yes | yes |
| Deterministic tick | n/a | thread in a web server | **the main loop** | yes | yes |
| Auth | free | free | free (§5.6) | bridge it | bridge it |
| Frontend freedom | poor | full | **full** | full | full |
| Cache signalling | free | free | **by hand (§2.6)** | n/a | broken |

## 4. Recommendation

**A separate process that embeds the Odoo ORM, running both the game loop and
the UI backend.** Option E.

It keeps the one property that justifies building on Odoo at all — the ORM
enforces the simulated company's business rules, and game writes commit in the
same transaction as the records they produce — while giving the game the
lifecycle and the main loop it wants.

### 4.1 How it serves HTTP: reuse Odoo's stack

One sub-decision remains: whether the process serves HTTP with its own
framework or with Odoo's.

- **E1 — its own** (FastAPI/Starlette/werkzeug). Native async websockets, no
  gevent. But `check_signaling` per request (§2.6), retry on serialisation
  failure (§6.6), and session authentication (§5.6) all become ours to write,
  and each is easy to get subtly wrong.
- **E2 — Odoo's** — a second `odoo-bin` on its own port, with the game addon
  installed, `--max-cron-threads=0`, and the tick as the process's own loop.

**Recommend E2.** The three things E1 costs us are exactly the three things
Odoo's dispatcher already does correctly, and none of them is interesting work.
Everything the frontend cares about is unchanged either way: raw static serving
(§2.1), `json2` (§2.3), no asset pipeline. We are reusing Odoo's *dispatcher*,
not its web client.

Revisit E1 if the game ever needs many concurrent websocket connections, where
a threaded WSGI server plus gevent is genuinely the wrong shape. For a world
with a handful of players it is not.

### 4.2 What "the same process" means here, precisely

The phrase is ambiguous enough to have already caused one misunderstanding, so
to be exact. There are **two** processes:

| process | contains |
|---|---|
| `odoo-bin` | the Odoo web client — accounting, inventory, the usual |
| `odoo-bin game_run` | **the tick *and* the UI backend** |

"The UI rides in the game process" means the second row: the game loop and the
game's HTTP endpoints are one OS process. It does **not** mean the game shares
a process with the Odoo web client — that is option B, and it is what having a
separate process was for.

### 4.3 That process is multi-threaded, and has to be

§2.8 forces this. A cron using the progress API holds the tick for ≥10 real
seconds, so a UI request handled on the tick's thread would stall behind it —
four game hours of stall at `K = 1440`.

`DESIGN.md` §3.3 splits it further, and for the same reason: the **clock**
thread must not be the **cron** thread either, or a ten-second cron would delay
the clock `UPDATE` past `max_gap` and silently lose game time. So the process
runs three kinds of thread:

| thread | does | must not block on |
|---|---|---|
| clock | the one-statement `UPDATE`, on a short interval | anything |
| cron | `_process_jobs` | — (it *is* the slow one) |
| request (many) | the UI's HTTP and websocket | either of the above |

This is worth stating plainly because it looks like the "complexity" that
counted against running the game as a thread inside the web server (§3.B) — but
it is not the same cost. `ThreadedServer` already runs a thread per request; we
are adding **two**, to a threading model Odoo already owns and tests. What
§3.B objected to was needing to elect *one* server process to own the tick when
there are several; that problem is absent here regardless of how many threads
this process has.

It does mean E1's "single async loop" framing was never really on the table:
whatever serves HTTP here is concurrent with a tick that blocks.

## 5. Design

### 5.1 Layout

**As built**, the addon is `addons/odoo_sim/`, and this document's earlier
proposal of `odoo_sim/addons/game/` is superseded:

```
odoo_sim/
  DESIGN.md            game clock
  UI_DESIGN.md         this document
  ui/                  the frontend source tree, its own package.json

addons/odoo_sim/       depends: base, bus
  pulse.py             CHANNEL / TYPE / payload() / send()   (5.6)
  cli/game_run.py      clock thread + cron thread + serving bootstrap
  cli/sim_pause.py     pause / resume
  tests/test_pulse.py  5 tests
  models/              game state (Odoo models)          <- to build
  controllers/main.py  the page + the json2 API          <- to build
  static/dist/         vite build output, served raw (2.1)
```

`pulse.py` sits beside the CLI rather than inside it deliberately: the pulse is
a module the UI backend imports, and reaching it through a `Command` subclass
would have made testing it require loading a CLI module. Import it as
`from odoo.addons.odoo_sim import pulse`.

I had argued for keeping it out of `addons/` to protect the fork's rebase, and
that argument does not survive contact: `addons/odoo_sim/` is a *new directory*,
so it conflicts with nothing upstream and does not touch the diff of any
existing file. What it buys is that the addon sits on the default addons path,
so `odoo-bin game_run` is discoverable with no `--addons-path` flag. That is the
better trade and the doc follows the code.

The one thing to preserve is what §2.7 actually bought: the command ships in the
addon, so `odoo/` gains nothing. `sim_init` stays in core because it runs before
the addon exists.

Note also that `game_run` works whether or not the module is *installed* —
command discovery reads the addons path, not the database. Installation only
matters once the game has models of its own, which is the point at which the UI
work begins.

Two processes, one database:

```bash
# the Odoo web client — accounting, inventory, the usual
odoo-bin -d world --max-cron-threads=0

# the game: tick + UI backend
odoo-bin game_run -d world --http-port=8070 --max-cron-threads=0
```

`--max-cron-threads=0` on **both**: the first so the web server never fires a
cron behind the game's back, the second because the tick does it itself.

### 5.2 The game's own state: Odoo models, not raw tables

Declare game state as ordinary Odoo models — `game.player`, `game.plot` — which
gives tables `game_player`, `game_plot`, entirely separate from Odoo's own, in
the same database.

Use models rather than raw tables because:

- Schema migration comes free with module upgrade.
- Constraints, defaults and computed fields are declared once.
- Odoo's own list and form views make game state inspectable during
  development without building a debug UI.
- Decisively: writes to `game_plot` and writes to `stock_move` land in the same
  `cr`, and commit or roll back together.

**One trap, inherited from the clock.** `create_date` and `write_date` on these
tables will carry **game** time, because `cr.now()` is the game clock
(`DESIGN.md` §4.2). For gameplay state that is correct and desirable. For
meta-state — when a player last logged in, telemetry, anything an operator
reads — it is wrong, and silently so: at `K = 1440` a `write_date` from an hour
ago reads as two months old. Meta-state must carry an explicit real-time column
and must not lean on the magic ones.

### 5.3 The loop

`odoo/service/server.py:542-610` is the reference implementation, and the
game's version is strictly simpler: one database, so no `cron_database_list()`,
no `postgres` connection, and none of the thundering-herd jitter that exists
only to spread several workers apart.

This is the **cron** thread specifically. The clock thread is separate and
deliberately shares nothing with it (§4.3), because a cron holding this loop
for ten real seconds must not be able to delay the clock past `max_gap`. That
thread is short enough to give in full:

```python
while True:
    with registry.cursor() as cr:
        clock = game_clock.tick(cr)          # the whole clock thread
    publish_pulse(clock)                     # §5.6
    time.sleep(CLOCK_INTERVAL)               # < max_gap
```

`game_clock.tick` is also crash recovery: the statement clamps its own elapsed
term, so a restart after a kill or a suspend is an ordinary tick that happens
to have been a long time coming.

What this loop must keep is the `LISTEN`, so that event-driven crons
(`_trigger`, which issues `NOTIFY cron_trigger`) still wake it immediately
instead of waiting out the tick. It is started by `game_run` after
`server.start(preload=[db])` has brought the server up:

```python
cr.execute("LISTEN cron_trigger")
cr.commit()
while True:
    select.select([cr._cnx], [], [], TICK_SECONDS)
    cr._cnx.poll()
    cr._cnx.notifies.clear()

    IrCron._process_jobs(db_name)              # signalling handled inside (2.6)
    threading.current_thread().dbname = db_name  # _process_jobs deleted it (2.6)
```

**The thread must opt into the watchdog.** `process_limit`
(`server.py:509-535`) enforces `limit_time_real_cron` on threads, but only
those carrying `type = 'cron'` and a `start_time`:

```python
if not thread.daemon and thread_type != 'websocket' or thread_type == 'cron':
    if getattr(thread, 'start_time', None):
```

Odoo's own runner sets both — `t.type = 'cron'` in `cron_spawn`
(`server.py:631`) and `thread.start_time = time.time()` around each
`_process_jobs` call (`server.py:604-608`). Our thread has to do the same, or a
runaway game cron runs forever with nothing to stop it. This is a second
argument for the serving bootstrap of §3.E: `process_limit` is driven from
`ThreadedServer.run`'s main loop, so we get the watchdog only because the
server is actually running.

**The tick interval is now a game-design parameter.** It replaces
`SLEEP_INTERVAL = 60` (`server.py:68`), which is where `DESIGN.md` §4.3's
`K <= 240` ceiling came from: scheduling jitter is `TICK * K` game seconds.

| `TICK` | `K = 60` | `K = 240` | `K = 1440` |
|---|---|---|---|
| 60 s (Odoo's default) | 1 game hour | 4 game hours | 1 game day |
| 5 s | 5 game min | 20 game min | 2 game hours |
| 1 s | 1 game min | 4 game min | 24 game min |

At `TICK = 1` an hourly cron is accurate to 24 game minutes even at `K = 1440`,
which makes interval crons meaningful again. The cost is one
`_get_all_ready_jobs` query per second, which is nothing.

**But read that table as a lower bound, not a guarantee.** §2.8 — a job that
keeps reporting remaining work holds the tick for ≥10 real seconds, and an
overrunning tick simply starts the next one late. So `TICK * K` is the
resolution the loop *offers*; what a given cron actually gets also depends on
what ran before it. Setting `TICK = 1` buys nothing on a world whose crons
routinely run the full ten seconds, and no smaller `TICK` will fix that — the
floor is `MIN_TIME_PER_JOB`, not the tick.

`DESIGN.md` §5.2 is unaffected: missed occurrences are still collapsed, by
design.

### 5.4 The API surface

`type='json2'` routes (§2.3) under a single prefix:

```python
@http.route('/game/api/clock', type='json2', auth='user', methods=['GET'])
def clock(self):
    ...
```

`json2` and not `jsonrpc`: the frontend is `fetch`, not Odoo's RPC layer, and
we want HTTP status codes on errors rather than an envelope that is always 200.

Under E2 these are ordinary Odoo controllers, so `check_signaling` and the
serialisation-failure retry come from the dispatcher and are not our problem.

### 5.5 The clock endpoint returns the tick basis, not the time

**Fetching the time is fine** — this document previously treated avoiding it as
a design goal, and that was purism. It is one small request. What the client
should *not* do is fetch it at frame rate, because it does not need to: one
reading plus interpolation is exact between ticks (§2.5), so the fetch sets a
basis and the browser fills in the gaps.

What changed with `DESIGN.md` §3.3 is *what* it interpolates from, and the fact
that the basis now goes stale:

```json
{
  "game_now": "2026-09-10T14:23:07.000Z",
  "last_tick_real": "2026-09-10T13:26:04.000Z",
  "rate": 1440.0,
  "paused": false,
  "max_gap": 5.0,
  "server_real_now": "2026-09-10T13:26:04.412Z"
}
```

#### The trap: neither formula is correct on its own

This is worth spelling out, because both obvious choices are wrong and the
failure is silent in both directions. Measured, with `K = 1440`, `max_gap = 5`,
a browser that fetched the basis at `t = 0` and a loop ticking every second:

| | loop alive, `t = 10` | loop died at `t = 5`, reader at `t = 60` |
|---|---|---|
| the database says | 14400 | 14400 |
| browser, **clamped** formula | **7200** ✗ | 14400 ✓ |
| browser, **unclamped** formula | 14400 ✓ | **86400** ✗ |

The clamped formula — the one the server uses — binds after `max_gap` real
seconds because the browser's `last_tick_real` is frozen at the moment it
fetched, so a browser using it stops the clock while the game is running fine.
The unclamped formula tracks a live loop perfectly and then runs away when the
loop stops: **20 game hours of divergence after 55 real seconds of silence.**

The resolution is not a cleverer formula. The clamp exists to detect that *the
loop* stopped ticking, and a browser cannot observe ticks. **It has to be
told** — which is the pulse of §5.6.

So the division of labour is:

| | carries | when |
|---|---|---|
| `GET /game/api/clock` | the basis | cold start, reconnect, and whenever the page wants to resync |
| the pulse (§5.6) | the same basis | every tick or few, and it is what makes the clamp usable |

The endpoint is not the interesting half. A page that only ever fetched would
be wrong in one of the two directions above; a page that has the pulse could
almost skip the fetch. Both exist because the fetch is the cold-start path.

Two corrections the client must make, both of which matter more here than in a
normal application because errors are multiplied by `K`:

**Skew — do not trust the browser's wall clock.** It can be minutes off. At
`K = 1440`, one real second of skew is twenty-four game minutes. Hence
`server_real_now` in the payload: the client records `Date.now()` at receipt
and keeps the offset.

**Drift — do not use `Date.now()` to advance, either.** It jumps on NTP
correction and across sleep/wake. Use `performance.now()`, which is monotonic:

```js
// re-captured on every pulse, not just at startup
let basis;
const utc = s => Date.parse(s + 'Z');   // naive ISO from the server; see below

function onBasis(msg) {
    basis = {
        gameNow:      utc(msg.game_now),
        lastTickReal: utc(msg.last_tick_real),
        paused:       msg.paused,
        running:      msg.running,
        t0:           utc(msg.server_real_now),   // server real ms
        p0:           performance.now(),          // monotonic reference
    };
}

function gameNow() {
    if (basis.paused) return basis.gameNow;
    const realNow = basis.t0 + (performance.now() - basis.p0);
    // clamp both ends, exactly as game_at() does: the browser is a third
    // clock and can read behind PostgreSQL's.
    const gap = Math.min(Math.max(realNow - basis.lastTickReal, 0), maxGap * 1000);
    return basis.gameNow + gap * rate;
}
```

`gap` is clamped at both ends exactly as `GameClock.game_at` clamps it (§2.5) —
`GREATEST(..., 0)` matters here more than it does server-side, because the
browser's clock is the one most likely to be wrong. And the upper clamp is only
safe because the pulse keeps `lastTickReal` fresh; that is the whole point of
the trap table above.

#### Parse the timestamps as UTC, explicitly

Note the `+ 'Z'` in `onBasis` above. It is not decoration.

`pulse.payload` serialises with `datetime.isoformat()` on naive UTC datetimes,
so the strings carry **no offset**: `"2026-09-12T22:38:10.473543"`. JavaScript
parses an ISO *date-time* with no offset as **local time** — while parsing a
date-*only* string as UTC, an inconsistency that lives in the language spec and
has caught everyone at least once.

The damage is subtle because it partly cancels. `gap` is a difference between
two timestamps from the same payload, so the offset cancels there and the clock
still *ticks* at the right rate — which is why this survives a casual test. What
does not cancel is the absolute value: the displayed game time is wrong by the
browser's UTC offset, multiplied by `K`.

| browser | `K` | display is wrong by |
|---|---|---|
| UTC+2 | 60 | 5 game days |
| UTC+2 | 1440 | **120 game days** |
| UTC−5 | 1440 | 300 game days |
| UTC+5:30 | 1440 | 330 game days |

And it stops cancelling entirely the moment anyone compares a parsed
`server_real_now` against `Date.now()` — the obvious way to compute skew, and
wrong by the whole local offset. `performance.now()` (§5.5) avoids that by not
involving a wall clock at all, which is a second reason to prefer it.

So: append `Z` at the parse boundary, once, and never parse these strings any
other way. Three corollaries, each of which is a plausible wrong turn:

**Do not reach for Odoo's `deserializeDateTime`.** It is `fromSQL`, not
`fromISO` (`addons/web/static/src/core/l10n/dates.js:709`):

```js
export function deserializeDateTime(value, options = {}) {
    return DateTime.fromSQL(value, { numberingSystem: "latn", zone: "utc" })
        .setZone(options?.tz || "default")
```

`fromSQL` wants Odoo's wire format — `"2026-09-12 22:38:10"`, space-separated —
so it returns an **Invalid DateTime** on our `T`-separated string rather than a
wrong one, which at least fails loudly. And note the `.setZone(...)` on the next
line: even if it parsed, it would convert to the user's timezone, which is the
opposite of what a raw interpolation basis wants. Use `DateTime.fromISO(v, {
zone: "utc" })` if you are already carrying Luxon, or the two-line `Date.parse`
above if you are not.

**The server is right not to emit SQL format**, tempting as it looks: `fromSQL`
truncates to the second, and one second of `last_tick_real` is **24 game
minutes** of basis error at `K = 1440`. Precision beats helper compatibility for
a clock.

**Never pattern-match the string, always parse it.** Python's `isoformat()`
omits the fractional part entirely when microseconds happen to be zero:

```
datetime(2026, 9, 9, 12, 0, 0)          -> '2026-09-09T12:00:00'
datetime(2026, 9, 9, 12, 0, 0, 473543)  -> '2026-09-09T12:00:00.473543'
```

Both are valid ISO and both parse, but a regex expecting `.ffffff` sees the
first roughly one tick in a million and fails then and only then.

One thing the client cannot preserve, worth stating so nobody chases it:
JavaScript's `Date` holds milliseconds, so the microseconds the server carefully
keeps are truncated at the boundary regardless. That bounds the client's basis
error at 1 ms real — about 1.4 game seconds at `K = 1440` — which is far below
the RTT term already accepted above. The microseconds matter for the *server's*
arithmetic, not for the display.

The residual error is roughly RTT/2 — tens of milliseconds real, so tens of
*seconds* of game time at `K = 1440`. Acceptable for a displayed clock; if it
ever is not, the fix is the standard one (several samples, keep the one with
the lowest RTT), not a polling loop.

### 5.6 The pulse, and locking when it stops

**One message does three jobs**, which is why it is worth designing once:

1. **It carries the clock basis**, so the browser's clamp is usable (§5.5).
2. **It is the liveness pulse.** Its *arrival* is the signal, independent of
   its contents.
3. **Its absence locks the UI.**

The clock thread already runs a short-interval `UPDATE` (`game_clock.tick`), so
it publishes from where it already is.

#### The pulse, as built

Built, and in its own module rather than inside the CLI command — testing it
otherwise would have meant importing a `Command` subclass:

```python
from odoo.addons.odoo_sim import pulse

pulse.CHANNEL                      # 'odoo_sim.world'
pulse.TYPE                         # 'odoo_sim.pulse'
pulse.payload(clock, real=None)    # -> dict, the seven fields below
pulse.send(cr, clock)              # -> bool
```

| | |
|---|---|
| channel | `"odoo_sim.world"` |
| type | `"odoo_sim.pulse"` |
| cadence | **every tick, unconditionally** |
| constraint | pulse interval < `max_gap` |

```json
{
  "game_now":        "2026-09-10T14:23:07.000Z",
  "last_tick_real":  "2026-09-10T13:26:04.000Z",
  "rate":            1440.0,
  "paused":          false,
  "max_gap":         5.0,
  "running":         true,
  "server_real_now": "2026-09-10T13:26:04.412Z"
}
```

Two fields earn their place beyond the basis of §5.5. `running` is
`game_clock.is_running(clock)` evaluated server-side, so the browser never
re-derives the predicate — it is the same answer the write path enforces, which
is what keeps the lock and the guard from drifting apart. `server_real_now` is
what lets the client correct its own wall-clock skew for *interpolation*
(§5.5).

**Unconditionally is the part to defend.** Sending only when something changed
is the obvious optimisation and it destroys the mechanism: silence has to mean
death, so a pulse that is suppressed when the world is quiet is indistinguishable
from a loop that has stopped. The pulse is not a change notification.

#### Pause is not silence — which is what makes three states legible

This falls out of sending unconditionally, and it is the property the lock
depends on: **a paused world keeps pulsing.** Confirmed on the live world, two
consecutive pulses one second apart:

```
game_now        2026-09-13T11:34:20.517063   ← identical
last_tick_real  2026-09-10T12:03:08.610544
paused true, running false

game_now        2026-09-13T11:34:20.517063   ← identical
last_tick_real  2026-09-10T12:03:09.623591   ← still advancing
paused true, running false
```

Game time frozen to the microsecond while `last_tick_real` keeps moving — which
is §2.5's `CASE`-not-`WHERE` correction working, and the reason a resume credits
nothing for the pause.

So the client can tell three states apart with no ambiguity, and it needs no
extra signal to do it:

| pulses | `running` | state |
|---|---|---|
| arriving | `true` | live |
| arriving | `false` | **paused** — deliberate, the world is fine |
| silent | — | **dead** — the loop is gone |

Only death is silent. A lock-on-silence therefore cannot misfire on a
deliberate pause, and "paused" and "lost contact" are distinguishable at the
mechanism level rather than needing to be guessed at in the UI. What remains is
purely how each should *look* and what each should still allow (§9).

#### The lock threshold: count silence, do not compare clocks

`max_gap` is the right magnitude — it is exactly how long the server's own clock
keeps advancing without a tick, so a client that gives up after `max_gap` stops
at the instant the database stops, and one constant governs both sides. With the
shipped `DEFAULT_MAX_GAP` of 5 seconds and a pulse per tick, that is a lock
about five seconds after the loop dies.

**But measure it as elapsed-since-last-pulse-arrived, on `performance.now()` —
never as `Date.now()` against `last_tick_real`.** The server compares
PostgreSQL's clock against `last_tick_real`; a browser doing the analogous
comparison is comparing *its* wall clock against a server timestamp, so a client
skewed slow locks late and one skewed fast locks spuriously. Elapsed time since
a locally-observed arrival involves no server timestamp and no absolute clock at
all, so skew cannot reach it:

```js
let lastPulse = performance.now();          // on every pulse
const locked = () => (performance.now() - lastPulse) > maxGap * 1000
                  || !basis.running;
```

This splits the two roles cleanly, and they should not be conflated:

| | uses | why |
|---|---|---|
| interpolating the clock | absolute basis + `server_real_now` skew correction | needs to agree with the server's *value* |
| deciding to lock | local monotonic delta only | needs to be immune to the client's clock |

A dropped connection should lock immediately rather than wait out `max_gap` —
the websocket's own `close`/`error` is a faster and more reliable death signal
than silence.

#### What "lock" means, and why it is a safety property

Freezing the clock display is the smallest part. The reason to lock *input* is
that a dead loop does not stop the world from accepting writes: the web threads
are alive, PostgreSQL is up, and `auth='user'` still passes. So a player at an
unlocked page could keep acting into a world that cannot process any of it —
actions land with a frozen `create_date`, and every consequence that depends on
a cron (a crop growing, a delivery arriving) simply never happens. The player
would be accumulating a private, silent divergence from the world.

So locking means refusing actions, not dimming the clock.

**And the lock cannot only live in the browser.** A client-side lock is a
courtesy that a stale tab, a reconnecting client, or anything replaying a
request will miss. `game_clock` now ships the predicate, so the guard is one
call and must not be re-derived per endpoint:

```python
if not game_clock.is_running(game_clock.clock_for(request.db, request.env.cr)):
    raise UserError("The world is not running.")
```

`is_running` returns `True` for a database that is not a game world at all,
which means the guard can sit on a shared write path without breaking ordinary
Odoo. It covers `paused` as well as a dead loop — the browser should treat
those as *different states with the same effect on input* (§9).

The UI lock is the user experience; this is the guarantee.

#### Game events ride the same channel

State changes — a harvest completing, a delivery arriving — are the ordinary
traffic. Server side, one call:

```python
env['bus.bus']._sendone(channel, 'game.event', payload)
```

Client side, a raw `WebSocket` to `/websocket` and one subscribe frame. No Odoo
JavaScript, no SharedWorker. Because the bus fans out through `NOTIFY imbus`
(§2.4), it does not matter which of the two processes sent it.

**Authentication.** Under E2 the player is an Odoo user, `auth='user'`, and the
session cookie carries — but the two processes must share the session store, or
a login on `:8069` will not be recognised on `:8070`. Point both at the same
`--data-dir`. This is the one piece of two-process plumbing that has no
equivalent in a single-process design, and it will present as "the game always
redirects me to the login page".

If the game later wants its own accounts, decoupled from `res.users`, that is
an `auth='public'` route plus a game-side session — no reason to do it before
there is a second player.

### 5.7 The dev loop

**Development.** Vite on `:5173`, proxying `/game/api`, `/websocket` and
`/web/session` to the game process on `:8070`. The browser talks only to Vite,
so there is no CORS to configure and the session cookie is same-origin. HMR is
untouched by anything Odoo does.

**Production.** `vite build` into `odoo_sim/addons/game/static/dist/`, served
raw (§2.1). Filenames must be content-hashed — `STATIC_CACHE` is seven days
(`odoo/http.py:334`) and the response carries it, so an unhashed `main.js` will
be stale in every browser that has seen it. Vite hashes by default; the point
is not to turn it off.

The page itself is a minimal template in the POS mould (§2.2). Because the
entry filename is hashed, the controller should read
`static/dist/.vite/manifest.json` at render time rather than hardcoding a name.

### 5.8 What is deliberately not in the addon

The `game_clock` table and `odoo/game_clock.py` stay in core. They are read
below the ORM by `odoo/sql_db.py`, before any registry exists, and cannot
depend on a module being installed. The addon reads the clock through
`game_clock.clock_for(...)` and exposes it. It does not own it.

## 6. Risks and known issues

### 6.1 `static/` is public

`Stream.from_path(filepath, public=True)` (`http.py:2231`) — no authentication
on anything under `static/`. Correct for a JS bundle, wrong for game data. All
per-player bootstrap state goes in the rendered template behind `auth='user'`
(§2.2), never in a static JSON file next to the bundle.

### 6.2 Game-time magic columns on the game's own tables

See §5.2. The failure is silent and only shows up when someone reads a
`write_date` and believes it.

### 6.3 The UI is what makes "the clock never stops" visible

`DESIGN.md` §5.6 — game time advances whether or not a server is running,
because it derives from `pg_catalog.now()`. At `K = 1440` an overnight break is
about 1.3 game years.

This is no longer hypothetical. Measured on `simdb` while writing this:

```
$ psql -d simdb -qtAc "SET search_path=public,pg_catalog;
                       SELECT now(), pg_catalog.now();"
game_now=2030-01-10 14:04:06+01 | real_now=2026-09-10 13:26:04+02

$ psql -d simdb -qtAc "SELECT count(*) FILTER (WHERE active),
                              count(*) FILTER (WHERE active AND nextcall < now()),
                              max(lastcall) FROM ir_cron;"
18 | 17 | 2026-09-18 17:27:50
```

**3.3 game years aged unattended, and 17 of 18 active crons overdue**, all
waiting to fire in one burst on the next tick. The world has been left alone
for a few real days and its cron schedule is now meaningless.

**Decided and fixed, in `DESIGN.md` §3.3.** The clock became an accumulator
owned by the loop: `paused` is the explicit stop, and `max_gap` caps what a
dead loop can accrue, so a killed process or a closed laptop freezes the world
instead of ageing it. The measurement above is what the old design did, kept
here because it is the evidence that settled it.

What remains for the UI is not the pause itself but its consequence, and it is
a *new* risk rather than the old one: because the clock can now stop, and
because a browser interpolating from a snapshot cannot tell a stopped clock
from a running one, the UI can silently display a time the database does not
agree with. §5.5 measures it at 20 game hours after 55 real seconds of silence,
and resolves it with the pulse (§5.6). **The failure mode moved from "the world
ages while you sleep" to "the page lies about what time it is", and the second
one is quieter.**

That makes the pulse load-bearing rather than a nicety, and it is the one
part of the clock design the UI owns rather than inherits.

### 6.4 Two toolchains

An `npm` build joins the repo. Keep the boundary sharp: the addon is Python and
consumes `static/dist/` as an opaque artefact; the frontend is TypeScript and
consumes the API as an opaque contract. When debugging the *game*, the tool is
`odoo-bin shell`, not the browser.

### 6.5 The transpiler gate is a path convention

§2.1 — the build output must not land under `static/src/`, or the transpiler
will rewrite it into an Odoo module and break it. `static/dist/` by convention,
with no enforcement behind it.

### 6.6 Two processes contend for the same rows — new

The web client and the game now write the same tables concurrently. PostgreSQL
answers that with serialisation failures and deadlocks, and they will appear
under load in a way they never did with one process.

Odoo's own dispatcher already retries them (`service/model.py`), which is a
substantial part of the argument for E2 over E1 (§4.1). The tick is covered
too: `_process_jobs_loop` catches `TransactionRollbackError` per job and skips
(`ir_cron.py:226-228`). What is *not* covered is any long-running game logic we
write that holds rows across a slow section — that is ours to keep short.

### 6.7 `_process_jobs` deletes the thread's database — new

§2.6. A game loop that sets `thread.dbname` once at startup loses it after the
first tick, and the clock silently falls back to real time unless
`config['db_name']` names exactly one database.

Two mitigations, and we should take both: always run the process with
`-d <db>` so the fallback resolves, **and** re-set `thread.dbname` after each
`_process_jobs` call (§5.3). The failure mode is a world whose clock quietly
stops accelerating, which is not something a test would catch unless it is
looking for it.

### 6.8 A second `odoo-bin` needs its own ports and its own session store — new

`--http-port` obviously; `--gevent-port` too, if either process runs prefork.
And `--data-dir` must be *shared*, not separate, or sessions do not carry
between the two (§5.6). The two requirements pull in opposite directions and
are easy to get backwards.

### 6.9 Reconnection is a resync, not a replay — new

§2.4.1. The bus's catch-up window is denominated in game seconds, so at
`K = 1440` it is about sixty real seconds. Any client that was locked, asleep,
or merely disconnected for longer than that cannot replay what it missed.

This is not an edge case for this UI — it is the *normal* path out of a lock,
because a lock means the loop was down and a loop that was down for five
seconds was probably down for longer. So the reconnect handler must re-fetch
state rather than resume a notification stream, and the design should never
lean on "the bus will tell us what changed while we were away".

Raising `bus.gc_retention_seconds` (§2.4.1) widens the window but does not
change the shape: a resync path has to exist regardless, and if it exists and
works, the window matters much less.

### 6.10 What is and is not covered by tests

Narrowed — the pulse is now tested, so this is no longer the blanket gap it was
one revision ago.

**Covered:** `odoo/game_clock.py` (27 tests, green against both a game world and
an ordinary database) and `addons/odoo_sim/pulse.py` (5 tests), including the
one the lock depends on — that a pulse is *still sent while paused*.

**Not covered:** the loop's threading and the two CLI commands, verified by
running a world rather than by a suite. That is real evidence, but a regression
in the clock thread's timing or its shutdown behaviour would not be caught.

For the UI the residual risk is narrow and worth naming: the pulse's *contents*
are tested, its *cadence in a live loop* is not. A clock thread that stalls or
drifts still emits well-formed pulses, and the client's own lock is what catches
that — so the lock is not merely a UX nicety, it is the only automated check
that the loop is keeping time. Worth remembering before anyone decides the lock
is over-engineering.

## 7. Milestone 1 — the clock on a page

Deliberately no gameplay. The point is to exercise the whole spine end to end:

1. `odoo_sim/addons/game/__manifest__.py` — depends on `base`, `bus`.
2. `cli/game_run.py` — `server.start(preload=[db])` with
   `--max-cron-threads=0`, serving HTTP on its own port, plus the loop of §5.3
   in its own thread (§4.3).
3. `controllers/main.py` — `GET /game` renders the page; `GET /game/api/clock`
   returns the basis of §5.5.
4. ~~The pulse~~ — **done**, and to this spec: `odoo_sim.pulse` on
   `odoo_sim.world`, every tick, all seven fields, verified on a live world.
5. `views/index.xml` — the minimal standalone template (§5.7).
6. `odoo_sim/ui/` — Vite project; subscribe, interpolate with
   `requestAnimationFrame`, and render the three states of §5.6 — live, paused,
   locked.
7. Tests, added to the suite that now exists in `addons/odoo_sim/tests/` — the
   pulse's own five are written (§6.10), so what milestone 1 owes is the UI's
   half:
   - the clock endpoint agrees with the `game_clock` row;
   - the JS parses a payload whose microseconds are zero (§5.5);
   - the clock still returns game time *after* a tick has run (§6.7);
   - a write endpoint refuses when `is_running` is false.

   Run with `odoo-bin -d <db> -i odoo_sim --test-tags /odoo_sim
   --stop-after-init`.

**The JS has a spec to conform to, and it is executable.** `test_pulse.py`
rebuilds a `GameClock` from the payload alone and asserts it reproduces
`game_at()`. That is precisely the arithmetic §5.5's `gameNow()` implements, so
when the two disagree, that test is the authority and the JS is wrong. Worth
porting the same round-trip into the frontend's own tests against a captured
payload, so the two clamps cannot drift.

Verifiable in one sentence — and note it is the **opposite** of the criterion
this document gave before §3.3: on a world at `--rate 1440` the page shows a
clock advancing a day a minute, and when the game process is killed, the page
**stops and locks within `max_gap`** rather than running on.

The kill is the test worth writing first. It is the only one that distinguishes
this design from the one that preceded it, and it is the one a happy-path first
run will never perform.

What that proves: the process boots and holds a registry, a standalone page
boots outside the web client, a foreign build is served raw, the API answers,
and the clock the browser shows is the clock Odoo is stamping records with.

Only then: a cron that moves a game record on its own schedule, to prove the
loop; and an endpoint that writes, to prove the transaction story of §4.

## 8. Decisions

| Question | Decision |
|---|---|
| Where does the game run? | **A separate process that embeds the ORM** (§3.E). Own lifecycle and a deterministic main loop, without giving up transactions. |
| Is that not the option you rejected? | **No.** The objection to C was RPC, not processes. Embedding the ORM keeps one commit across game and business tables (§3.C). |
| How does that process serve HTTP? | **Odoo's dispatcher**, a second `odoo-bin` on its own port (§4.1). Signalling, retry and session auth are already correct there. |
| Which two things share a process? | **The tick and the UI backend** — not the game and the web client (§4.2). |
| Serving or non-serving bootstrap? | **Serving**: `server.start(preload=[db])` *without* `stop=True`, which serves nothing (§3.E). |
| One thread or several? | **Several, necessarily.** A blocking cron would otherwise stall every UI request for ten real seconds (§2.8, §4.3). |
| Where does the entry point live? | **`game/cli/game_run.py`** inside the addon — addons can ship CLI commands, so the core diff stays at zero (§2.7). |
| What drives the crons? | **The game loop**, `IrCron._process_jobs` on each tick, with `LISTEN cron_trigger` so triggers still fire immediately (§5.3). |
| What sets the game's time resolution? | **The tick interval**, replacing `SLEEP_INTERVAL = 60`. This is what lifts `DESIGN.md`'s `K <= 240` ceiling (§5.3). |
| Odoo's asset pipeline, or our own build? | **Our own**, into `static/dist/`. Served raw and untranspiled (§2.1). |
| Game state as Odoo models, or raw tables? | **Odoo models.** Migrations, constraints and shared transactions (§5.2). |
| Which dispatcher? | **`json2`** — plain JSON in and out, real status codes (§2.3). |
| How does the client know the time? | **It fetches a basis and interpolates**, refreshing the basis from the pulse. Fetching is fine; fetching at frame rate is not (§5.5). |
| Why not just interpolate from one fetch? | **Because the clock can now stop.** Clamped-from-stale freezes a live world; unclamped runs away from a dead one — 20 game hours in 55 real seconds (§5.5). |
| How often must the pulse arrive? | **More often than `max_gap`** — the same constant that bounds the server's own clock (§5.6). |
| What happens when the pulse stops? | **The UI locks**, at a threshold of `max_gap`, so it stops exactly when the world does (§5.6). |
| How is that silence measured? | **Monotonic elapsed since the last pulse arrived** — never a local wall clock against `last_tick_real`, which a skewed client gets wrong in both directions (§5.6). |
| Does locking mean freezing the clock? | **No — it means refusing actions.** A dead loop still accepts writes it can never process (§5.6). |
| Is the lock enough in the browser? | **No.** Write paths call `game_clock.is_running(...)`; the browser lock is UX, that is the guarantee (§5.6). |
| What does a reconnecting client do? | **Resync, never replay.** The bus's catch-up window is ~60 real seconds at `K = 1440` (§2.4.1, §6.9). |
| How do state changes reach the client? | **`bus.bus._sendone`** and a raw WebSocket; it crosses processes via `NOTIFY imbus` (§2.4). Same channel as the pulse. |
| Who is the player? | **An Odoo user**, `auth='user'`, with a shared `--data-dir` (§5.6). |

## 9. Open questions

1. **What should paused and locked each look like, and what should paused still
   allow?** The mechanism no longer leaves this ambiguous — pause is not silence
   (§5.6), so the client can tell live, paused and dead apart with certainty.
   What is left is genuinely a game question: both states must refuse writes that
   assume time passes, but "paused" is a state the player chose and "lost
   contact" is a fault, so they should not look alike or offer the same way out.
   Whether a paused world still allows planning, inspecting or queueing is the
   first real *design* question this work runs into, as opposed to a plumbing
   one. *(Pausing itself is settled: `DESIGN.md` §3.3.)*
2. **Whether the web-client process is needed at all in the long run.** Right
   now it is how you inspect state and how the business side is operated. If
   the game UI eventually covers everything, the second process becomes a
   development tool rather than part of the deployment.
3. **Which renderer.** Pixi, Phaser or plain canvas — not decidable until there
   is something to draw, and milestone 1 does not depend on it.
4. **How much game state is game state.** Every field is a choice between a
   `game.*` model and an existing Odoo field. Wrong in one direction the game
   reimplements Odoo; wrong in the other it contorts business records to hold
   gameplay. No general rule; decide per domain.
5. **Read path for business data.** Purpose-built projections per screen to
   start — generic read APIs are how this turns back into the web client.
