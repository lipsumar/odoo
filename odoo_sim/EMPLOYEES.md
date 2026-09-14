# Odoo Sim — Employees

Status: v1 built. Hiring, the working day, three kinds of work, and unpaid
employees leaving. There is no way to pay anyone yet (§7).
Branch: `odoo-sim`
Target: Odoo 19.0
Companion to `DESIGN.md` (the clock), `GAME_STATE.md` (what exists),
`MAIL.md` (email) and `UI_DESIGN.md` (the page).

## 1. Problem

Until now the player pressed every button in the world: *Manufacture* at the
bench, *Accept delivery* at the door, *Put in* and *Send* at the post. A
company grows by having other people press them.

An employee is an **agent**, like a customer (`GAME_STATE.md` §7). It acts in
both halves of the game:

- **In the world**, it does the work under the world's own rules. A run at a
  workstation consumes the recipe, not the BoM. Nothing is packed that is not
  on the shelves, and a delivery exists once it is unpacked.
- **In Odoo**, it records what it did, as itself: it marks the manufacturing
  order done, and validates the receipt or the delivery order.

v1 employees do three things: execute a manufacturing order, receive a
delivery (unpacking it), and ship a delivery order (packing it). Each takes
on any task it can do whenever it is free.

## 2. The rule

**An employee learns what to do from Odoo, and does it in the world.** Odoo is
where the company says what is to be done, so that is where an employee looks,
as a real one would. The world still decides what happens:

- an order Odoo says is ready, for components the world does not hold, is not
  made (§4.2);
- a delivery order to an address Odoo has wrong goes to the wrong address, and
  the box comes back (§4.4);
- whatever the employee records in Odoo is what it did in the world, not what
  Odoo asked for.

So an employee does not make Odoo honest. It is honest about its own work, and
it trusts Odoo for everything else, the same way the player's staff would.

## 3. Hiring

In `addons/odoo_sim/models/game_employee.py`.

| model | what it is |
|---|---|
| `game.job` | a position the company can hire for: `name`, monthly `wage`, currency. Scenario data |
| `game.employee` | someone hired: their name, Odoo user, job, the `wage` agreed, their time zone, when they started, whom they write to, `state` (`employed` / `gone`), and when they will ask for pay and leave (§7) |
| `game.employee.task` | something an employee took on (§4) |

**Hire** on `/game` (`job._hire(tz, hired_by)`) hires someone at once:

- **A name, and an Odoo user.** The user is internal (`share = False`), with
  *Inventory / User* and *Manufacturing / User*, and an address at the company:
  on its alias domain, else its email's domain, else `example.com`. Whatever
  the employee records, Odoo says they did it (`write_uid`, the chatter). Names
  come from a list, in turn.
- **A contract, in the world.** The wage is copied from the job as it is today,
  and does not change if the job's does. It starts now, is due at the end of
  each month, and nothing about it is in Odoo.
- **A time zone**: the one the page shows time in, as when forwarding
  (`DESIGN.md` §3.4). Their working day, and their months, are in it.
- **Whom they write to**: the player who pressed *Hire*.

Their mail goes where an employee's mail goes (`MAIL.md` §5): to their own
game inbox, which nobody reads yet.

## 4. Work

### 4.1 The working day

Nine to five, every day, with no lunch break (`workday.py`). Weekends are not
modelled, as for forwarding.

**Work stops at five and carries on at nine.** A task's end is computed in
working time when it is taken on (`workday.add_working_time`): four hours
begun at three in the afternoon end at eleven the next morning. A run at a
workstation waits for its employee, so the run's `date_end` is the task's.
Nothing is split or interrupted. The world simply settles the task at that
instant, like any other due event.

An employee only takes on work while at work. Outside hours the cron triggers
itself for the employee's next nine o'clock.

### 4.2 Looking for work

`game.employee._cron_work()`, the cron `odoo_sim.ir_cron_employees_work`, has
every employee at work and free look for something to do
(`_look_for_work`). An employee does **one task at a time**, locked
`FOR UPDATE` like a workstation.

What there is to do is **what Odoo says is ready** (`_work_to_do`):

