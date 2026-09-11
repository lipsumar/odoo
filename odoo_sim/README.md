# Running a game world

How to create, drive, pause and stop an odoo-sim world. For *why* any of it
works this way, see [DESIGN.md](DESIGN.md) (the clock),
[UI_DESIGN.md](UI_DESIGN.md) (the page), [GAME_STATE.md](GAME_STATE.md)
(what exists in the world, as opposed to what Odoo records) and
[MAIL.md](MAIL.md) (email, which never leaves a world).

The one idea worth having up front: **game time is not derived from the clock
on the wall.** It is a number in a table that the game loop pushes forward. No
loop, no time. That is what makes a world pausable, and it is why a world you
walk away from is exactly where you left it when you come back.

---

## Quick start

```bash
# 1. an ordinary Odoo database
odoo-bin -d mygame --stop-after-init

# 2. make it a world: 720 game seconds per real second
odoo-bin sim_init -d mygame --rate 720

# 3. start time
odoo-bin game_run -d mygame
```

Step 1 needs no `-i`: Odoo installs `base` when it initialises a database, and
everything else a world needs arrives with it. Add `--without-demo=all` if you
would rather not have Odoo's demo records in your world.

Game time is now running at 720x. One real second is twelve game minutes; one
real hour is a game month. Stop the loop with Ctrl-C and the world stops with
it.

To watch the clock from another terminal:

```bash
watch -n1 'psql -d mygame -tAc "SET search_path=public,pg_catalog; SELECT now()"'
```

`SET search_path` is the whole trick: it puts the game's `now()` ahead of
PostgreSQL's, so a plain `SELECT now()` returns game time. Without it you get
real time.

---

## Creating a world

```bash
odoo-bin sim_init -d mygame --rate 720 [--max-gap 5] [--game-start ISO] [--force]
```

| option | meaning | default |
|---|---|---|
| `--rate` | game seconds per real second | 60 |
| `--max-gap` | real seconds without a tick before the world freezes | 5 |
| `--game-start` | ISO 8601 UTC instant to start at | now |
| `--force` | overwrite an existing world | off |

The rate is fixed for the life of the world. Some reference points:

| `--rate` | one real second is | one real minute is | one real hour is |
|---|---|---|---|
| 60 | a game minute | a game hour | ~2.5 game days |
| 720 | 12 game minutes | 12 game hours | ~1 game month |
| 1440 | 24 game minutes | a game day | ~2 game months |

Above about 1440 an hourly cron starts to be finer than the tick can resolve;
see "Choosing a rate" below.

### A new world drifts a little, then stops

`sim_init` prints something like:

```
mygame is now a game world: at 2026-09-10 13:03:19, running at 720.0x once ticked
Nothing is ticking it, so it will drift 1:00:00 to 2026-09-10 14:03:19 and
stand still there. Run `odoo-bin game_run -d mygame` to start time.
```

That drift is expected. Readers interpolate forward from the last tick until
`max_gap` of real time has passed with no tick, then freeze. So an untended
world always settles exactly `max_gap × rate` past its last tick — one game
hour here — and stays there. It is bounded and harmless; it is not a leak.

---

### Do I need to install anything?

Not to run a world. `sim_init`, `game_run` and `sim_pause` are found on the
addons path and run whether or not the `odoo_sim` module is installed.

**The UI and the world are the exception.** Routes, templates and the game's
own models come from the registry, so `/game`, its API and the world's state
exist only on a database that has the module installed (it brings `mrp` and
`purchase_stock` with it):

```bash
odoo-bin -d mygame -i odoo_sim --stop-after-init
```

Then `game_run` serves the game page at `http://localhost:8069/game` alongside
the ordinary web client. The page is a Vite build that is not checked in, so
build it once (Node 20.19+ or 22.12+):

```bash
cd odoo_sim/ui
npm install
npm run build      # writes addons/odoo_sim/static/dist/
```

Until you do, `/game` says the frontend is not built and tells you how. The
clock endpoint works either way.

To work on the frontend, run `npm run dev` there instead and open
`http://localhost:5173/`. That is Vite's own page, with hot reload, talking to
Odoo's API and bus through a proxy. Log in on `:8069` first. Set `ODOO_URL` if
Odoo is not on `http://localhost:8069`. `npm test` runs the frontend's tests.

`bus` is the one module the loop actually uses, for the world pulse, and it
installs itself: it is `auto_install`, so it arrives as soon as `web` does, and
`web` is loaded by default (`--load` defaults to `base,rpc,web`). If you do
manage to end up without it, `game_run` says so and runs anyway:

```
bus is not installed on mygame: no world pulse will be published, so clients
cannot tell a running world from a dead one
```

The clock and the crons are unaffected — only a UI would notice.

## Running it

```bash
odoo-bin game_run -d mygame [--clock-tick 1] [--cron-tick 1] [--no-pulse]
```

**`game_run` is a full Odoo server.** It calls the same `server.start()` that
plain `odoo-bin` does, so the web client is there on `--http-port` (8069 by
default), you log in normally, and every ordinary Odoo option applies. You do
not need to run a second `odoo-bin` alongside it.

