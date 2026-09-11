# Odoo Sim — Mail in the World

Status: built — the post, the post office, the player's inbox on `/game`, and
the seams agents use
Branch: `odoo-sim-email` (issue #4)
Target: Odoo 19.0
Companion to `DESIGN.md` (the clock), `UI_DESIGN.md` (the page) and
`GAME_STATE.md` (what exists).

## 1. Problem

A game world runs on a real Odoo database, and Odoo emails all the time:
notifications to followers, "Send by Email" on an order, acknowledgements from
aliases, digests from crons. Pointed at a world, each of those would go to a
real SMTP server and, from there, to whatever addresses the database holds —
demo addresses at best, a real company's customers at worst, if the world was
made from a copy of one.

Issue #4 asks for the opposite, and the answer to "is this cut off from the
outside world?" is **yes, completely**:

- **No real email ever leaves a world, and none comes in.** The game handles
  all of it.
- The **player has an inbox** on the game page, and can read and reply.
- **Agents** in the world can receive and send email. *How* they compose
  and answer is out of scope; the seams they will use are not.
- **Odoo's aliases work as normal.** Mail to `sales@<alias domain>` makes a
  lead if Odoo is configured to, and a reply to a notification lands on the
  record's chatter.

## 2. The rule

**A world's mail is a closed system, and Odoo is one of its correspondents.**
Everything anyone sends is *posted* into the world, and the world delivers
it. Odoo sends through the post like everybody else. It receives through its
own mail gateway (`mail.thread.message_process`), which the post hands mail
to exactly as fetchmail would. So Odoo's routing — aliases, replies by
References, catchall, bounces — is Odoo's own code running unchanged, and is
not reimplemented here.

## 3. Nothing leaves, nothing comes in

Three places, from the most to the least specific:

| where | what it does in a world |
|---|---|
| `odoo_sim/models/ir_mail_server.py` | `send_email` posts the message into the world (`game.email._post`) and returns its Message-Id, as a successful send does. `_connect__` returns a stand-in session, so no SMTP connection is ever opened. `test_smtp_connection` refuses. |
| `odoo_sim/models/fetchmail.py` | an incoming mail server fetches nothing: `_connect__` refuses, and `_fetch_mail` returns the refusal (the button raises it; the cron lets it go). |
| core, `ir_mail_server._connect__` | **the backstop**: on a game world, refuse to open an SMTP connection at all, with `MailDeliveryException`. This is what protects a world *without* the game installed — `sim_init` and `game_run` work on any database (README), and the fetchmail and notification crons would still run. Six lines, placed after upstream's test-mode check so test behaviour is unchanged. |

`send_email` is the chokepoint for everything Odoo sends: `mail.mail._send`,
templates, the composer, bounces (`_routing_create_bounce_email`) all end
there, and it is the method Odoo's own mail tests mock. The world copy is
prepared as SMTP would have been given it: recipients from
`_prepare_smtp_to_list` (To, Cc, Bcc, and the `send_validated_to` context
`mail.mail` sets), and `_alter_message__` applied (forged To headers, Bcc and
internal headers removed).

Posting happens **in the sender's transaction**. A `mail.mail` that rolls
back was never sent — which is more honest than SMTP, where the email is
gone before the rollback.

"World" means `game_clock.clock_for(db)` is not `None` — the same test as
everywhere else (`DESIGN.md` §4.1).

## 4. The post

`addons/odoo_sim/models/game_mail.py`.

| model | what it is |
|---|---|
| `game.email` | one email as it was posted, never edited: `date` (game time), `message_id`, From / Reply-To / To / Cc / Subject / In-Reply-To / References, a text `preview`, the `raw` message (what Odoo's gateway is handed), `origin` (`odoo` / `player` / `outside`), `author_id` (the player who wrote it on the page), and `res_model` / `res_id` — what Odoo said it was about |
| `game.email.delivery` | one copy at one address: `address` (normalized), `route` (`player` / `odoo` / `outside`), `user_id` for a player, `state` (`in_transit` → `delivered` or `failed`), `is_read`, and the gateway's `failure` |

Read-only for `base.group_system`, writable by nobody, like every `game.*`
model (`GAME_STATE.md` §9). *Settings → Technical → Game World → Post* and
*Deliveries* (debug mode) show everything that was sent and where each copy
went.

**`_post(message, recipients, *, origin, author=None)` is the only way an
email enters the world.** It:

1. stamps `Date` with the **game** instant. `_build_email__` writes a real
   `utcnow()` (a raw clock, `DESIGN.md` §5.4), and Odoo's gateway takes `Date`
   as the date of whatever it posts from an email, so an unstamped reply would
   land on the chatter dated in the real present;
2. drops `Bcc` from the letter (it stays on the envelope);
3. records what Odoo said the email is about: `X-Odoo-Objects`
   (`mail/models/models.py` `_notify_by_email_get_headers`), falling back to
   the `mail.message` with that Message-Id. Informational, like a run's MO
   link: the world never reads it back;
4. addresses a copy to every recipient (§5), `in_transit`;
5. triggers the post office.

## 5. Addressing

Decided per address when posted, like an envelope, in this order:

1. **An active employee's own address** (`res.users`, `share = False`,
   `email_normalized`) → `player`: their inbox. This wins even on the
   company's own domain, the way an employee's mailbox lives on a real mail
   server beside Odoo's aliases.
2. **An address Odoo receives** → `odoo`. That means its aliases, catchall,
   bounce and default-from (`mail.alias.domain._find_aliases`), **and
   anything else on an alias domain**, including `bounce+…` addresses. The
   company's mail server sends the whole domain to Odoo, and what Odoo does
   with an address it has no alias for is Odoo's business.
3. **Anyone else** → `outside`: a mailbox outside the company. Portal users
   are outside too. The outside world is unbounded, so an outside copy is
   always delivered — there is nobody to bounce it.

No alias domain configured means nothing routes to Odoo. That is also true
of real Odoo: "if configured in Odoo", as the issue says.

## 6. The post office

The cron `odoo_sim.ir_cron_post_office` (`game.email.delivery._cron_deliver`),
triggered by every post. An untimed `_trigger()` notifies at once
(`ir_cron._trigger_list`), and `game_run` listens for that, so mail arrives
within moments rather than on the next cron tick. Transit takes no game time.
The hourly interval is a safety net.

Delivering is **never done in the sender's transaction.** Whatever happens on
receipt — the gateway making a lead, an agent answering — runs in the post
office's own. That keeps a `mail.mail` being sent from re-entering the mail
gateway halfway through its loop, and it is the same choice the vendor agent
makes (`GAME_STATE.md` §6.1).

- `player` and `outside` copies are marked delivered.
- `odoo` copies of one email are **handed to the gateway once**, with a
  `Delivered-To` header per address, as a receiving mail server adds. Once,
  because the gateway routes one message to every alias among its recipients
  and would ignore a second copy as a duplicate Message-Id anyway.
  `Delivered-To`, because an alias that was only in Bcc leaves no other trace
  in the headers.
- A gateway refusal (`ValueError: No possible route…`) fails those copies and
  records why. It does not fail the rest of the post. Odoo's own bounces, where
  it decides to send one, are ordinary Odoo mail and come back through the
  post.
- Outside copies are then passed to the agents' hook (§8).
- The page is told the world changed (`GAME_STATE.md` §8).

**One email per transaction.** A gateway refusal rolls back a savepoint, and
rolling back a savepoint clears the whole transaction's `precommit` queue
(`sql_db.py` `_FlushingSavepoint`). Two emails in one transaction would let
the second one's refusal silently discard the first one's tracking and
notifications. Fetchmail commits after each message for the same reason. So
`_cron_deliver` delivers one email, commits, and repeats. It stops at 100 per
run, so that mail begetting mail cannot hold the cron, and re-triggers itself
when there is more. `_deliver()` with no limit, all in one transaction, is
for tests.

The cron runs as root, like fetchmail's: the gateway runs as whoever hands it
the mail.

## 7. The player's inbox

`controllers/mail.py`, all `type='json2'`, `auth='user'`, internal users only
(the same `_world()` guard as the world's routes):

| route | what |
|---|---|
| `GET /game/api/mail/<id>` | one email: headers, the body (sanitized), attachment names, and `reply: {to, subject}` — Reply-To or From, and `Re:` once |
| `POST /game/api/mail/<id>/read` | mark it read → the snapshot |
| `POST /game/api/mail/send` | `{to, cc, subject, body, parent_id?}` → the snapshot. `body` is plain text as typed. `parent_id` threads the reply (In-Reply-To, References), which is how Odoo's gateway finds the record a reply belongs to |

- **The mailbox rides the world snapshot.** `game.world._snapshot(user)` adds
  `mail: {address, unread, inbox, sent}` — the newest 50 of each, without
  bodies — whenever the routes pass the player. So the bootstrap blob, every
  action's answer, and every refetch after a `world_changed` notice carry it,
  and `sync.js`'s ordering applies to it unchanged. The notice still carries
  nothing (`GAME_STATE.md` §8).
- **Mail is private.** An email is shown only to whoever received it or wrote
  it, and is a 404 to anyone else — including as a `parent_id`.
- **Sending needs a running world** (`is_running`). The post office is a cron,
  so mail posted into a world nothing ticks would sit in transit. Marking read
  is not an event in the world, and is allowed while time stands still.
- **An email is someone else's HTML.** The body is sanitized on the server
  (`html_sanitize`, as `mail.message` bodies are) and shown in an
  `<iframe sandbox>` with no scripts and no same-origin access. Links open in
  a new tab.
- Addresses are parsed, not guessed: a typo is refused (`Not an email address:
  …`) rather than silently dropped. A player with no email address is told
  why nothing arrives, and cannot write.

The page (`ui/src/mailView.js`): Inbox (with the unread count) and Sent, one
email open, and a form for a new email or a reply. As elsewhere, `describeMail`
is pure and tested, and the DOM is updated in place. The form's fields are
filled once per form, because the page re-renders on every pulse and the
fields hold what the player is typing.

## 8. Agents

Behaviour is out of scope; the seams are built, and agreed with the sales/bank
work (`game.customer`):

**Sending:**

```python
env['game.email']._send_from(sender, to, subject, body, *, cc=(), attachments=(), parent=None)
```

`sender` is a `res.partner` or a formatted address; `to` / `cc` are a string
of addresses or a list of strings and partners; `body` is HTML; attachments
are `(filename, bytes, mimetype)`; `parent` is the `game.email` being answered.
It returns the posted email, and delivery follows. The player's endpoint uses
this same call, with `author`.

To reach Odoo, an agent writes to an address Odoo receives (§5) — a sales
alias, say — or to the player's own address. Not to the catchall directly:
Odoo bounces direct writes to catchall.

**Receiving:** `game.email.delivery._received()`, called once per delivery run
with the copies just delivered to outside addresses, in the post office's
transaction. It does nothing by default. An agent extends it:

```python
class GameEmailDelivery(models.Model):
    _inherit = 'game.email.delivery'

    def _received(self):
        super()._received()
        for delivery in self.filtered(lambda d: d.email_id.res_model == 'account.move'):
            move = self.env['account.move'].browse(delivery.email_id.res_id).exists()
            if move:
                self.env['game.customer']._receive_invoice(move)
```

Triggering the agent's own cron from here, rather than acting inline, keeps
it an agent reacting to its mail.

The vendor agent still ships on confirmation (`GAME_STATE.md` §6.1). A
purchase order the player sends by email lands in the vendor's outside
mailbox, where a future vendor that reads email will find it.

## 9. Known issues and risks

- **Only email.** SMS, web push and other IAP-backed services are not
  intercepted. `sms.sms` goes through IAP, and in a world it would still reach
  the real service if one is configured.
- **`odoo-mailgate.py`**, the script that feeds mail to Odoo over XML-RPC from
  an MTA, is outside Odoo's process and cannot be stopped from inside it. A
  world should not be wired to one.
- **The post office runs while paused.** Its triggers are due at the paused
  game instant, and the loop keeps polling. Sending from the game page is
  refused while the world stands still, but the Odoo backend is not guarded
  (`GAME_STATE.md` §8), so mail Odoo sends then is delivered then.
- **A gateway refusal is recorded, not bounced.** When Odoo's gateway raises
  rather than bouncing, the sender is not told. The failure is in
  *Deliveries*. A mail-server bounce would be the realistic answer.
- **An address on the alias domain that is not an employee is Odoo's.** If the
  company's own contact address (`res.company.email`) is on that domain and is
  no alias, mail to it fails in the gateway, as it would on a real
  catchall-only domain.
- **`sales@` is usually taken.** On a database with Accounting it is already
  the Sales Journal's alias, so the issue's own example needs another name for
  a CRM team's alias (`leads@`, say). That is Odoo's constraint, not the post's.
- **The inbox lists the newest 50**, and attachments are listed by name only;
  the page cannot download them.
- **No Bcc and no reply-all from the page.**

## 10. Testing

```bash
odoo-bin -d <db> -u odoo_sim --test-tags /odoo_sim,/base:TestGameClockMail --stop-after-init
cd odoo_sim/ui && npm test
```

`addons/odoo_sim/tests/test_mail.py` pins a running world at a game instant
years away from real time, and in every test that lets Odoo send, it trips
`smtplib` itself:

- **outgoing:** a `mail.mail` is posted and never reaches SMTP; outside a
  world Odoo sends as it always did; Bcc stays off the letter; a notification
  records the record it is about; the post carries game time in `Date`;
  a preview drops the invisible padding of Odoo's layout; posting wakes the
  post office;
- **addressing:** employees (even on the alias domain, in any case), aliases,
  unknown addresses on the domain, the catchall, portal users and strangers
  each go where §5 says; a typo is refused;
- **delivery:** mail waits for the post office, then reaches exactly one
  player; **an alias makes a record as it always did**; an alias in Bcc is
  found through `Delivered-To`; **a reply from the inbox lands on the
  thread**, authored by the player; what Odoo cannot route fails with the
  reason while the rest is delivered; nothing is delivered twice; the cron's
  one-email-at-a-time mode; agents hear exactly their mail; no ORM write
  access;
- **nothing comes in:** an incoming mail server neither connects nor fetches;
- **over HTTP:** the snapshot and the page carry the player's mail; opening an
  email sanitizes it and proposes the reply; opening is a read and marking read
  is a write; mail is private (404 for someone else's, including as a reply's
  parent); sending, threading; refused in a paused world, without a recipient,
  with a typo, without an address of one's own; 403 for portal users.

`base/tests/test_game_clock.py` `TestGameClockMail` checks the core backstop
through the base class's own method, past the game's override.

The UI tests cover `describeMail`: folders and unread counts, the open email,
reply titles, and when the form may send.

Checked by hand at `--rate 720`, on the paperclip scenario with CRM
installed, through `game_run` and headless Chrome, with no outgoing mail
server configured:

1. The player writes to `leads@<alias domain>` from `/game`, and a CRM lead
   exists half a second later.
2. A chatter message to a customer reaches her outside mailbox, stamped in
   game time, with the catchall as Reply-To.
3. Her answer, written through `_send_from`, lands on her contact's chatter
   authored by her, and Odoo notifies the player by email, into the game inbox.
4. The player opens it and presses Reply, and the answer lands on the same
   chatter, authored by the player.
5. Odoo's periodic digest, sent by a cron, arrives in the game inbox too.
