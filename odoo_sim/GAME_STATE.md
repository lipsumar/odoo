# Odoo Sim — The Game World's Own State

Status: built for manufacturing, purchasing and selling, with money in a game bank,
and the paperclip scenario (§12)
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

And one way goods leave it:

- **Sold** goods leave the world when the player ships them *in the world*,
  once the customer has paid. The customer asks for goods; the player sends it
  an invoice; it pays, if it agrees with the invoice; the player ships.
  Validating the delivery in Odoo comes after, if at all.

Money is the same problem again, and matters more, because it is the score.
Odoo records what the company says it was paid; the **game bank** holds what
it was paid. Only game transactions move it, and Odoo hears about them the
way it would from a real bank, through a bank feed.

Later, autonomous employees will press the same buttons. Email is a system of
its own (`MAIL.md`): customers write to the company and read its invoices by
email (§7), and the vendor does not read it yet.

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
  shipment's purchase order, a customer's copy of an invoice) are
  **informational**. Nothing reads them back.
- **Money moves only in the game bank.** No Odoo record -- a registered
  payment, an invoice marked paid, a statement line typed in -- moves a cent.

Checkpoints where the game compares reality against Odoo are out of scope.

## 3. Reality: a ledger and a balance

### 3.1 Two models

In `addons/odoo_sim/models/game_stock.py`, as ordinary Odoo models so that
every world write shares a transaction with the Odoo writes around it
(UI_DESIGN.md §5.5):

| model | what it is |
|---|---|
| `game.stock.entry` | append-only ledger: `date`, `product_id`, signed `qty`, `kind` (`genesis` / `manufactured` / `consumed` / `received` / `delivered`), and the world event responsible (`production_id`, `shipment_id` or `customer_order_id`) |
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

`game.stock._apply(product, qty, kind, *, date, production, shipment, customer_order)` is the
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
  `date_end = date_start + duration × qty`, settled by §9.
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
settled by §9) → `accepted`. **Accept delivery** (`shipment._accept()`) is
refused before arrival and after acceptance, and is when the contents enter
the world as `received`.

## 7. Selling: customers

In `models/game_customer.py`.

### 7.1 The customer agent

