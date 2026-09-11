# Odoo Sim — Materialising the Game

Status: the clock, the loop, the pulse, the page and the clock endpoint are
built; the frontend is not.
Branch: `odoo-sim`
Target: Odoo 19.0
Companion to `DESIGN.md` (the game clock), which is assumed throughout.

> **This revision cuts the design back to a proof of concept.** Earlier
> revisions worked out, in detail, what a smooth real-time clock in the browser
> needs: local interpolation from a basis, a lock when the pulse stops, and
> three visually distinct states. That is real and still wanted, but none of it
> is needed to put the game time on a page, which is milestone 1 — so it is
> summarised in §7 and left for later, with the full analysis in git history
> (commits `6798594a` through `314b99f6`).
>
> **The websocket stays in milestone 1.** It is the transport every later screen
> needs, and the pulse it carries is already built; the POC subscribes to it and
> steps the clock forward on each message, without interpolating. This document
> now describes the smallest thing that exercises the whole spine — process,
> page, foreign build, API, and the bus — end to end.
>
> What is already built, and unchanged by this cut:
>
> - `odoo/game_clock.py` — the accumulator clock (`DESIGN.md` §3.3), with
>   `clock_for`, `tick`, `set_paused`, `is_running`, `GameClock.game_at`.
> - `addons/odoo_sim/` — `loop.py` (clock + cron threads), `pulse.py`,
>   `cli/game_run.py`, `cli/sim_pause.py`, and their tests.
> - `addons/odoo_sim/controllers/main.py` — `GET /game` renders the page,
>   `GET /game/api/clock` returns the clock reading.
> - `addons/odoo_sim/views/index.xml` — the standalone page template, with a
>   "frontend not built" fallback it currently shows.
>
> What is left: `odoo_sim/ui/`, a Vite project that subscribes to the pulse and
> displays the clock.

## 1. Problem

The clock runs, but nothing shows it. We want a UI that renders the game
world — eventually a graphical environment in the Farmville mould, initially
just the game time ticking on a page.

What the thing we build has to be, in the end:

1. A **frontend** that renders game state, with real graphics ambitions.
2. A **backend** that reads and writes the Odoo database through the ORM, so a
   game action and the business records it produces are one transaction.
3. **Its own state** — inventory of the *game*, not of the warehouse — in
   tables that are the game's, not Odoo's.
4. All of it on a **single database**, alongside the Odoo tables.

Requirement 4 is the whole premise: the Odoo database is the authoritative
record of the simulated business (`DESIGN.md` §1), so the game's own state
belongs next to it, in the same transaction scope.

Milestone 1 does none of the gameplay. It shows the game clock on a standalone
page, to prove the process boots and holds a registry, a foreign build is
served raw, the API answers, the bus pushes to the browser, and the clock the
page shows is the clock Odoo stamps records with.

## 2. What the 19.0 source lets us do

### 2.1 Odoo serves static files raw, with no pipeline in the way

`odoo/http.py:2219` — any URL of the form `/<module>/static/<path>` is read
straight off disk and streamed back, with no asset bundle, no database, no
authentication. And the JS transpiler that rewrites ES modules into Odoo's
module system is gated on the path (`odoo/tools/js_transpiler.py:725-728`): it
only fires under `/<addon>/static/src` or `/static/tests`.

So a bundle built by any toolchain we like, dropped in `static/dist/`, is
served byte-for-byte untouched. **We can host a completely foreign frontend
inside an Odoo addon without touching Odoo's asset pipeline at all** — as long
as the build output lands under `static/dist/` and never `static/src/`.

### 2.2 POS is the precedent for a standalone app

`addons/point_of_sale/controllers/main.py:36` routes `/pos/ui` outside the web
client, and `addons/point_of_sale/views/pos_assets_index.xml:5` is a
hand-written `<!DOCTYPE html>` that boots one app and nothing else — including
injecting server state as a JSON blob into a global before the scripts load,
and stubbing out the web client's menu loading so it cannot pull the backend in
behind you.

`views/index.xml` follows this pattern: a hand-written document, a bootstrap
blob in `window.odooSim`, no `t-call-assets`, no reference to
`web.assets_backend`.