What it adds is two threads and one refusal: a clock thread advancing game
time, a cron thread firing crons on game time, and `--max-cron-threads=0`
forced on itself so Odoo's own poller cannot fire a cron behind the game's
back.

| option | meaning | default |
|---|---|---|
| `--clock-tick` | real seconds between advances of game time | 1.0 |
| `--cron-tick` | real seconds between cron polls | 1.0 |
| `--no-pulse` | don't publish the world pulse on the bus | off |

`--cron-tick` sets how finely crons can be scheduled: resolution is
`cron-tick × rate` game seconds. At `--rate 1440 --cron-tick 1` that is 24 game
minutes, so an hourly cron fires roughly on the hour. It is a **lower bound**,
not a guarantee — a single cron can hold the poller for ten real seconds or
more, so a busy tick simply starts the next one late.

`--clock-tick` must leave headroom under `--max-gap`, or one late tick freezes
the world. `game_run` refuses to start if it doesn't:

```
--clock-tick=4.0s leaves no headroom under this world's max_gap of 5.0s:
a single late tick would freeze the clock. Lower the tick, or raise max_gap
with sim_init --force.
```

### Threaded only, no `--workers`

`game_run` refuses to start in prefork mode:

```
game_run does not support --workers (2): the clock and cron threads live in
this process, and a prefork master forks HTTP workers out from under them,
taking their database connections apart.
```

That is not caution — it was measured. A prefork master forks its workers
*after* our threads have started and opened connections, and the inherited
sockets come apart on both sides (`cursor already closed` on the clock read).
Threaded mode, the default, is the supported shape.

### Anything else you point at the same world

You rarely need a second server, since `game_run` already serves the web
client. If you do want one — a prefork server for a heavier UI, say — start it
with `--max-cron-threads=0`:

```bash
odoo-bin -d mygame --max-cron-threads=0 --workers=4 --http-port=8070
```

Without that flag it polls crons too, on its own schedule, and the game loop is
no longer the only thing driving the world.

---

## Playing the paperclip scenario

A company that makes paperclips from wire it buys in 50 m spools
([GAME_STATE.md](GAME_STATE.md) §10):

```bash
odoo-bin -d mygame -i odoo_sim_paperclips --stop-after-init
odoo-bin game_run -d mygame
```

Then, at `http://localhost:8069`:

1. **Buy wire.** In Purchase, order Wire from *Tensile Wire Supply* and confirm
   the order. The vendor ships it; `/game` shows it under *Deliveries*,
   arriving a game day later.
2. **Take delivery.** When it is at the door, press *Accept delivery* on
   `/game`. The wire now exists. Validating the receipt in Odoo is how you
   *record* that, and the game never checks that you did.
3. **Make paperclips.** Create a manufacturing order in Odoo if you like, then
   at the *Paperclip bench* on `/game` pick a quantity (and the order), and press
   *Manufacture*. Each paperclip takes 10 cm of wire and two game minutes.
4. **Record it.** Mark the manufacturing order done in Odoo, or don't. Odoo
   will believe whatever you tell it; the world won't.

To see what the world holds, rather than what Odoo says it holds, turn on
debug mode and open *Settings → Technical → Game World*: the balance, the
ledger of every real event, the runs and the shipments, all read-only.

---

## Email

**A world's email never leaves it, and no real email comes in**
([MAIL.md](MAIL.md)). Odoo still sends everything it normally would. The game
catches it where it would have reached a mail server, and delivers it inside
the world according to the address:

- **an employee's address** goes to their inbox, under *Mail* on `/game`,
  where they can read it, reply and write new mail;
- **one of Odoo's aliases, or anything on an alias domain** goes to Odoo's own
  mail gateway, as if fetchmail had brought it in. An alias creates its record,
  and a reply lands on the chatter of the record it answers;
- **anyone else** goes to a mailbox outside the company, where the world's
  agents will read it.

There is nothing to set up for SMTP. A configured outgoing server is simply
never used, and an incoming one fetches nothing. For aliases and replies, the
company needs an alias domain, as in any Odoo: *Settings → General Settings →
Alias Domain*. Your address is your user's email, and *Notification: By
Emails* in your preferences sends Odoo's notifications to the game inbox
rather than to Discuss.

In debug mode, *Settings → Technical → Game World → Post* lists every email
sent in the world, and *Deliveries* shows where each copy went, including
anything Odoo's gateway refused and why.

---

## Pausing

```bash
odoo-bin sim_pause -d mygame            # freeze
odoo-bin sim_pause -d mygame --resume   # continue
```

Pausing is immediate and total: every reader sees the same frozen instant,
whether or not a loop is running. It settles game time up to the moment you
paused, so nothing is lost, and resuming continues from exactly there — the
paused stretch is never credited to the world.

```
simtest2 is now paused at game time 2026-09-10 18:59:31
simtest2 is now running at game time 2026-09-10 18:59:31
```

The loop keeps ticking while paused; it just doesn't move time. That is
deliberate — a paused world still publishes its pulse, so a client can tell
"paused" apart from "the loop died".

