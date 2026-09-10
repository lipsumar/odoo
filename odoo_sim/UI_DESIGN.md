# Odoo Sim — Materialising the Game

Status: design only, nothing built
Branch: `odoo-sim`
Target: Odoo 19.0
Companion to `DESIGN.md` (the game clock), which is assumed throughout.

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

The question this document answers is where the code lives: inside Odoo as an
addon, outside it as a separate service, or some split.

## 2. Findings from the 19.0 source

Five things that turned out better than expected, and that shape the answer.

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

### 2.4 The bus is reachable without any Odoo JavaScript

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

So the game frontend opens a `new WebSocket('/websocket')`, sends one subscribe
frame, and receives pushes. Odoo's own client uses a SharedWorker
(`/bus/websocket_worker_bundle`) to share one socket across browser tabs; we do
not have to. `/websocket/peek_notifications` is a polling fallback, also public
and CORS-open.

### 2.5 The clock is three numbers, and they never change

`odoo/game_clock.py` — `clock_for(dbname)` returns `anchor_real`, `anchor_game`
and `rate`, and the row is immutable for the lifetime of the world
(`DESIGN.md` §3.1).

This has a consequence for the UI that is worth stating plainly, because it
inverts the obvious design: **the client should never ask the server what time
it is.** It asks once for the three constants and computes the time itself,
forever. See §5.4 — it is the single biggest simplification available to us,
and it falls out of a decision already made for unrelated reasons.

## 3. The options

### A. A client action inside the Odoo web client

An OWL component registered as `ir.actions.client`, assets in
`web.assets_backend`, reached from a menu item.

- **For:** the least new machinery of any option. Free ORM, session, RPC. The
  game is a menu item next to Sales. Odoo's own list and form views remain
  available beside it for inspecting state.
- **Against:** the entire backend bundle loads around what is meant to be a
  canvas. The web client owns the viewport, the keyboard, the URL and the
  navigation, and every one of those is something a game eventually wants. OWL
  is a DOM component framework — it offers nothing to a WebGL renderer and gets
  in its way. The asset pipeline's dev loop (`--dev=xml,reload`, bundle
  regeneration) is poor next to a modern frontend toolchain.
- **Verdict:** right for a debug panel, wrong for the destination. The cost is
  not visible at "display the clock" and becomes the dominant cost later.

### B. An Odoo addon that serves a standalone page

The addon is a *server*: models, controllers, the game loop. The page it serves
is outside the web client entirely, POS-shaped (§2.2).

Two sub-variants, differing only in who builds the frontend:

- **B1 — Odoo's asset bundle and OWL**, exactly as POS does it. Keeps one build
  system, but keeps OWL and the pipeline too, so it inherits most of A's
  objections while giving up A's ORM-views-alongside convenience.
- **B2 — our own toolchain**, built into `static/dist/` and served raw (§2.1).
  Any renderer (Pixi, Phaser, three.js), npm, TypeScript, HMR.

- **For (B2):** full frontend freedom with zero pipeline friction, while the
  backend keeps everything that makes building on Odoo worthwhile — the ORM,
  the registry, authentication, and above all **one transaction spanning the
  game's tables and Odoo's**.
- **Against:** two build systems in the repo. The frontend must be disciplined
  about not reaching for web-client services that are not loaded.
- **Verdict:** the recommendation. See §4.

### C. A separate service, talking to Odoo over RPC

A FastAPI/Node backend beside Odoo, reaching it through JSON-RPC.

- **For:** total freedom on both sides; independent deployment and scaling.
- **Against:** everything the addon gets for free has to be rebuilt — session
  bridging, access control, and per-record round-trips where the ORM would do
  one query. Two migration tools own tables in one database.

  The decisive objection is transactional. A game action is naturally *"spend
  100 coins **and** confirm the sale order"*. Split across two processes, those
  are two transactions with no shared boundary, and every interesting action
  becomes a distributed-commit problem. There is no version of this that is not
  a saga.

  Secondary, but telling: `game_clock.current_clock()` resolves the database
  from `threading.current_thread().dbname` (`game_clock.py`, `DESIGN.md` §5.8).
  A foreign process has no such thread and no such clock — it would read game
  time through `public.now()` in SQL, or reimplement the arithmetic. A third
  copy of the formula is exactly what `DESIGN.md` §4.2 was avoiding.
- **Verdict:** rejected for writes. Worth revisiting only if the game backend
  ever has to scale independently of Odoo, which is not a problem we have.

### D. A separate service, direct SQL against the Odoo tables

- **Against:** writing past the ORM skips computed fields, constraints,
  `ir.rule`, tracking and cache invalidation. The database's invariants live in
  Python, not in the schema. Reading past it is merely risky; writing past it
  corrupts.
- **Verdict:** non-starter. `game_clock` is raw SQL because it is read from
  *below* the ORM (`odoo/sql_db.py`) and must not depend on a registry — a
  bootstrap constraint, not a precedent for gameplay.

