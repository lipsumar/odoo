# Odoo Sim — Materialising the Game

Status: design only, nothing built
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
> **State of the premise, and one thing to settle.** The separate-process
> decision reached this document from the user directly. The parallel work on
> `DESIGN.md` §7 step 3 had recommended it but still has an open question the
> user has not answered there: *will the world ever run in prefork mode
> (`--workers=N`)?* If it will, a separate process is forced. If it is always
> `--workers=0` on one machine, a cron **thread** inside the web server becomes
> defensible — less code, and it inherits the cron watchdog at
> `server.py:509-535`. That question is upstream of this document, so it is
> worth making sure the two sides have the same answer before either is built.
> Nothing in §5 changes if the answer is "prefork"; §3.B comes back into play
> if it is not.
>
> Nothing described here is built. `odoo_sim/` is still only `DESIGN.md`.

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

### 2.5 The clock is three numbers, and they never change

`odoo/game_clock.py` — `clock_for(dbname)` returns `anchor_real`, `anchor_game`
and `rate`, and the row is immutable for the lifetime of the world
(`DESIGN.md` §3.1).

This has a consequence for the UI that is worth stating plainly, because it
inverts the obvious design: **the client should never ask the server what time
it is.** It asks once for the three constants and computes the time itself,
forever. See §5.5.

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

So the tick gets its own thread and request handling gets its own. This is
worth stating plainly because it looks like the "complexity" that counted
against running the game as a thread inside the web server (§3.B) — but it is
not the same cost. `ThreadedServer` already runs a thread per request; we are
adding exactly **one** thread, to a threading model Odoo already owns and
tests. What §3.B objected to was needing to elect *one* server process to own
the tick when there are several; that problem is absent here regardless of how
many threads this process has.

It does mean E1's "single async loop" framing was never really on the table:
whatever serves HTTP here is concurrent with a tick that blocks.

## 5. Design

### 5.1 Layout

```
odoo_sim/
  DESIGN.md            game clock
  UI_DESIGN.md         this document
  addons/
    game/
      __manifest__.py          depends: base, bus
      cli/game_run.py          the process entry point (2.7)
      models/                  game state (Odoo models)
      controllers/main.py      the page + the json2 API
      static/dist/            <- vite build output, served raw (2.1)
  ui/                          the frontend source tree, its own package.json
```

The addon lives under `odoo_sim/addons/`, added to `--addons-path`, and not in
`addons/` — that directory is upstream's, and the fork's diff against it should
stay rebaseable. Because an addon can ship a CLI command (§2.7), the whole
thing adds **nothing** to the core diff.

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

What it must keep is the `LISTEN`, so that event-driven crons (`_trigger`,
which issues `NOTIFY cron_trigger`) still wake the loop immediately instead of
waiting out the tick. It runs in **its own thread** (§4.3), started by
`game_run` after `server.start(preload=[db])` has brought the server up:

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

### 5.5 The clock endpoint returns constants, not the time

The obvious design — `GET /game/api/now` every second — is the wrong one. The
clock is immutable (§2.5), so the endpoint returns the mapping and the client
evaluates it locally at whatever frame rate it likes:

```json
{
  "anchor_real": "2026-09-10T09:00:00Z",
  "anchor_game": "2026-09-10T09:00:00Z",
  "rate": 1440.0,
  "server_real_now": "2026-09-10T14:23:07.412Z"
}
```

One request for the lifetime of the page. No polling, no socket traffic, and a
clock that stays smooth when the network does not. This is `DESIGN.md` §3.1
paying a second dividend: an immutable clock is a *cacheable* clock.

Two corrections the client must make, both of which matter more here than in a
normal application because errors are multiplied by `K`:

**Skew — do not trust the browser's wall clock.** It can be minutes off. At
`K = 1440`, one real second of skew is twenty-four game minutes. Hence
`server_real_now` in the payload: the client records `Date.now()` at receipt
and keeps the offset.

**Drift — do not use `Date.now()` to advance, either.** It jumps on NTP
correction and across sleep/wake. Use `performance.now()`, which is monotonic:

```js
// captured once, when the response lands
const t0 = Date.parse(res.server_real_now);   // server real ms
const p0 = performance.now();                 // monotonic reference

function gameNow() {
    const realNow = t0 + (performance.now() - p0);
    return anchorGame + (realNow - anchorReal) * rate;
}
```

The residual error is roughly RTT/2 — tens of milliseconds real, so tens of
*seconds* of game time at `K = 1440`. Acceptable for a displayed clock; if it
ever is not, the fix is the standard one (several samples, keep the one with
the lowest RTT), not a polling loop.

### 5.6 Push, and authentication

Time needs no push (§5.5). State changes do — a harvest completing, a delivery
arriving. Server side, one call from the tick:

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

Nobody has had to *look* at that yet. A player who closes the tab and comes
back in the morning will, immediately and unmistakably. This is the piece of
work that will force the decision `DESIGN.md` §7-7 defers — **and it is now
foreseeable enough to decide before the UI ships, rather than after a player
loses a world to it.**

Note that a separate process makes a *pause* easier to implement than it looked
in `DESIGN.md` §3.1: the game loop is a single place that can stop ticking. It
does not make it easier to implement *correctly* — game time is derived from
`pg_catalog.now()`, so stopping the loop does not stop the clock. The mutable
clock and the timestamp-tie problem are still there. Do not mistake the one for
the other.

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

## 7. Milestone 1 — the clock on a page

Deliberately no gameplay. The point is to exercise the whole spine end to end:

1. `odoo_sim/addons/game/__manifest__.py` — depends on `base`, `bus`.
2. `cli/game_run.py` — `server.start(preload=[db])` with
   `--max-cron-threads=0`, serving HTTP on its own port, plus the loop of §5.3
   in its own thread (§4.3).
3. `controllers/main.py` — `GET /game` renders the page; `GET /game/api/clock`
   returns the four values of §5.5.
4. `views/index.xml` — the minimal standalone template (§5.7).
5. `odoo_sim/ui/` — Vite project; fetch the clock once, render it with
   `requestAnimationFrame` and the skew/drift handling of §5.5.
6. Tests: the endpoint agrees with `game_clock.clock_for(db)`, and — the one
   that would have caught §6.7 — that the clock still returns game time *after*
   a tick has run.

Verifiable in one sentence: on a world created with `--rate 1440`, the page
shows a clock advancing a day a minute, and it keeps doing so with the network
disconnected.

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
| How does the client know the time? | **It computes it.** Constants once, then `performance.now()`. No polling (§5.5). |
| How do state changes reach the client? | **`bus.bus._sendone`** and a raw WebSocket; it crosses processes via `NOTIFY imbus` (§2.4). |
| Who is the player? | **An Odoo user**, `auth='user'`, with a shared `--data-dir` (§5.6). |

## 9. Open questions

1. **Pausing** (§6.3). The UI forces `DESIGN.md` §7-7. A separate process makes
   stopping the *loop* easy and stopping the *clock* no easier. Decide before
   shipping, not after.
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