### 2.3 There is a plain-JSON dispatcher, not just JSON-RPC

`odoo/http.py:2638` — `type='json2'` takes the request body as the parameters,
returns the endpoint's value as the response body, and maps exceptions onto
real HTTP status codes. `type='jsonrpc'` is the JSON-RPC 2.0 envelope the web
client's RPC layer speaks — always HTTP 200, errors in the body. A frontend
written against `fetch` wants `json2`, and `/game/api/clock` uses it.

### 2.4 The clock is an accumulator owned by the game loop

**`DESIGN.md` §3.3.** Game time is `(game_now, last_tick_real, rate, paused,
max_gap)`, advanced by the loop once per tick. It only advances when something
ticks it: `paused` stops it deliberately, `max_gap` caps what a dead loop can
accrue. `odoo/game_clock.py` exposes what the UI backend builds on:

| | |
|---|---|
| `clock_for(dbname, cr=None)` | a reading — `game_now`, `last_tick_real`, `rate`, `paused`, `max_gap` |
| `is_running(clock)` | `False` iff paused or the loop is dead; `True` for a non-game database |
| `GameClock.game_at(real)` | the clamped interpolation, mirroring `public.now()` |

Two constants: `DEFAULT_MAX_GAP` is **5 real seconds**, `CACHE_TTL` is **1 real
second**. A reading may be up to a second stale; for a displayed clock that is
nothing.

For the POC the page reads `game_now` and shows it. It does not interpolate, so
the age of a reading does not matter — the clock steps forward once per pulse
rather than sweeping. §7 is where that stops being good enough.

### 2.5 The bus is reachable with no Odoo JavaScript

`addons/bus/controllers/websocket.py:11` — `/websocket` is
`type="http", auth="public", cors='*', websocket=True`, and the protocol on the
wire is plain JSON: `addons/bus/websocket.py:909` reads
`jsonrequest['event_name']`, and `:941` handles `subscribe` with
`{"channels": [...], "last": <id>}`. So a raw browser `WebSocket` and one
subscribe frame is the whole client — no `bus_service`, no `SharedWorker`, no
OWL.

Server side, `addons/odoo_sim/pulse.py` already publishes. `pulse.send(cr,
clock)` calls `env['bus.bus']._sendone` on channel `odoo_sim.world` with type
`odoo_sim.pulse`, and the game loop calls it once per tick, unconditionally
(`loop.py`). The clock endpoint and the pulse carry the **same payload shape**
(`pulse.payload`), so the browser has one parser for both.

The bus fans out through `NOTIFY imbus` on the `postgres` database
(`bus.py:164`), so a `_sendone` from the loop reaches any connected browser
regardless of which thread or process sent it — nothing to plumb across the
boundary.

## 3. Where the code lives — decided and built

The game runs as **a process that embeds the Odoo ORM**: `odoo-bin game_run -d
world` imports Odoo, runs its serving bootstrap, and runs the clock and cron
threads beside the request threads. It is a full Odoo server — it hosts the web
client on the same port — plus a loop.

Why embed the ORM rather than reach Odoo over RPC from a separate service: a
game action is naturally *"spend 100 coins **and** confirm the sale order"*.
Split across an RPC boundary those are two transactions with no shared commit.
Embedded, they are one `cr`.

Why a dedicated command rather than an addon thread inside a plain `odoo-bin`:
the command owns its own configuration. It forces `--max-cron-threads=0` so
Odoo's poller cannot fire a cron behind the loop's back, refuses `--workers`
(prefork forks the clock threads out from under themselves — measured), and
refuses a `--clock-tick` with no headroom under `max_gap`. An addon in someone
else's server can only hope the right flags were passed.

The full options analysis (five candidates, the transactional objection to RPC,
the prefork measurement) is in `git log` and commit `4c4ad16a`. It is decided
and built; this document does not re-argue it.

| | embedded process (chosen) |
|---|---|
| ORM access | direct |
| One transaction with Odoo | yes |
| Deterministic tick | yes — the loop owns the interval |
| Auth | free — `auth='user'`, one session store |
| Frontend freedom | full |
| Cache signalling | free — Odoo's own dispatcher |