| work | Odoo says | the world also needs |
|---|---|---|
| **manufacture** | an MO `confirmed` or `in progress` whose readiness is *Ready* (`reservation_state = 'assigned'`), for a product a workstation makes | a free workstation for it, and the recipe's components on the shelves |
| **receive** | a receipt that is *Ready* (`assigned`), for a purchase order | a delivery at the door (`arrived`) whose packing slip names that purchase order (`game.shipment.purchase_id`), that nobody is unpacking |
| **ship** | a delivery order that is *Ready* (`assigned`) | an address for its contact in Odoo, and everything it asks for on the shelves |

Oldest first: an MO by its scheduled start, a transfer by its scheduled date.
The employee takes the first one it can do.

**What the world refuses is seen before trying.** Components and goods are
checked against the shelves (`game.stock._holds`) before a run starts or a box
is packed. Checking first is not just politeness. The attempt runs under a
savepoint, and rolling one back throws away the transaction's queued bus
notices and mail tracking (`MAIL.md` §6). The savepoint is there for what
could not be foreseen, which is logged and skipped.

**Taken on once, ever.** A unique index on the task's MO, and one on its
transfer, means nothing is taken on twice. That holds even when recording it
in Odoo failed and Odoo still shows it as ready (§4.5): the work was done, and
doing it again would make the goods twice.

**Nobody starts what they cannot finish before leaving** (§7): a task that
would end after `date_leave` is left for someone else.

**When it runs.** Work becomes possible when the world changes (goods on the
shelves, a station freed, a delivery arriving, a task finished) and when Odoo
readies something. So the cron is triggered:

- by `game.world._changed()`, which every change to the world already calls;
- by `stock.move._action_confirm` and `_action_assign`, which is how a
  transfer or an MO becomes *Ready* (`models/stock_move.py`);
- when someone is hired;
- at the next nine o'clock, for employees off work;
- hourly, as a safety net for anything else, such as the player correcting an
  address.

Its own changes trigger it again, and that converges. The cron deletes its
due triggers once it has run (`ir_cron._clear_schedule`), and a pass that
finds nothing new to do changes nothing.

### 4.3 Manufacture

`_start_making`: a run of the MO's quantity, in the product's unit, at a free
workstation that makes it (`workstation._start(qty, mo, date_end=...)`). The
components leave the shelves now; the goods arrive at the end
(`GAME_STATE.md` §5). The player's *Manufacture* is refused meanwhile: the
station is running.

