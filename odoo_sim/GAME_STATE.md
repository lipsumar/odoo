# Odoo Sim — The Game World's Own State

Status: built for manufacturing and purchasing, with the paperclip scenario (§10)
Branch: `odoo-sim`
Target: Odoo 19.0
Companion to `DESIGN.md` (the clock), `UI_DESIGN.md` (the page) and `MAIL.md`
(email, which never leaves a world).

## 1. Problem

Odoo records what the company *says* happened. Nothing stops the player marking
a manufacturing order done for 100 products that were never made, or
validating a receipt for goods that never arrived — and nothing should. That
is ordinary Odoo, and keeping records honest is part of running a business.

So the game needs its own account of **what actually exists**, which Odoo
models and never defines. v1 covers two ways goods come into the world:

- **Manufactured** goods exist once they are made at a workstation *in the
  world*. The player presses a button at the station and waits while it
  works; recording the production on the MO in Odoo comes after, if at all.
- **Purchased** goods exist once their delivery has been accepted *in the
  world*. The player confirms a purchase order; the vendor ships it, and it
  arrives after the vendor's lead time; the player accepts it at the door.
  Validating the receipt in Odoo comes after, if at all.

Later, autonomous employees will press the same buttons. Email is a system of
its own (`MAIL.md`); the vendor does not read it yet.

## 2. The rule

**The world is the truth; Odoo is the player's account of it.** From that:

- The world **never reads Odoo data the player can still edit** to decide what
  happened. When an event happens, the world copies what it needs at that
  moment (§6.2) and does not look back.
- The game **does not stop Odoo lying**. There are no guards on
  `button_mark_done` or `button_validate`. Making Odoo match reality is the
  player's goal. Consequences come from the world instead: a workstation cannot
  consume wire that is not there (§3.2).