**One process, one database, one command:**

```bash
odoo-bin game_run -d world
```

The clock, the crons, `/game`, `/game/api/clock` and the Odoo web client are
all on `--http-port` (8069). Do not pass `--max-cron-threads=0`; `game_run`
forces it on itself.

## 4. The frontend stack: Vite and vanilla JavaScript

No React, no Vue, no OWL. A framework earns its place when there is shared
mutable state across many components, and milestone 1 is one div fed by one
websocket. Vite is there for three things wanted on day one, none of which imply
a framework:

- a dev server with HMR,
- a proxy for `/game`, `/game/api` and `/websocket`, so the browser talks only
  to Vite and there is no CORS and the session cookie is same-origin,
- a content-hashed production build.

Whether the eventual *renderer* wants a library (canvas, Pixi, Phaser) is a
separate and still-open question (§9) — a renderer is not a DOM framework.

## 5. Design

### 5.1 Layout

```
odoo_sim/
  DESIGN.md            game clock
  UI_DESIGN.md         this document
  README.md            how to run a world
  ui/                  the Vite project, its own package.json     <- TO BUILD

addons/odoo_sim/       depends: base, bus
  loop.py              GameLoop: the clock and cron threads, and the refusals
  pulse.py             CHANNEL / TYPE / payload() / send()
  cli/game_run.py      argument parsing + the serving bootstrap
  cli/sim_pause.py     pause / resume
  controllers/main.py  GET /game  +  GET /game/api/clock
  views/index.xml      the standalone page template
  static/dist/         vite build output, served raw (§2.1)      <- TO BUILD
  tests/
```

`game_run` works whether or not the module is *installed* — command discovery
reads the addons path, not the database. The **UI is the exception**: `/game`
and `/game/api/clock` are registry routes, so they need `-i odoo_sim`.

### 5.2 The clock endpoint — built

`GET /game/api/clock`, `type='json2'`, `auth='user'`. Returns the reading from
`game_clock.clock_for(...)` — the same dict `pulse.payload` produces (§2.5), so
the client parses the fetch and the pulse with one code path:

```json
{
  "game_now":        "2026-09-10T14:23:07.000000",
  "last_tick_real":  "2026-09-10T13:26:04.000000",
  "rate":            1440.0,
  "paused":          false,
  "max_gap":         5.0,
  "running":         true,
  "server_real_now": "2026-09-10T13:26:04.412000"
}
```

For the POC the page uses `game_now` and `paused`. The rest — `rate`,
`last_tick_real`, `max_gap`, `server_real_now` — is what an interpolating client
needs later (§7); it costs nothing to send now.

404 if the database is not a game world. The endpoint deliberately does **not**
refuse when the loop is dead — it is the read that tells a client the world has
stopped. `is_running` guards belong on write paths.

It is the cold-start and reconnect path. Once connected, the client tracks the
clock from the pulse (§5.4); the fetch is what it does before the socket is open
and after it drops.

### 5.3 The page — built

`GET /game` renders `odoo_sim.index`: a hand-written document (§2.2) behind
`auth='user'`, carrying a bootstrap blob with the first clock reading, the bus
channel, and the notification type. The controller reads
`static/dist/.vite/manifest.json` at render time to find the hashed entry
filename; if nothing is built it renders a "frontend is not built" fallback
that still carries the live clock blob in `window.odooSim`.

### 5.4 The client — to build

`odoo_sim/ui/`, a Vite vanilla-JS project. The websocket is in milestone 1
deliberately: it is the transport every later screen uses (game events ride the
same channel), and standing it up now against a payload that is only a clock is
the cheapest it will ever be to get right.

Milestone 1:

1. Read the bootstrap blob from `window.odooSim` (the first clock reading, the
   channel name, the type) and paint `game_now` into a single div.