`game.customer` is an automated buyer: a `partner_id`, the product it buys,
how much per order (`qty`, in the product's unit), the most it pays per unit
**taxes included** (`max_price`), how long after agreeing to an invoice it
pays (`payment_delay`, game hours), and how long after receiving its goods it
orders again (`interval`).

A customer has **one order open at a time**. The customer-order cron
(triggered when a customer is created and at its `next_order_date`, hourly as
a safety net) has every customer with nothing open, whose time has come, place
a `game.customer.order` -- a copy of its terms at that moment -- and write to
the company about it.

**Everything a customer says goes through `_write_to_company(subject, body)`**,
the seam for the game's email. Until that lands, the message is posted on the
customer's contact in Odoo, as an email from them, and the order shows on
`/game`. Once it lands, that method sends an in-game email, and nothing else
changes.

### 7.2 Reading an invoice

Posting a customer invoice (`account.move._post`) **only triggers the invoice
cron**, as approving a purchase order triggers the vendor. Each customer reads
the posted customer invoices addressed to its company (`commercial_partner_id`,
so an invoice to a contact counts) that it has not read yet. `game.customer.
_receive_invoice(move)` is the entry point: idempotent, a no-op for anything
but a posted customer invoice to a world customer, and the hook an inbound
email will call. Today "sent" means "posted"; once customers read email it
will mean the email, and only the trigger moves.

Reading is a **snapshot**, `game.customer.invoice`: number, payment
reference, total, currency, and the quantity of the product the customer buys,
converted to the product's unit. `UNIQUE(invoice_id)`. The invoice is never
read again: resetting it to draft and raising the price afterwards changes
nothing.

Then the customer decides. It refuses, and writes back to say why, when the
invoice:

| the invoice | the reply |
|---|---|
| arrives with nothing on order | "We have not ordered anything from you." |
| is a second one for an order already invoiced | "… and are not paying twice." |
| is in another currency | "We pay in USD." |
| does not contain what it buys | "This invoice is not for the Paperclip we asked for." |
| is for more than it asked | "We asked for 200 Paperclip, not 300." |
| totals more than `qty × max_price` | "… is more than we pay: $ 0.08 each at most, taxes included." |

It compares the **total**, so anything else on the invoice -- a shipping line,
a service -- simply raises the price per unit. An invoice for less than it
asked is accepted: the customer pays for what was invoiced, expects that much,
and drops the rest.

Otherwise the order is `invoiced`, and the payment falls due `payment_delay`
later, when the settle (§9) pays it.

### 7.3 Payment

At `date_due`, `game.customer.invoice._pay()` pays the invoice's total from
the customer's account to the company's (§8), with the invoice's payment
reference as the communication, dated `date_due`. The order is `paid`, for the
quantity and amount invoiced.

A customer that cannot cover it refuses the invoice ("We cannot pay … at the
moment.") and its order goes back to waiting for an invoice. The payment is
tried under a savepoint, so one empty account does not stop the world
settling.

### 7.4 Delivery

**Ship** (`order._deliver()`) is the player's action, and is when the goods
leave the world, as `delivered` entries. It is refused before the customer has
paid -- the customer's terms: there is nothing to ship until the money is in --
after the order has shipped, and when the world does not hold the goods. It
settles first, so a payment due by the clock counts. The customer orders again
`interval` game hours later.

Validating the delivery in Odoo is recording it, and ships nothing.

## 8. Money: the game bank

In `models/game_bank.py`.

### 8.1 Two models

| model | what it is |
|---|---|
| `game.bank.account` | one per actor (`UNIQUE(partner_id)`): a `number` (`GB000001`), a currency, a `balance` with `CHECK (balance >= 0)`, and the Odoo journal it feeds (`journal_id`) |
| `game.bank.transaction` | append-only: `date`, `kind` (`deposit` / `payment`), payer, payee, `amount > 0`, and `reference`, the communication |

The pattern of §3: the balance is a cache of the transactions, kept for the
`CHECK`, and `Monetary` is NUMERIC, so a thousand payments of 0.10 add up to
exactly 100. A test spends the last cent.

A deposit has no payer (a constraint says so): it is money entering the world,
which only a scenario does, for opening balances. Every other movement is
between two accounts, so the sum of all balances is the sum of all deposits.

**The company's balance is the score.** Only game transactions move it. Odoo
can register a payment, mark an invoice paid, or be told by hand that a
million came in; the bank has seen none of it, and tests pin both.

### 8.2 One write path

`game.bank.account._book(payer, payee, amount, reference, kind, *, date)` is
the **only** way money moves; `_pay` and `_deposit` are the two ways into it.
It locks both rows in id order, so two payments crossing between the same
accounts wait for each other rather than deadlock; takes the money with a
single `UPDATE … WHERE balance >= amount`, where no row changed is a
`UserError` ("Binder & Co. (GB000002) cannot pay $ 11.50: there is only
$ 3.00 in it."); credits the payee; and records the transaction, in the
caller's transaction. There is no exchange: payer and payee hold the same
currency.

### 8.3 The bank feed

Odoo hears of the bank's transactions as it would from a real bank, through a
feed (`models/account_bank_feed.py`) in the shape of Odoo's own online
synchronisation.

- **The connection is the game's.** `game.bank.account.journal_id` names the
  Odoo journal an account feeds, and only the game writes it. The company's
  account is connected to the company's first bank journal in its currency:
  when the account opens, or when such a journal is created later, since the
  chart of accounts can arrive after the world does. The journal then shows
  *Bank Feeds: Game bank*.
- **An import** (`account.bank.statement.line._game_bank_import(account)`)
  creates a statement line for each transaction on the account that the
  journal has not seen: the signed amount (money out is negative), the
  communication as label, the counterparty's name, account number and
  partner, and the bank's identifier for the transaction (`game_bank_ref`,
  `GBT00000042`). A unique index on `(journal_id, game_bank_ref)` makes it
  idempotent, and makes a second import racing the first fail instead of
  duplicating.
- **When.** Every transaction on a connected account triggers the feed cron,
  so a payment reaches Odoo within a cron tick. Hourly as a safety net.
- It **only writes to Odoo**, and reads nothing there but which lines it has
  already imported. A line the player deletes comes back at the next import:
  the bank still has the transaction.

Community Odoo cannot reconcile a statement line with an invoice
(`account_payment.py` says as much where it forces a journal entry on every
payment). The player records a customer's payment with *Register Payment* on
the invoice; the statement lines are the bank's side, to check the journal
against.

### 8.4 Opening

On install (`post_init_hook`) and on upgrade to 1.2 (`migrations/1.2/`), the
company's account is opened **empty**. Unlike stock (§3.3), Odoo's bank
balance is not taken at its word: money is the score, and the score starts
from what the game gives. Scenarios give their actors their starting money with
`<function model="game.bank.account" name="_open">`, which deposits only into
an account that has never moved. So it can sit outside `noupdate`, and a world
that upgrades into a scenario gets the money too. The company's own starting
capital, if a scenario wants one, would be the same kind of deposit.

## 9. Time: settling what is due

`game.world._settle(now=None)` finishes every run whose `date_end` has passed,
marks every shipment whose `date_arrival` has passed as arrived, and pays every
accepted customer invoice whose `date_due` has passed. It runs:

- **from the settle cron**, triggered at each run's end, each shipment's
  arrival and each payment's due time (`ir.cron._trigger(at)`). A future trigger does not wake the loop
  (`ir_cron.py` `_trigger_list` only notifies for triggers already due), so it
  lands on the loop's next poll, up to one cron tick late
  (`cron-tick × rate` game seconds, DESIGN.md §4.3). Both crons also have a
  one-game-hour interval as a safety net;
- **at the start of every player action**, so an action sees the world as of
  the moment it was taken, not as of the last cron.

Due rows are taken `FOR UPDATE SKIP LOCKED`, after flushing, because the
select is raw SQL and must see a `date_end` written earlier in the same
transaction (a test pins it). Settling twice makes the goods once.

## 10. The page: endpoints and the bus

`controllers/world.py`, all `type='json2'`, `auth='user'`:

| route | what |
|---|---|
| `GET /game/api/world` | `game.world._snapshot()`: stock (including zero lines for everything the world makes, uses, buys or sells), workstations with their recipe, current run and open MOs, shipments not yet accepted, the company's bank account (balance and its last ten transactions), and customer orders not yet delivered, each with the last invoice its customer read |
| `POST /game/api/workstations/<id>/start` | `{qty, production_id?}` → start a run, answer with the snapshot |
| `POST /game/api/shipments/<id>/accept` | accept a delivery, answer with the snapshot |
| `POST /game/api/customer_orders/<id>/deliver` | ship a paid order, answer with the snapshot |

Guards, on every route:

- **Internal users only.** `auth='user'` alone admits portal users → 403.
- **Actions require a running world** (`game_clock.is_running`, DESIGN.md
  §3.3): a paused or dead world would start a run no cron will ever finish.
  Refusals are `UserError`, which `json2` answers as 422 with the sentence in
  `message` (`odoo/http.py:2676`), and the page shows that sentence.
- The `game.*` models grant no write access to anyone (§11), so the routes act
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

### 10.1 The client

`odoo_sim/ui/src/`: `sync.js` keeps the world current. Every request takes a
ticket when sent, and an answer is dropped if something sent later has already
been applied, so a fetch sent before a press cannot overwrite the press's
answer. A burst of notices collapses into one follow-up fetch.
`worldView.js` is `describeWorld` (pure, tested) plus a DOM that updates
elements in place rather than rebuilding them, because it re-renders on every
pulse, and a rebuilt input loses what the player was typing into it. Run
progress is interpolated from the clock reading. The *Bank* panel shows the
balance and the last transactions, signed from the company's side; *Customer
orders* shows each order's terms and where it stands, and *Ship* once it is
paid -- or once its payment is due by the clock, as *Accept delivery* appears
at arrival time, since shipping settles first.

## 11. Protection, and a window for debugging

`security/ir.model.access.csv` grants **read-only** access to
`base.group_system`, and nothing to anyone else, the bank included. *Settings →
Technical → Game World* (debug mode) lists the balance, the ledger, runs,
shipments, recipes, workstations, vendors, bank accounts and transactions,
customers, their orders and the invoices they read, read-only.

**An administrator can still cheat.** A server action with Python code can
`sudo()` its way into anything. That is treated like editing a save file,
outside the threat model. The stronger answer, if it ever matters, is a player
who is not an Odoo administrator.

## 12. The paperclip scenario

`addons/odoo_sim_paperclips`, `depends: odoo_sim`. Both halves, and they agree
at install:

| Odoo (the player's, noupdate) | the world |
|---|---|
| **Wire**, in m, Buy route; bought from *Tensile Wire Supply* in **Spool (50 m)** at 12.50, 1 day lead time | vendor ships wire only, arriving 24 game hours after it gets the order |
| **Paperclip**, in Units, Manufacture route | recipe: **0.1 m wire, 2 game minutes** per paperclip |
| BoM: 1 paperclip = 0.1 m wire; one operation, *Manufacture paperclip*, 2 min, on work center *Paperclip bench* | workstation *Paperclip bench*, linked to that work center |
| **Binder & Co.**, a contact; Paperclip sells at 0.05 plus the default sales tax | customer: 200 paperclips an order, at most **0.08 each, taxes included**; pays 4 game hours after agreeing to an invoice; orders again a game day after delivery; **1000 in the bank** |

It enables work orders and units of measure for employees. It starts with no
stock, and the company with no money: the customer's payments are the first. Everything the player may change is `noupdate`, so upgrading the module
never undoes their edits to the BoM or the prices.

At `--rate 720` a paperclip takes a tenth of a real second, a delivery two
real minutes, and a customer pays twenty real seconds after reading its
invoice.

## 13. Known issues and risks

- **Settling lags by one cron tick** when nobody acts (§9). Harmless, since any
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
- **Money only comes in.** The company pays nobody yet: wire is shipped and
  never paid for. Vendor bills and outgoing payments are next (§14).
- **A customer "receives" an invoice when it is posted**, not when it is sent,
  until email lands (§7.2). Until then its order request is a message on its
  contact in Odoo, easy to miss; `/game` shows the order too.
- **Community cannot reconcile statement lines** (§8.3). The imported lines
  and the registered payments are two records of one fact, which the player
  cannot match in Odoo.
- **A deleted statement line comes back** at the next import (§8.3). That is
  the bank being right, but it can surprise.
- **A customer never gives up.** An order nobody invoices stays open forever,
  and its customer never orders again.
- **Payments lag by one cron tick**, like settling (§9). Harmless: shipping
  settles first.

## 14. Out of scope, and next

- Checkpoints comparing reality against Odoo.
- Vendors that read their email. A purchase order the player sends by email
  already reaches the vendor's mailbox (`MAIL.md` §8); only the vendor agent's
  trigger has to move (§6.1).
- Goods leaving the world other than by sale: scrap.
- Locations: `(product, place)` as the balance key, for the dock, the shelves
  and the station, which is what the graphics will want.
- Lots and serial numbers.
- Paying out: vendor bills, and payments the player initiates in Odoo reaching
  the bank (a payment file the bank reads, as a real one would).
- Wiring customers to the game's email (§7.1, §7.2).
- Customers who negotiate, cancel, pay late, order different things, or come
  and go.
- Players creating recipes; employees pressing buttons.

## 15. Testing

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

`test_bank.py`: deposits and payments, refusals, the last cent, no exchange,
account numbers, the company's account and its journal, no ORM write access;
and the feed (what a line says, signs, once only, a deleted line coming back,
unconnected accounts, the trigger). `test_customer.py`: asking and asking
again, one order at a time, reading once, the snapshot of an invoice, every
refusal, partial invoices, contacts, an empty account, drafts and credit
notes, shipping (what was paid for, not before, not twice, not what is not
there), the page's view, and shipping over HTTP. Three more premise tests:
**registering a payment or typing a statement line in Odoo moves no money, and
validating a delivery ships nothing.**

`odoo_sim_paperclips/tests/` plays the scenario honestly -- buying and making,
then selling to Binder & Co. and getting paid -- and checks that Odoo moved
exactly as the world did, goods and money alike. It compares movements rather than totals,
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

## 16. Decisions

| Question | Decision |
|---|---|
| Whose recipe does a workstation follow? | **The world's** (`game.recipe`). The BoM is the player's model of it (§4). |
| Does Odoo stop the player lying? | **No.** The world does not care; matching it is the player's goal (§2). |
| Genesis? | **Yes**, from internal quants, once (§3.3). |
| Is production timed? | **Yes.** Components out at start, goods in at the end (§5). |
| Is delivery timed? | **Yes.** Vendor lead time, then accepted by the player (§6). |
| Link runs to MOs? | **Yes, informational only** (§5). |
| How does the vendor learn of an order? | **A cron triggered on approval**, reading confirmed orders, which is ready to be re-pointed at email (§6.1). |
| Can anyone edit `game.*`? | **No.** Read-only for admins, for debugging (§11). |
| State on the bus? | **No.** A content-free notice, then an authenticated fetch (§10). |
| What keeps score? | **The game bank** (§8). Only game transactions move it, never Odoo data. |
| How does Odoo learn of a payment? | **A bank feed** importing statement lines, deduplicated by the bank's transaction identifier (§8.3). |
| Does a customer pay whatever it is invoiced? | **No.** It pays on its terms: what it asked for, at its price or less, taxes included (§7.2). |
| When do the goods leave? | **When the player ships, after payment** (§7.4). |
| How does a customer learn of an invoice? | **A cron triggered on posting**, reading posted invoices, ready to be re-pointed at email (§7.2). |
| Does the company start with money? | **No**, unlike stock: the score starts from what the game gives (§8.4). |