### Summary

| | A: client action | B2: addon + own frontend | C: RPC service | D: direct SQL |
|---|---|---|---|---|
| ORM access | direct | direct | round-trip | none |
| One transaction with Odoo | yes | **yes** | no | unsafe |
| Auth | free | free | bridge it | bridge it |
| Push | free | one raw socket | rebuild | rebuild |
| Frontend freedom | poor | **full** | full | full |
| Dev loop | asset pipeline | **HMR** | HMR | HMR |
| Deploy units | 1 | **1** | 2 | 2 |
| Game clock | free | **free** | reimplement | SQL only |

## 4. Recommendation

**Build an Odoo addon whose job is to be a game server, not a web-client
screen.** Option B2.

The reasoning in one line: the reason to build the game on Odoo at all is that
the ORM enforces the business rules of the simulated company, and the only way
to keep that benefit is to run the game's own writes inside the same
transaction as Odoo's. That requires being in the Odoo process. Nothing else
about Odoo's frontend has to come along.

The addon is also the natural home for the outstanding step 3 of `DESIGN.md`
(§7) — running with `--max-cron-threads=0` and driving `IrCron._process_jobs`
from the game loop. **The UI backend and the game loop are the same
component**, and building them as two would be the first mistake to avoid: one
process that owns the clock, the tick, the game state and the API.

## 5. Design

### 5.1 Layout

```
odoo_sim/
  DESIGN.md            game clock
  UI_DESIGN.md         this document
  addons/
    game/
      __manifest__.py          depends: base, bus
      models/                  game state (Odoo models)
      controllers/main.py      the page + the json2 API
      static/dist/            <- vite build output, served raw (2.1)
  ui/                          the frontend source tree, its own package.json
```

The addon lives under `odoo_sim/addons/`, added to `--addons-path`, and not in
`addons/`. That directory is upstream's, and the fork's diff against it should
stay small enough to rebase (`DESIGN.md` counts 36 inserted lines across five
files — worth protecting).

The frontend source lives outside the addon, and only its build output lands
inside. Source and build artefacts should not share a tree.

### 5.2 The game's own state: Odoo models, not raw tables

Declare game state as ordinary Odoo models — `game.player`, `game.plot` — which
gives tables `game_player`, `game_plot`, entirely separate from Odoo's own, in
the same database. That satisfies the separation requirement without inventing
anything.

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

### 5.3 The API surface

`type='json2'` routes (§2.3) under a single prefix:

```python
@http.route('/game/api/clock', type='json2', auth='user', methods=['GET'])
def clock(self):
    ...
```

`json2` and not `jsonrpc`: the frontend is `fetch`, not Odoo's RPC layer, and
we want HTTP status codes on errors rather than an envelope that is always 200.

### 5.4 The clock endpoint returns constants, not the time

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
correction and across sleep/wake. Use `performance.now()`, which is monotonic,
for elapsed time since the response arrived:

```js
// captured once, when the response lands
const t0 = Date.parse(res.server_real_now);   // server real ms
const p0 = performance.now();                 // monotonic reference

function gameNow() {
    const realNow = t0 + (performance.now() - p0);
    return anchorGame + (realNow - anchorReal) * rate;
}
```

The residual error is the response's one-way latency, roughly RTT/2 — tens of
milliseconds real, so tens of *seconds* of game time at `K = 1440`. Acceptable
for a displayed clock; if it ever is not, the fix is the standard one (several
samples, keep the one with the lowest RTT), not a polling loop.

### 5.5 Push, for state and not for time

Time needs no push (§5.4). State changes do — a harvest completing, a delivery
arriving. Server side, one call (§2.4):

```python
self.env['bus.bus']._sendone(channel, 'game.event', payload)
```

Client side, a raw `WebSocket` to `/websocket` and one subscribe frame. No
Odoo JavaScript, no SharedWorker.

**Deployment note.** In threaded mode the websocket is served in-process
(`odoo/service/server.py:206-237`). Under `--workers > 0` it is routed to a
separate gevent process on `gevent_port` (`server.py:776,817`), which needs a
reverse-proxy rule. Standard Odoo deployment, but it is the one thing about the
game UI that is not `odoo-bin` and a browser.

### 5.6 The dev loop

**Development.** Vite on `:5173` with a proxy for `/game/api`, `/websocket` and
`/web/session` to `:8069`. The browser talks only to Vite, so there is no CORS
to configure and the session cookie is same-origin. Odoo serves no frontend at
all in this mode; HMR is untouched by anything Odoo does.

**Production.** `vite build` into `odoo_sim/addons/game/static/dist/`, served
raw (§2.1). Filenames must be content-hashed — `STATIC_CACHE` is seven days
(`odoo/http.py:334`) and the response carries it, so an unhashed `main.js` will
be stale in every browser that has seen it. Vite hashes by default; the point
is not to turn it off.