2. Open a `WebSocket` to `/websocket`, send one subscribe frame
   `{"event_name": "subscribe", "data": {"channels": ["odoo_sim.world"], "last": 0}}`
   (`ir.websocket._subscribe` reads both keys directly, so `last` is not
   optional). Each inbound frame is a JSON **array** of
   `{"id", "message": {"type", "payload"}}`; for every element whose
   `message.type` is `"odoo_sim.pulse"`, write `message.payload.game_now` into
   the div.
3. `fetch('/game/api/clock')` once before the socket is open, and again on
   `close`/`error` before reconnecting, so the div is never blank and a
   reconnect resyncs rather than resumes (§7).
4. If `paused` is true, say so. If the socket is closed and the fetch fails, say
   that.

That is the whole POC. **No interpolation, no lock** — the clock steps forward
by `rate` game seconds on each pulse (~1 s) rather than sweeping, and a dead
loop shows as a frozen number rather than a locked screen. Both are fine for
proving the spine, and both are what §7 adds next.

**Development:** `npm run dev`, Vite on `:5173`, proxying `/game`, `/game/api`
and `/websocket` (with `ws: true`) to `:8069`. The browser talks only to Vite:
no CORS, the session cookie is same-origin, and the websocket upgrade rides the
same proxy.

**Production:** `npm run build` into `addons/odoo_sim/static/dist/`. Filenames
must be content-hashed — `STATIC_CACHE` is seven days (`odoo/http.py:334`), so
an unhashed `main.js` will be stale in every browser that has seen it. Vite
hashes by default; the point is not to turn it off.

### 5.5 Game state, when it comes — not milestone 1

Declare game state as ordinary Odoo models — `game.player`, `game.plot` — which
gives tables `game_player`, `game_plot` in the same database as Odoo's own, so
a write to `game_plot` and a write to `stock_move` land in one `cr` and commit
or roll back together. Migrations, constraints and computed fields come free
with module upgrade.

**One trap, inherited from the clock.** `create_date` and `write_date` on these
tables carry **game** time, because `cr.now()` is the game clock (`DESIGN.md`
§4.2). For gameplay state that is correct. For meta-state — when a player last
logged in, telemetry, anything an operator reads — it is wrong and silently so:
at `K = 1440` a `write_date` from an hour ago reads as two months old.
Meta-state needs an explicit real-time column.

## 6. Risks

### 6.1 `static/` is public

`Stream.from_path(filepath, public=True)` — no authentication on anything under
`static/`. Correct for a JS bundle, wrong for game data. All per-player
bootstrap state goes in the rendered template behind `auth='user'` (§5.3),
never in a static JSON file next to the bundle.

### 6.2 Two toolchains

An `npm` build joins the repo. Keep the boundary sharp: the addon is Python and
consumes `static/dist/` as an opaque artefact; the frontend is JavaScript and
consumes the API as an opaque contract. When debugging the *game*, the tool is
`odoo-bin shell`, not the browser.

### 6.3 The transpiler gate is a path convention

§2.1 — the build output must not land under `static/src/`, or the transpiler
will rewrite it into an Odoo module and break it. `static/dist/` by convention,
with no enforcement behind it.

### 6.4 Game-time magic columns on the game's own tables

See §5.5. Silent, and only shows up when someone reads a `write_date` and
believes it.

## 7. Deferred — what a real-time clock needs beyond stepping

Milestone 1 steps the clock once per pulse and trusts whatever number arrives.
Earlier revisions of this document worked out what a production clock needs; the
summary is kept here so it is not re-derived from scratch, and the full
treatment with measurements is in git history (commits `6798594a`, `206f54b8`,
`8fd0c55f`, `42dc412f`, `146a2c0d`, `314b99f6`).

- **Interpolation.** Stepping once per pulse makes the clock lurch `rate` game
  seconds at a time. A smooth clock takes each pulse as a *basis* and advances
  it locally between pulses with `performance.now()` (monotonic — `Date.now()`
  jumps on NTP correction and sleep/wake), clamping elapsed time exactly as
  `GameClock.game_at` does. This is why the payload already carries `rate`,
  `last_tick_real`, `max_gap` and `server_real_now`.