- Links from the world to Odoo documents (a run's manufacturing order, a
  shipment's purchase order) are **informational**. Nothing reads them back.

Checkpoints where the game compares reality against Odoo are out of scope.

## 3. Reality: a ledger and a balance

### 3.1 Two models

In `addons/odoo_sim/models/game_stock.py`, as ordinary Odoo models so that
every world write shares a transaction with the Odoo writes around it
(UI_DESIGN.md §5.5):

| model | what it is |
|---|---|
| `game.stock.entry` | append-only ledger: `date`, `product_id`, signed `qty`, `kind` (`genesis` / `manufactured` / `consumed` / `received`), and the world event responsible (`production_id` or `shipment_id`) |
| `game.stock` | one row per product: `qty`, with `CHECK (qty >= 0)` |

The balance is a cache of the ledger's sum, kept for one reason: a `CHECK` on a
single row is what makes "you cannot consume what is not there" hold under
concurrency. The ledger is the history, which explains any gap between the
world and Odoo.

Quantities are in the product's own unit (`uom_id`), and **NUMERIC, not
float8**: a `Float` with `digits` gets a `numeric` column
(`odoo/orm/fields_numeric.py:126-133`). With float8, `0.3 - 0.1 - 0.1 - 0.1` is
`-2.8e-17`, and the third paperclip would be refused with the wire sitting on
the bench. A test pins this, and fails with the digits removed.

### 3.2 One write path

`game.stock._apply(product, qty, kind, *, date, production, shipment)` is the
**only** way reality changes. It writes the ledger row and updates the balance
in the caller's transaction. A take is a single
`UPDATE ... WHERE qty + delta >= 0`; if no row changes, it raises `UserError`
("There is not enough Wire in the world…") and the whole action rolls back.
An addition is an upsert.

`date` is the game instant the event happened, not when it was processed: a
run that ended at 14:00 and was settled by a cron at 14:07 happened at 14:00.

### 3.3 Genesis

A world is created on a database that may already hold stock. On install
(`post_init_hook`), and on upgrade from 1.0 (`migrations/1.1/`),
`game.world._genesis()` copies the positive internal-location quants in as
`genesis` entries. That is the one moment Odoo is taken at its word, so Odoo
and the world agree at the start, and every later difference is one the game
caused. It does nothing once the ledger has any entry.

### 3.4 What is tracked

Keyed on `product.product`. Every physical good can have a balance, whether or
not Odoo tracks its inventory. Odoo 19's "Track Inventory"
(`is_storable`, `addons/stock/models/product.py:829`) is a bookkeeping choice,
and reality does not care about it. Services never get a balance.

No locations yet: the world is one site.

## 4. The world's physics: recipes

`game.recipe` (one per product): what one unit consumes (`game.recipe.line`,
in each component's unit) and how long it takes, in game minutes per unit.

**The BoM is the player's model of the recipe, not the recipe.** A workstation
consumes what the recipe says, whatever the BoM says. If the two disagree,
marking an MO done consumes the wrong components in Odoo, Odoo drifts from
reality, and the player has something to notice. Had the workstation followed
the BoM, an empty BoM would make free products, which is exactly the lie the
world exists to catch.

Recipes are scenario data for now. How a player creates recipes in an open
world is a later question.

## 5. Manufacturing: workstations and runs

In `models/game_workstation.py`.

`game.workstation` has a `recipe_id` (what it makes), and an informational
`workcenter_id` naming the Odoo work center that models it.

Pressing the button is `workstation._start(qty, mrp_production=None)`. It
creates a `game.production` run:

- **Components leave the world when the run starts**, because that is when
  they are taken off the shelf. A run without enough wire does not start at
  all.
- **Goods enter it when the run ends**, at
  `date_end = date_start + duration × qty`, settled by §7.
- **One run at a time per station.** A station is a physical thing. The station
  row is locked `FOR UPDATE` so two presses at once cannot both find it free.
- **The MO link is informational.** It must be an MO for the product the
  station makes, and an MO cancelled afterwards changes nothing.

## 6. Purchasing: vendors and shipments

In `models/game_vendor.py` and `models/purchase_order.py`.

### 6.1 The vendor agent

`game.vendor` is an automated supplier: a `partner_id`, a `lead_time` in game
hours, and a catalogue (`product_ids`) of what it can actually ship.

Approving a purchase order (`button_approve`, so that double validation is
covered; `purchase_stock` also creates the receipt there,
`addons/purchase_stock/models/purchase_order.py:179`) **only triggers the
vendor-agent cron**. The agent reads every confirmed order sent to a vendor's
company (`commercial_partner_id`, so an order to a contact counts) that has no
shipment yet. It is triggered rather than run inline so that the vendor stays
an agent reacting to what it has been sent. When vendors read email, only the
trigger moves.

### 6.2 A shipment is a snapshot

`vendor._ship(order)` copies each line **as it reads at that moment**, in the
product's own unit (`purchase.order.line.product_uom_qty`,
`addons/purchase/models/purchase_order_line.py:24`): two spools become 100 m of
wire. The order is never read again. Editing it afterwards does not change
what is on the truck. `UNIQUE(purchase_id)` makes the agent idempotent.

Lines outside the vendor's catalogue are not shipped. The player finds out the
way anyone would, when the delivery is short.

A shipment goes `in_transit` → `arrived` (at `date_shipped + lead_time`,
settled by §7) → `accepted`. **Accept delivery** (`shipment._accept()`) is
refused before arrival and after acceptance, and is when the contents enter
the world as `received`.

## 7. Time: settling what is due

`game.world._settle(now=None)` finishes every run whose `date_end` has passed
and marks every shipment whose `date_arrival` has passed as arrived. It runs:

- **from the settle cron**, triggered at each run's end and each shipment's
  arrival (`ir.cron._trigger(at)`). A future trigger does not wake the loop
  (`ir_cron.py` `_trigger_list` only notifies for triggers already due), so it
  lands on the loop's next poll, up to one cron tick late
  (`cron-tick × rate` game seconds, DESIGN.md §4.3). Both crons also have a
  one-game-hour interval as a safety net;
- **at the start of every player action**, so an action sees the world as of
  the moment it was taken, not as of the last cron.

Due rows are taken `FOR UPDATE SKIP LOCKED`, after flushing, because the
select is raw SQL and must see a `date_end` written earlier in the same
transaction (a test pins it). Settling twice makes the goods once.

## 8. The page: endpoints and the bus

`controllers/world.py`, all `type='json2'`, `auth='user'`:

| route | what |
|---|---|
| `GET /game/api/world` | `game.world._snapshot()`: stock (including zero lines for everything the world makes, uses or buys), workstations with their recipe, current run and open MOs, and shipments not yet accepted |
| `POST /game/api/workstations/<id>/start` | `{qty, production_id?}` → start a run, answer with the snapshot |
| `POST /game/api/shipments/<id>/accept` | accept a delivery, answer with the snapshot |

Guards, on every route:

- **Internal users only.** `auth='user'` alone admits portal users → 403.
- **Actions require a running world** (`game_clock.is_running`, DESIGN.md
  §3.3): a paused or dead world would start a run no cron will ever finish.
  Refusals are `UserError`, which `json2` answers as 422 with the sentence in
  `message` (`odoo/http.py:2676`), and the page shows that sentence.
- The `game.*` models grant no write access to anyone (§9), so the routes act
  through `sudo()`. The routes are the permission.

`GET /game/api/world` is a pure read and does not settle. A run past its end
that the cron has not reached yet is shown as *Finishing…*.

**The bus notice carries nothing.** Every change to the world sends
`odoo_sim.world_changed` with an empty payload on the pulse's channel. The page
then fetches the state behind a login. The state cannot ride the bus itself:
**any websocket, logged in or not, may subscribe to any string channel**
(`addons/bus/models/ir_websocket.py:15-28`, `:56-57`), so product names and
order references on `odoo_sim.world` would be public. The page also re-fetches
on every socket (re)connect, because notices sent while it was down are gone.

`/game` hands the first snapshot over in the bootstrap blob, with
`changed_type`, as it already did for the clock.

### 8.1 The client

`odoo_sim/ui/src/`: `sync.js` keeps the world current. Every request takes a
ticket when sent, and an answer is dropped if something sent later has already
been applied, so a fetch sent before a press cannot overwrite the press's
answer. A burst of notices collapses into one follow-up fetch.
`worldView.js` is `describeWorld` (pure, tested) plus a DOM that updates
elements in place rather than rebuilding them, because it re-renders on every
pulse, and a rebuilt input loses what the player was typing into it. Run
progress is interpolated from the clock reading.

## 9. Protection, and a window for debugging

`security/ir.model.access.csv` grants **read-only** access to
`base.group_system`, and nothing to anyone else. *Settings → Technical → Game
World* (debug mode) lists the balance, the ledger, runs, shipments, recipes,
workstations and vendors, read-only.

**An administrator can still cheat.** A server action with Python code can
`sudo()` its way into anything. That is treated like editing a save file,
outside the threat model. The stronger answer, if it ever matters, is a player
who is not an Odoo administrator.

## 10. The paperclip scenario

`addons/odoo_sim_paperclips`, `depends: odoo_sim`. Both halves, and they agree
at install:

| Odoo (the player's, noupdate) | the world |
|---|---|
| **Wire**, in m, Buy route; bought from *Tensile Wire Supply* in **Spool (50 m)** at 12.50, 1 day lead time | vendor ships wire only, arriving 24 game hours after it gets the order |
| **Paperclip**, in Units, Manufacture route | recipe: **0.1 m wire, 2 game minutes** per paperclip |
| BoM: 1 paperclip = 0.1 m wire; one operation, *Manufacture paperclip*, 2 min, on work center *Paperclip bench* | workstation *Paperclip bench*, linked to that work center |

It enables work orders and units of measure for employees. It starts with no
stock. Everything the player may change is `noupdate`, so upgrading the module
never undoes their edits to the BoM or the prices.

At `--rate 720` a paperclip takes a tenth of a real second and a delivery two
real minutes.

## 11. Known issues and risks

- **Settling lags by one cron tick** when nobody acts (§7). Harmless, since any
  action settles first; the page shows *Finishing…* in the gap.
- **Concurrent settles can fail a cron run.** Odoo runs `REPEATABLE READ`, so
  locking a row that another transaction settled and committed after this
  transaction's snapshot raises a serialization error. HTTP retries; a cron
  logs a failure and retries next tick. Repeated failures deactivate a cron
  after seven *game* days (DESIGN.md §5.3), minutes at a fast rate. Rare, since
  it needs two settles racing on the same row.
- **A purchase order cancelled after confirmation still ships.** The vendor has
  its copy. Negotiating a cancellation is gameplay for the email era.
- **Genesis on an upgraded world copies whatever Odoo held**, demo data
  included.
- **`Product Unit` precision is 2 decimals.** A recipe needing less than
  0.01 of a unit would round to nothing, and make things for free.

## 12. Out of scope, and next

- Checkpoints comparing reality against Odoo.
- Vendors that read their email. A purchase order the player sends by email
  already reaches the vendor's mailbox (`MAIL.md` §8); only the vendor agent's
  trigger has to move (§6.1).
- Goods leaving the world: sale deliveries (a new `kind`), scrap.
- Locations: `(product, place)` as the balance key, for the dock, the shelves
  and the station, which is what the graphics will want.
- Lots and serial numbers.
- Money: the real bank balance against `account.move`, which is probably the
  same ledger pattern.
- Players creating recipes; employees pressing buttons.

## 13. Testing

```bash
odoo-bin -d <db> -i odoo_sim_paperclips --test-tags /odoo_sim,/odoo_sim_paperclips --stop-after-init
cd odoo_sim/ui && npm test
```

`addons/odoo_sim/tests/test_world.py` builds its own products, recipe, station
and vendor: the ledger (exact decimals, refusals that leave nothing behind, no
ORM write access even for admin), genesis, runs (timing, one at a time,
conservation, recipe over BoM, the MO link), the vendor (unit conversion,
snapshot, catalogue, idempotence, contacts, drafts), arrival and acceptance,
the snapshot, and the endpoints over HTTP (guards, 422s, 403 for portal, the
bootstrap blob). Two tests state the premise outright: **marking an MO done
and validating a receipt make nothing real.**

`odoo_sim_paperclips/tests/` plays the scenario honestly once and checks that
Odoo moved exactly as the world did. It compares movements rather than totals,
and first settles whatever the world already had in flight, because it has to
pass on a world someone has been playing in (DESIGN.md §8).

Both suites pass on an ordinary database and on a game world.

The UI tests (`node --test`) cover `describeWorld`, `sync.js` ordering and
coalescing, and the watcher passing on world notices and resyncing on
reconnect.

Checked by hand at `--rate 720`, through `game_run` and headless Chrome:

1. With no wire, a press is refused.
2. Confirming a purchase order ships 50 m; accepting it early is refused, and it
   arrives a game day later.
3. Accepting it puts 50 m in the world.
4. A 20-paperclip run takes 2 m and delivers 20 paperclips on time, while Odoo
   still says 0.
5. An open page picks up a run started by another client, through the notice
   alone.

## 14. Decisions

| Question | Decision |
|---|---|
| Whose recipe does a workstation follow? | **The world's** (`game.recipe`). The BoM is the player's model of it (§4). |
| Does Odoo stop the player lying? | **No.** The world does not care; matching it is the player's goal (§2). |
| Genesis? | **Yes**, from internal quants, once (§3.3). |
| Is production timed? | **Yes.** Components out at start, goods in at the end (§5). |
| Is delivery timed? | **Yes.** Vendor lead time, then accepted by the player (§6). |
| Link runs to MOs? | **Yes, informational only** (§5). |
| How does the vendor learn of an order? | **A cron triggered on approval**, reading confirmed orders, which is ready to be re-pointed at email (§6.1). |
| Can anyone edit `game.*`? | **No.** Read-only for admins, for debugging (§9). |
| State on the bus? | **No.** A content-free notice, then an authenticated fetch (§8). |