---

## Stopping

Ctrl-C, or kill the process. There is nothing to shut down cleanly: a loop that
stops ticking *is* how a world freezes.

The world keeps drifting for at most `max_gap` real seconds, settles
`max_gap × rate` past the last tick, and stays there — however long you leave
it. Restarting picks up from that instant; the first tick after a restart
carries the same clamped catch-up, so no time is invented.

This holds for `kill -9`, a crash, or closing a laptop lid, none of which get
to run any shutdown code. That is the point of `max_gap`: liveness is inferred
from ticks arriving, not from anything the process promises on its way out.

---

## Looking at a world

```bash
# game time right now
psql -d mygame -tAc "SET search_path=public,pg_catalog; SELECT now()"

# the raw clock
psql -d mygame -c "SELECT game_now, last_tick_real, rate, paused, max_gap FROM game_clock"

# is it actually running? (a large gap means nothing is ticking it)
psql -d mygame -tAc "SELECT pg_catalog.now() - last_tick_real AS since_last_tick FROM game_clock"

# crons, on game time
psql -d mygame -c "SELECT id, active, interval_number||' '||interval_type AS ival, nextcall, lastcall FROM ir_cron ORDER BY nextcall"

# the pulse the UI consumes
psql -d mygame -c "SELECT create_date, message FROM bus_bus ORDER BY id DESC LIMIT 3"
```

---

## Changing the rate

The rate is fixed for the life of a world, but you can re-create the clock over
an existing one. `--force` carries the world's current game instant across, so
it does not rewind:

```bash
odoo-bin sim_init -d mygame --rate 60 --force
```

```
Carrying the existing game instant across: 2026-09-10 21:29:26
mygame is now a game world: at 2026-09-10 21:29:26, running at 60.0x once ticked
```

Stop the loop first, or the running loop will be ticking a clock underneath
you. Pass `--game-start` as well if you *want* to move the world's clock — that
is the one way to set it deliberately, and the only way to move it backwards.

Without `--force`, `sim_init` refuses rather than clobbering a world:

```
mygame is already a game world (at 2026-09-10 21:29:26 running at rate 60.0).
Use --force to overwrite.
```

---

## Choosing a rate

`--rate` trades how fast the world moves against how finely you can schedule.
Two things bound it:

- **Cron resolution** is `cron-tick × rate` game seconds. At `--rate 1440`,
  a 1-second tick resolves to 24 game minutes, which is fine for hourly and
  daily crons and useless for minutely ones.
- **Missed occurrences collapse.** A daily cron that comes due while you were
  not looking fires *once*, not once per skipped day. Crons written as "process
  the last 24 hours" will see a wider window than they expect.

For getting a feel for a world, 60–720 is comfortable. 1440 (a game day per
real minute) is about as fast as interval crons stay meaningful.

---

## If something looks wrong

**Time isn't moving.** Either nothing is ticking the world, or it is paused.
Check both:

```bash
psql -d mygame -c "SELECT paused, pg_catalog.now() - last_tick_real AS since_last_tick FROM game_clock"
```

A `since_last_tick` larger than `max_gap` means no loop is running. `paused =
t` means someone paused it.

**`SELECT now()` returns real time.** You forgot `SET search_path = public,
pg_catalog` in that session. Odoo's own connections set it automatically.

**A typo'd database name prints a traceback**, then the real message
(`... is not a game world`). The traceback is the clock failing to read a
database that does not exist; the line underneath it is the one to read.

**Crons aren't firing.** Confirm the loop is running and check `nextcall` — it
is in game time, so compare it against `SELECT now()` with the search path set,
not against the wall clock.

**A cron got disabled on its own.** Odoo deactivates a cron that fails
repeatedly over seven days, and those are seven *game* days — about seven real
minutes at `--rate 1440`. See DESIGN.md §5.3.

**Reconnecting clients lose events.** The bus keeps its backlog for 24 game
hours by default, which is two real minutes at `--rate 720`. `game_run` warns
about this at startup and prints the value to set:

```bash
psql -d mygame -c "INSERT INTO ir_config_parameter (key, value) VALUES ('bus.gc_retention_seconds', '2592000') \
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
```

That value is one real hour at `--rate 720`; `game_run` prints the right number
for your rate.

See DESIGN.md §5.12 — the same shape catches out any duration compared against
a game-time column.

---

## A full session, start to finish

```bash
dropdb --if-exists mygame
odoo-bin -d mygame --stop-after-init
odoo-bin sim_init -d mygame --rate 720
odoo-bin game_run -d mygame              # leave running

# elsewhere
psql -d mygame -tAc "SET search_path=public,pg_catalog; SELECT now()"   # moving
odoo-bin sim_pause -d mygame
psql -d mygame -tAc "SET search_path=public,pg_catalog; SELECT now()"   # frozen
odoo-bin sim_pause -d mygame --resume
                                          # Ctrl-C the loop
psql -d mygame -tAc "SET search_path=public,pg_catalog; SELECT now()"   # frozen again
```