- **Parse timestamps as UTC, explicitly.** `pulse.payload` emits naive ISO
  strings with no offset (`"2026-09-12T22:38:10.473543"`). JavaScript parses a
  date-*time* with no offset as **local time**. The clock still ticks at the
  right rate (the offset cancels in a difference) but the displayed absolute
  time is wrong by the browser's UTC offset × `K` — up to hundreds of game
  days. Append `Z` at the parse boundary, once. Do not use Odoo's
  `deserializeDateTime` (it is `fromSQL`, and it also converts to the user's
  timezone).

- **Lock when the pulse stops.** Because the clock is an accumulator, a browser
  interpolating from a stale basis cannot tell a stopped clock from a running
  one, and the divergence is silent (~20 game hours after 55 real seconds of
  silence at `K = 1440`). When pulses stop arriving for `max_gap` — measured as
  monotonic elapsed since the last arrival, never a wall clock against
  `last_tick_real` — the UI must lock: not just freeze the clock but **refuse
  actions**, because a dead loop still accepts writes it can never process. The
  server-side guard is `game_clock.is_running(...)` on write paths; the browser
  lock is only UX.

- **Paused and locked must look different.** Paused is a state the player chose
  and offers a way out; locked is a fault, names what is wrong, and offers no
  resume. One shared grey overlay teaches the player that the game freezing is
  normal.

- **Reconnect is a resync, not a replay.** The bus's catch-up window is
  denominated in game seconds (`bus._gc_messages` runs on game time), so at
  `K = 1440` it is about 60 real seconds — and a client coming out of a lock
  was offline longer than that. The reconnect handler must re-fetch state, not
  resume a notification stream.

## 8. Milestone 1 — the clock on a page

| # | | |
|---|---|---|
| 1 | `__manifest__.py`, depends `base`, `bus` | **done** |
| 2 | `cli/game_run.py` — serving bootstrap, `--max-cron-threads=0` forced, clock + cron threads split | **done** |
| 3 | `pulse.py` — `odoo_sim.pulse` on `odoo_sim.world`, every tick | **done** |
| 4 | `controllers/main.py` — `GET /game`, `GET /game/api/clock` | **done** |
| 5 | `views/index.xml` — standalone template + unbuilt fallback | **done** |
| 6 | `odoo_sim/ui/` — Vite vanilla JS: read the blob, subscribe to `odoo_sim.world`, step `game_now` into a div from each pulse, fetch on cold start and reconnect | **to build** |
| 7 | tests (below) | partly done |

Tests, in `addons/odoo_sim/tests/`:

- the clock endpoint agrees with the `game_clock` row — **done**
  (`test_controllers.py`);
- the clock still returns game time after a tick has run — **done**;
- the page renders, and renders the fallback when nothing is built — **done**;
- once the frontend exists: it parses a payload whose microseconds are zero
  (Python's `isoformat()` drops the fractional part when it is zero), it
  displays the `game_now` from a pulse message, and it falls back to the fetch
  when the socket closes.

Run: `odoo-bin -d <db> -i odoo_sim --test-tags /odoo_sim --stop-after-init`.

**Verifiable in one sentence:** on a world at `--rate 1440`, `/game` shows a
clock that advances about a day a minute, in one-second steps.

## 9. Open questions

1. **What should a paused world still allow?** Both paused and locked refuse
   writes that assume time passes. Whether a paused world lets the player plan,
   inspect, or queue an action for resume is the first real *game*-design
   question this work reaches. Queueing in particular needs somewhere to hold
   an intent that is not yet a record.
2. **Which renderer.** Pixi, Phaser or plain canvas — not decidable until there
   is something to draw. The vanilla-JS decision (§4) rules out a *DOM*
   framework and does not prejudge this.
3. **How much game state is game state.** Every field is a choice between a
   `game.*` model and an existing Odoo field. Wrong one way the game
   reimplements Odoo; wrong the other it contorts business records to hold
   gameplay. No general rule; decide per domain.
4. **Read path for business data.** Purpose-built projections per screen to
   start — generic read APIs are how this turns back into the web client.
5. **Whether the Odoo web client is wanted long-term.** Right now it is how you
   inspect state and operate the business side. If the game UI eventually
   covers everything, whether to keep serving it is a `--load` question.