The page itself is a minimal template in the POS mould (§2.2): a `<!DOCTYPE
html>`, the bootstrap JSON, and a `<script type="module">` pointing at the
built entry. Because the entry filename is hashed, the controller should read
`static/dist/.vite/manifest.json` at render time to find it rather than
hardcoding a name.

### 5.7 Authentication

For v1, `auth='user'`: the player is an Odoo user, logs in through Odoo's
normal login page, and the session cookie carries. Nothing to build.

If the game later wants its own accounts, decoupled from `res.users`, that is
an `auth='public'` route plus a game-side session — a real piece of work, and
no reason to do it before there is a second player.

### 5.8 What is deliberately not in the addon

The `game_clock` table and `odoo/game_clock.py` stay where they are, in core.
They are read below the ORM by `odoo/sql_db.py`, before any registry exists,
and cannot depend on a module being installed (§3.D).

The addon reads the clock through `game_clock.clock_for(...)` and exposes it.
It does not own it.

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

Nobody has had to look at that yet. A player who closes the tab and comes back
in the morning will, immediately and unmistakably. This is the piece of work
that will force the decision `DESIGN.md` §7-7 defers — **and it is now
foreseeable enough to decide before the UI ships, rather than after a player
loses a world to it.**

### 6.4 Two toolchains

An `npm` build joins the repo. Keep the boundary sharp: the addon is Python and
consumes `static/dist/` as an opaque artefact; the frontend is TypeScript and
consumes the API as an opaque contract. When debugging the *game*, the tool is
`odoo-bin shell`, not the browser.

### 6.5 The transpiler gate is a path convention

§2.1 — the build output must not land under `static/src/`, or the transpiler
will rewrite it into an Odoo module and break it. `static/dist/` by convention,
and it is a convention with no enforcement behind it.

## 7. Milestone 1 — the clock on a page

Deliberately no gameplay. The point is to exercise the whole spine end to end,
so that everything after it is filling in a shape that is known to work:

1. `odoo_sim/addons/game/__manifest__.py` — depends on `base`, `bus`.
2. `controllers/main.py` — `GET /game` renders the page; `GET /game/api/clock`
   returns the four values of §5.4.
3. `views/index.xml` — the minimal standalone template (§5.6).
4. `odoo_sim/ui/` — Vite project; fetch the clock once, render it with
   `requestAnimationFrame` and the skew/drift handling of §5.4.
5. A test that the endpoint agrees with `game_clock.clock_for(db)`.

Verifiable in one sentence: on a world created with `--rate 1440`, the page
shows a clock advancing a day a minute, and it keeps doing so with the network
disconnected.

What that proves: the addon loads on the sim addons path, a standalone page
boots outside the web client, a foreign build is served raw, the API answers,
and the clock the browser shows is the clock Odoo is stamping records with.

Only then: a second endpoint that writes, to prove the transaction story of §4.

## 8. Decisions

| Question | Decision |
|---|---|
| Addon, or separate service? | **Addon.** One transaction spanning game state and Odoo records is the reason to build on Odoo at all (§4). |
| Inside the web client, or standalone? | **Standalone page**, POS-shaped. The web client's chrome and asset pipeline are pure cost for a canvas (§3.A). |
| Odoo's asset pipeline, or our own build? | **Our own**, into `static/dist/`. Served raw and untranspiled (§2.1). |
| Game state as Odoo models, or raw tables? | **Odoo models.** Migrations, constraints and shared transactions. `game_clock` stays raw for bootstrap reasons only (§5.2). |
| Which dispatcher? | **`json2`** — plain JSON in and out, real status codes (§2.3). |
| How does the client know the time? | **It computes it.** Constants once, then `performance.now()`. No polling (§5.4). |
| How do state changes reach the client? | **`bus.bus._sendone`** and a raw WebSocket. No Odoo JS (§5.5). |
| Who is the player? | **An Odoo user**, `auth='user'`, for v1 (§5.7). |
| Where does the game loop live? | **In this addon**, together with the API. It is `DESIGN.md` §7 step 3 (§4). |

## 9. Open questions

1. **Pausing** (§6.3). The UI forces `DESIGN.md` §7-7. Decide before shipping,
   not after.
2. **Which renderer.** Pixi, Phaser or plain canvas — not decidable until there
   is something to draw, and milestone 1 does not depend on it. The build
   output is opaque to the addon either way (§6.4).
3. **How much game state is game state.** Every field is a choice between a
   `game.*` model and an existing Odoo field. Wrong in one direction the game
   reimplements Odoo; wrong in the other it contorts business records to hold
   gameplay. No general rule; decide per domain as each enters the game.
4. **Read path for business data.** Whether the frontend gets a purpose-built
   projection per screen, or something generic over the ORM. Purpose-built to
   start — generic read APIs are how this turns back into the web client.