At the end, the employee marks the MO done in Odoo. It sets the quantity
producing to what the run made, lets Odoo compute the components from the BoM
(which may be wrong, and that is the player's to notice), and calls
`button_mark_done` without wizards (`skip_consumption`, `skip_backorder`).
If the run made less than the MO asks (the MO was increased meanwhile), the
rest becomes a backorder.

### 4.4 Receive

`_start_receiving`: the employee is at the door with the delivery, for
`UNPACK_MINUTES` (5 game minutes). The player's *Accept delivery* is refused
meanwhile ("Alice Adler is unpacking P00003.").

At the end the delivery is unpacked: its contents enter the world as
`received` entries, dated then (`shipment._take_in`), and the shipment is
`accepted`. The employee validates the receipt with **what came**: each
product's quantity from the shipment, filling its moves in order, the
remainder on the last one. What was ordered and did not come is not
backordered, since the vendor ships an order once (`GAME_STATE.md` §6.2).

### 4.5 Ship

`_start_shipping`: the employee takes a box, packs everything the delivery
order asks for (the goods leave the shelves as `packed` entries), and writes
**the address Odoo has for its contact** on it: the company's name, then
Odoo's own formatting of the address for the contact's country
(`_display_address`), with empty fields' gaps closed up. That takes
`PACK_MINUTES` (5 game minutes). The box is the employee's meanwhile: the
player cannot pack, unpack or send it.

At the end the box goes to the post, as if the player had sent it
(`package._hand_to_post`), and the employee validates the delivery order with
what went in the box. The post then does what it always does
(`GAME_STATE.md` §7.4). If Odoo's address is not where the customer lives,
the box comes back to the bench, and the player deals with it.

A contact with no address in Odoo gets nothing: there is nothing to write on
the box.

### 4.6 Recording in Odoo

Each task is recorded when it is finished (`task._record`), as the employee's
user with `sudo()`: Odoo says who, and access rules do not decide what a
world's employee may do. It runs under a savepoint. A refusal, such as an MO
cancelled meanwhile, a tracked product needing lots, or a transfer validated
by the player first, is logged, kept on the task as `note`, and **changes
nothing in the world**. The goods were made, received or posted either way.

## 5. Settling

`game.world._settle` (`GAME_STATE.md` §9) gains three steps:

- after runs and arrivals, **tasks due** finish (`task._finish`). Their run has
  just ended, and their delivery is at the door;
- after complaints, **employees due to ask for pay** write (`_ask_for_pay`);
- then **employees due to leave** leave (`_leave_unpaid`).

## 6. The page

`POST /game/api/jobs/<id>/hire`, `{tz}` → the snapshot. It is guarded like
every action: internal users only, and a running world.

The snapshot (`game.world._snapshot`) gains:

- `jobs`: `{id, name, wage, currency}`;
- `employees`, employed only: name, job, wage, the task in hand
  (`{text, date_start, date_end}`), `shift_start` and `shift_end` (the working
  day as of the snapshot, so the page can tell *not in yet* from *gone home*
  as its clock moves on), `salary_due` (when the first unpaid salary fell or
  falls due) and `date_leave`;
- `worker`, the name of the employee at it, on a workstation's run, a delivery
  at the door and a package on the bench.

The *Employees* panel has a *Hire* button per job, and a card per employee:
what they are doing, and *Done at …*, *Waiting for work*, *Off until 09:00*,
*Carries on at 09:00* or *Gone home*. Once a salary is late, it adds *Not paid
since …: leaves …*. A run, a delivery or a box someone is working on says who,
and its buttons wait.

A run's progress bar is interpolated straight from start to end, so it creeps
through a night that the employee spends at home.

## 7. Pay

Salaries are due at the end of each month, in the employee's time zone. The
first month is paid for the days worked, the first day included: hired on
5 January on 310 a month, they are owed 270 (27 days of 31) at the end of
January (`_salaries`).

**Paid** means paid into the employee's account at the game bank by the
company's (`_paid`). Payments go to the oldest salary first (`_unpaid`). Money
only moves in the game bank (`GAME_STATE.md` §8), and **v1 has no way for the
player to pay anyone**, so in practice nobody is paid. That is what the rest
of this section is for: once there is a way to pay, nothing here changes.

When a salary is late:

| when (09:00, the employee's time) | what happens |
|---|---|
| the day after it fell due (1 February) | they write to whoever hired them: "My salary for January 2030, €270.00, was due on 31 January, and I have not been paid. If I have not been paid by 15 February, I will not be coming in any more." |
| every `REMIND_EVERY` (7 days) after that, before they leave | they write again, threaded under the first |
| `LEAVES_AFTER` (15 days) after it fell due (15 February) | they leave: "I have still not been paid for January 2030, so I am leaving today." `state = 'gone'`. Their Odoo user is left as it is: archiving it is up to the player |

`date_chase` and `date_leave` are settled like complaints (§5). Each checks
again when it comes due. If the salary was paid since, the employee
reschedules for the next one that is unpaid, and says nothing.

A task never runs past `date_leave` (§4.2). An employee always finishes what
they started before they leave, so leaving never interrupts a run or leaves
a box half-packed.

## 8. Protection and debugging

As for every `game.*` model (`GAME_STATE.md` §11): read-only for
administrators, writable by nobody. *Settings → Technical → Game World*
(debug mode) lists *Jobs*, *Employees* (with when they will ask for pay and
leave) and *Employee tasks* (with anything Odoo refused, in *Not recorded
because*).

## 9. The paperclip scenario

`odoo_sim_paperclips` adds one job, **Worker**, at **300 a month**: about what
Binder & Co.'s orders bring in if one comes every working day. Hired now,
never paid, a worker leaves on the 15th of next month.

## 10. Known issues and risks

- **Nobody can be paid.** Every employee leaves 15 days after their first
  month ends. Paying out (`GAME_STATE.md` §14) is next.
- **Nobody can be fired**, and nobody can be told what to do. Employees take
  the oldest ready work, whatever it is.
- **Employees do not read their mail.** A reply to their request for pay goes
  to their game inbox, where nobody reads it.
- **Recording is tried once.** An MO or transfer Odoo refused stays *Ready* in
  Odoo, and is never taken on again (§4.2). The player records it by hand.
- **Receiving matches by purchase order.** A receipt for a purchase order
  whose shipment has already been accepted, such as a backorder the player
  made, is never received by an employee.
- **The address is Odoo's formatting of it.** A customer whose postal address
  in the world is written another way, with the country or without it, will
  not get boxes an employee addresses. The paperclip scenario's customer is
  written the way Odoo writes a US address.
- **The progress bar creeps through the night** (§6).
- **Concurrent employees and settles** can fail a cron run with a
  serialization error, as concurrent settles can (`GAME_STATE.md` §13). The
  next run tries again.
- **Hourly safety net.** An Odoo change that confirms or reserves no stock
  move, such as correcting a contact's address, is noticed within a game hour,
  or at the next change to the world.

## 11. Out of scope, and next

- Paying employees, and payslips in Odoo.
- Firing; skills; assigning work; priorities other than oldest first.
- Employees who read and answer their mail.
- Weekends, holidays, sick days, a lunch break.
- Other work: purchasing, invoicing, answering customers.

## 12. Testing

How to run them: README.md, "Running the tests".

`addons/odoo_sim/tests/test_employee.py`. Time is never frozen, so a test
that needs someone at work hires them in a time zone where it is ten o'clock
now (`zone_at`). Orders and transfers are dated in the year 2000, so that a
played world's older work does not come first.

- `TestWorkingTime` (in `test_workday.py`): straight through, stopping at five, beginning at night,
  ending at five, days of work, a change of clocks, who is at work.
- `TestHiring`: a user of their own, two different people, the wage agreed,
  the cron woken, no ORM write access.
- `TestWorkingDay`: the cron puts people to work, nobody works at night and
  the cron wakes at nine, one task at a time, work carried over to the next
  morning with the station waiting.
- `TestManufacturing`: made and marked done by the employee, the recipe over
  the BoM, nothing made without components in the world, an MO Odoo says is
  waiting left alone, an MO cancelled meanwhile made and noted, taken on once,
  nothing started that would end after leaving.
- `TestReceiving`: nothing before arrival, unpacked, the player's *Accept*
  refused meanwhile, recorded as it came, a short delivery with no backorder,
  a delivery the player took in left alone.
- `TestShipping`: packed, the box locked, posted to Odoo's address, recorded,
  delivered; a wrong address comes back; no address or not enough goods packs
  nothing.
- `TestPay`: hired on the 5th, due on the 31st, gone on the 15th; the requests,
  threaded, and the resignation; paid means waiting for the next month.
- `TestEmployeeSnapshot`, `TestEmployeeApi`: what the page sees, hiring over
  HTTP, 404, 422 while paused, 403 for portal users.

`odoo_sim_paperclips` hires a Worker, who makes paperclips for an MO, and
checks that Odoo moved exactly as the world did.

The UI tests cover the hire buttons, each state of an employee's card, and a
run, delivery and box with someone at them.

## 13. Decisions

| Question | Decision |
|---|---|
| Where does an employee learn what to do? | **From Odoo**: whatever Odoo says is *Ready* (§2, §4.2). |
| Whose recipe does an employee follow? | **The world's**, as a workstation always does (`GAME_STATE.md` §4). |
| Is an employee someone in Odoo? | **Yes**: an internal user, so that Odoo says who recorded what. Left active when they leave: archiving it is the player's call. |
| Work past five? | **Stops, and carries on at nine**, computed in working time when taken on (§4.1). |
| Which task first? | **The oldest one they can do.** |
| Is work taken on twice if recording failed? | **No.** The world has happened (§4.2). |
| How is an employee paid? | **Not in v1.** Payments into their game bank account count, once there are any (§7). |
| When does an unpaid employee leave? | **15 days after the salary fell due**, at nine, having asked on the first morning and weekly (§7). |
| Does leaving interrupt work? | **No**: nobody takes on what would end after they leave. |
