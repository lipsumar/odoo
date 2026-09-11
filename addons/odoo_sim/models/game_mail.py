# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The world's post: every email sent in it, and what became of each copy.

A game world is a closed system (``odoo_sim/MAIL.md``): no real email leaves it
and none comes in.  Everything is *posted* here instead -- what Odoo sends
(``ir_mail_server.py`` catches it where it would have reached SMTP), what the
player writes on the game page, and what the world's agents write -- and the
post office delivers each copy by address, to one of three kinds of mailbox:

* ``player``: an employee's own address, read on the game page;
* ``odoo``: one of the company's aliases, or anything else on its alias
  domain, handed to Odoo's mail gateway exactly as fetchmail would hand it;
* ``outside``: anyone else -- a mailbox outside the company, read by agents.
"""
import base64
import email
import email.policy
import logging
import re
from datetime import timezone
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, make_msgid

from odoo import api, fields, game_clock, models
from odoo.exceptions import UserError
from odoo.tools.mail import (
    decode_message_header,
    email_normalize,
    formataddr,
    html2plaintext,
    html_sanitize,
    plaintext2html,
)

from odoo.addons.odoo_sim.models.game_world import _instant

_logger = logging.getLogger(__name__)

ROUTES = [
    ('player', "Player"),
    ('odoo', "Odoo"),
    ('outside', "Outside"),
]

#: How many emails of each folder the page is handed.
MAILBOX_LIMIT = 50

#: Length of the one-line preview a mailbox lists.
PREVIEW_LENGTH = 160

#: Emails the post-office cron delivers per run, each in its own transaction.
CRON_BATCH = 100

RE_PREFIX = re.compile(r'^\s*re\s*:', re.IGNORECASE)

#: Characters that take no space.  Odoo's email layout pads its preheader with
#: them so that a mail client's preview stops after the text; so must ours.
INVISIBLE = dict.fromkeys(map(ord, '­͏​‌‍⁠﻿'))


def is_world(cr):
    """ Whether ``cr`` is on a game world: a database whose mail never leaves it. """
    return game_clock.clock_for(cr.dbname, cr) is not None


def _text(message):
    """ The readable text of ``message``: its plain part, or its HTML one flattened. """
    part = message.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return ''
    content = part.get_content()
    text = html2plaintext(content) if part.get_content_subtype() == 'html' else content
    return text.translate(INVISIBLE)


class GameEmail(models.Model):
    """One email, as it was sent.  Never edited: a sent email is a fact."""
    _name = 'game.email'
    _description = "Game world: email"
    _order = 'date desc, id desc'
    _rec_name = 'subject'

    date = fields.Datetime(required=True, readonly=True, index=True, help="The game instant it was posted.")
    message_id = fields.Char("Message-Id", required=True, readonly=True, index=True)
    email_from = fields.Char("From", readonly=True)
    reply_to = fields.Char("Reply-To", readonly=True)
    email_to = fields.Char("To", readonly=True)
    email_cc = fields.Char("Cc", readonly=True)
    subject = fields.Char(readonly=True)
    preview = fields.Char(readonly=True)
    in_reply_to = fields.Char("In-Reply-To", readonly=True)
    references = fields.Char(readonly=True)
    raw = fields.Binary(
        "Message", attachment=False, readonly=True,
        help="The message itself, as it was posted: what Odoo's gateway is handed.")
    origin = fields.Selection([
        ('odoo', "Odoo"),
        ('player', "Player"),
        ('outside', "Outside"),
    ], required=True, readonly=True, help="Who posted it: Odoo's mail, the game page, or someone outside.")
    author_id = fields.Many2one(
        'res.users', "Written by", readonly=True, index='btree_not_null', ondelete='set null',
        help="The player who wrote it on the game page.")
    res_model = fields.Char(
        "About", readonly=True,
        help="The model of the record Odoo said this email was about. Informational: the world never reads it.")
    res_id = fields.Many2oneReference("About (ID)", model_field='res_model', readonly=True)
    delivery_ids = fields.One2many('game.email.delivery', 'email_id', "Deliveries", readonly=True)

    # -- posting -----------------------------------------------------------

    @api.model
    def _post(self, message, recipients, *, origin, author=None):
        """ Put ``message`` in the post for ``recipients``, and return it.

        **The one way an email enters the world.**  ``recipients`` are the
        envelope's -- every address in To, Cc and Bcc, as an SMTP server would
        have been handed them.  Nothing is delivered here: the post office
        does that in its own transaction (:meth:`game.email.delivery._deliver`),
        so that nothing a recipient does on receipt -- Odoo's gateway making a
        lead, an agent answering -- runs inside the sender's transaction.

        The message is stamped with the game instant, as a mail server stamps
        what it relays.  ``_build_email__`` writes a real ``utcnow()`` into
        ``Date`` (a raw clock, DESIGN.md 5.4), and Odoo's gateway takes
        ``Date`` as the date of whatever it posts from the email.
        """
        if not isinstance(message, EmailMessage):
            message = email.message_from_bytes(message.as_bytes(), policy=email.policy.SMTP)
        now = fields.Datetime.now()
        if not message['Message-Id']:
            message['Message-Id'] = make_msgid()
        del message['Date']
        message['Date'] = format_datetime(now.replace(tzinfo=timezone.utc))
        # The envelope says who gets a copy; the letter does not tell the others.
        del message['Bcc']

        def header(name, separator=' '):
            return decode_message_header(message, name, separator=separator) or False

        message_id = message['Message-Id'].strip()
        res_model, res_id = self._about(message, message_id)
        posted = self.create({
            'date': now,
            'message_id': message_id,
            'email_from': header('From', ', '),
            'reply_to': header('Reply-To', ', '),
            'email_to': header('To', ', '),
            'email_cc': header('Cc', ', '),
            'subject': header('Subject'),
            'preview': ' '.join(_text(message).split())[:PREVIEW_LENGTH],
            'in_reply_to': header('In-Reply-To'),
            'references': header('References'),
            'raw': base64.b64encode(message.as_bytes()),
            'origin': origin,
            'author_id': author.id if author else False,
            'res_model': res_model,
            'res_id': res_id,
        })
        addresses = list(dict.fromkeys(filter(None, (email_normalize(r) for r in recipients))))
        self.env['game.email.delivery']._address(posted, addresses)
        cron = self.env.ref('odoo_sim.ir_cron_post_office', raise_if_not_found=False)
        if cron:
            cron._trigger()
        if author:
            self.env['game.world']._changed()
        return posted

    def _about(self, message, message_id):
        """ Return ``(model, id)`` of the record Odoo says ``message`` is about.

        Odoo's notifications name it in ``X-Odoo-Objects``
        (``mail/models/models.py`` ``_notify_by_email_get_headers``); failing
        that, Odoo's own copy of the message knows.  ``(False, False)`` for
        anything that is not about a record.
        """
        objects = str(message['X-Odoo-Objects'] or '').split(',')[0].strip()
        model, _dash, record_id = objects.rpartition('-')
        if model in self.env and record_id.isdigit():
            return model, int(record_id)
        known = self.env['mail.message'].search(
            [('message_id', '=', message_id), ('model', '!=', False), ('res_id', '!=', False)], limit=1)
        return (known.model, known.res_id) if known else (False, False)

    def _message(self):
        """ The email itself, parsed back out of the post. """
        self.ensure_one()
        return email.message_from_bytes(base64.b64decode(self.raw), policy=email.policy.SMTP)

    # -- writing -------------------------------------------------------------

    @api.model
    def _send_from(self, sender, to, subject, body, *, cc=(), attachments=(), parent=None, author=None):
        """ Write an email as ``sender`` and post it.  Returns the ``game.email``.

        The one call for anyone in the world who is not Odoo: the player on the
        game page (``author`` is then that user), and every agent.

        :param sender: a ``res.partner``, or a formatted address
        :param to: addresses -- a string of them, or a list of strings and partners
        :param cc: as ``to``
        :param body: HTML
        :param attachments: ``(filename, content, mimetype)`` triples
        :param parent: the ``game.email`` this answers, so that it threads --
            in Odoo's gateway too, which finds a reply by its References
        """
        if isinstance(sender, models.BaseModel):
            email_from, name = sender.email_formatted, sender.display_name
        else:
            email_from = name = sender
        if not email_normalize(email_from or ''):
            raise UserError(self.env._("%s has no email address to write from.", name or self.env._("The sender")))
        to_list, cc_list = self._addresses(to), self._addresses(cc)
        if not to_list and not cc_list:
            raise UserError(self.env._("Who is this email for? It has no recipient."))

        headers, references = {}, False
        if parent:
            headers['In-Reply-To'] = parent.message_id
            references = ' '.join(filter(None, [parent.references, parent.message_id]))
        mail_server = self.env['ir.mail_server']
        message = mail_server._build_email__(
            email_from, ', '.join(to_list), subject or '', body or '',
            email_cc=', '.join(cc_list), references=references,
            attachments=list(attachments) or None, subtype='html', headers=headers,
        )
        recipients = mail_server._prepare_smtp_to_list(message, None)
        return self._post(message, recipients, origin='player' if author else 'outside', author=author)

    @api.model
    def _addresses(self, value):
        """ Formatted addresses out of a string of them, or a list of strings and partners.

        Refuses anything that is not an address rather than dropping it, so
        that a typo reads as a typo and not as mail that silently went nowhere.
        """
        items = [value] if isinstance(value, (str, models.BaseModel)) else list(value or ())
        addresses, wrong = [], []
        for item in items:
            if isinstance(item, models.BaseModel):
                for partner in item:
                    if partner.email_normalized:
                        addresses.append(partner.email_formatted)
                    else:
                        wrong.append(partner.display_name)
                continue
            for name, address in getaddresses([item or '']):
                if not (name or address):
                    continue
                if email_normalize(address):
                    addresses.append(formataddr((name, address)))
                else:
                    wrong.append(address or name)
        if wrong:
            raise UserError(self.env._("Not an email address: %s", ', '.join(wrong)))
        return addresses

    # -- the player's mailbox ------------------------------------------------

    @api.model
    def _mailbox(self, user):
        """ ``user``'s mail, as the page lists it: the newest of each folder, no bodies. """
        mine = [('user_id', '=', user.id), ('state', '=', 'delivered')]
        unread = [('delivery_ids', 'any', mine + [('is_read', '=', False)])]
        inbox = self.search([('delivery_ids', 'any', mine)], limit=MAILBOX_LIMIT)
        unread_ids = set(self.search(unread + [('id', 'in', inbox.ids)]).ids)
        partner = user.partner_id
        return {
            'address': partner.email_formatted if partner.email_normalized else None,
            'unread': self.search_count(unread),
            'inbox': [dict(mail._listed(), unread=mail.id in unread_ids) for mail in inbox],
            'sent': [mail._listed() for mail in self.search([('author_id', '=', user.id)], limit=MAILBOX_LIMIT)],
        }

    def _listed(self):
        self.ensure_one()
        return {
            'id': self.id,
            'from': self.email_from or '',
            'to': ', '.join(filter(None, [self.email_to, self.email_cc])),
            'subject': self.subject or '',
            'preview': self.preview or '',
            'date': _instant(self.date),
        }

    def _visible_to(self, user):
        """ Whether ``user`` may read this: they received it, or wrote it. """
        self.ensure_one()
        return self.author_id.id == user.id or any(
            delivery.user_id.id == user.id and delivery.state == 'delivered'
            for delivery in self.delivery_ids
        )

    def _content(self):
        """ The whole email, as the page opens it.

        The body is sanitized here, and the page shows it in a sandboxed frame
        besides: an email is someone else's HTML.
        """
        self.ensure_one()
        message = self._message()
        part = message.get_body(preferencelist=('html', 'plain'))
        body = part.get_content() if part is not None else ''
        if part is not None and part.get_content_subtype() != 'html':
            body = plaintext2html(body)
        return dict(
            self._listed(),
            to=self.email_to or '',
            cc=self.email_cc or '',
            reply_to=self.reply_to or '',
            body=html_sanitize(body, sanitize_style=True),
            attachments=[part.get_filename() or self.env._("attachment") for part in message.iter_attachments()],
            reply=self._reply_defaults(),
        )

    def _reply_defaults(self):
        """ Who an answer goes to, and its subject: what any mail client would propose. """
        self.ensure_one()
        subject = self.subject or ''
        return {
            'to': self.reply_to or self.email_from or '',
            'subject': subject if RE_PREFIX.match(subject) else f"Re: {subject}",
        }

    def _mark_read_by(self, user):
        self.delivery_ids.filtered(lambda delivery: delivery.user_id.id == user.id).is_read = True


class GameEmailDelivery(models.Model):
    """One copy of an email, and what became of it at one address."""
    _name = 'game.email.delivery'
    _description = "Game world: email delivery"
    _order = 'date desc, id desc'
    _rec_name = 'address'

    email_id = fields.Many2one('game.email', "Email", required=True, readonly=True, index=True, ondelete='cascade')
    date = fields.Datetime(related='email_id.date', store=True, index=True)
    address = fields.Char(required=True, readonly=True, index=True)
    route = fields.Selection(ROUTES, required=True, readonly=True)
    user_id = fields.Many2one('res.users', "Player", readonly=True, index='btree_not_null', ondelete='cascade')
    state = fields.Selection([
        ('in_transit', "In transit"),
        ('delivered', "Delivered"),
        ('failed', "Failed"),
    ], required=True, readonly=True, default='in_transit', index=True)
    date_delivered = fields.Datetime(readonly=True)
    is_read = fields.Boolean("Read", readonly=True)
    failure = fields.Char(readonly=True, help="Why Odoo's gateway would not take it.")

    # -- addressing ----------------------------------------------------------

    @api.model
    def _address(self, posted, addresses):
        """ Decide where each of ``addresses`` (normalized) is, and put a copy of ``posted`` in transit to it.

        In order: an employee's own address is their inbox, even on the
        company's own domain, the way their mailbox would be on a real mail
        server; an address Odoo receives goes to its gateway; everything else
        is outside the company.  Decided when posted, like an envelope.
        """
        players = self.env['res.users'].search([('share', '=', False), ('email_normalized', 'in', addresses)])
        to_odoo = self._odoo_addresses(addresses)
        values = []
        for address in addresses:
            users = players.filtered(lambda user: user.email_normalized == address)
            if users:
                values += [
                    {'email_id': posted.id, 'address': address, 'route': 'player', 'user_id': user.id}
                    for user in users
                ]
            else:
                route = 'odoo' if address in to_odoo else 'outside'
                values.append({'email_id': posted.id, 'address': address, 'route': route})
        return self.create(values)

    @api.model
    def _odoo_addresses(self, addresses):
        """ Those of ``addresses`` Odoo's own mail would receive.

        Its aliases, catchall and bounce addresses (``_find_aliases``), and
        anything else on an alias domain: the company's mail server sends the
        whole domain to Odoo, and Odoo's gateway bounces what it has no alias
        for, which is its business and not the post's.
        """
        domains = self.env['mail.alias.domain'].search([])
        aliases = set(domains._find_aliases(addresses))
        names = {name.lower() for name in domains.mapped('name')}
        return {address for address in addresses if address in aliases or address.rpartition('@')[2] in names}

    # -- delivering ----------------------------------------------------------

    @api.model
    def _cron_deliver(self):
        """ Deliver the post, one email per transaction.

        One per transaction, as fetchmail commits after each message: a refusal
        in Odoo's gateway rolls back a savepoint, and rolling back a savepoint
        clears the transaction's precommit queue (``sql_db.py``
        ``_FlushingSavepoint``) -- the tracking and notifications of whatever
        the gateway did with the emails before it, in the same transaction.
        Capped per run, so that mail begetting mail cannot hold the cron
        forever; what is left over wakes it again.
        """
        for _email in range(CRON_BATCH):
            if not self._deliver(limit=1):
                return
            self.env.cr.commit()
        self.env.ref('odoo_sim.ir_cron_post_office')._trigger()

    @api.model
    def _deliver(self, limit=None):
        """ Deliver what is in transit -- or the copies of the first ``limit``
        emails only -- and return the deliveries made or failed.

        Copies another transaction is delivering are skipped rather than
        waited on.  Transit takes no game time.
        """
        deliveries = self.env['game.world']._lock_due(self._name, 'in_transit', 'date', fields.Datetime.now())
        if limit:
            emails = deliveries.email_id[:limit]
            deliveries = deliveries.filtered(lambda delivery: delivery.email_id in emails)
        if not deliveries:
            return deliveries
        now = fields.Datetime.now()
        to_odoo = deliveries.filtered(lambda delivery: delivery.route == 'odoo')
        (deliveries - to_odoo).write({'state': 'delivered', 'date_delivered': now})
        for posted, copies in to_odoo.grouped('email_id').items():
            copies._hand_to_odoo(posted, now)
        deliveries.filtered(lambda delivery: delivery.route == 'outside')._received()
        self.env['game.world']._changed()
        return deliveries

    def _hand_to_odoo(self, posted, now):
        """ Give ``posted`` to Odoo's mail gateway, once for all of these addresses.

        Once, as Odoo's own fetchmail would: its gateway routes one message to
        every alias among its recipients, and would ignore a second copy as a
        duplicate Message-Id anyway.  ``Delivered-To`` names the addresses it
        came in for, as a receiving mail server would, which is how an alias
        that was only in Bcc is found.

        A refusal fails these deliveries and not the post: the gateway's
        ``ValueError`` ("no possible route") is recorded for whoever debugs it.
        """
        message = posted._message()
        for address in self.mapped('address'):
            message['Delivered-To'] = address
        try:
            with self.env.cr.savepoint():
                self.env['mail.thread'].message_process(False, message.as_bytes())
        except Exception as error:  # noqa: BLE001 - one bad email must not stop the post
            _logger.info("Odoo's mail gateway did not take %s: %s", posted.message_id, error)
            self.write({'state': 'failed', 'date_delivered': now, 'failure': str(error)})
        else:
            self.write({'state': 'delivered', 'date_delivered': now})

    def _received(self):
        """ Hook for the world's agents: these copies just reached outside mailboxes.

        Called once per delivery run with every copy it delivered to an outside
        address, in the post office's transaction.  Does nothing here.  An
        agent that reads its mail extends it (``_inherit =
        'game.email.delivery'``) and picks out the addresses it answers to --
        ``address``, and ``email_id.res_model`` / ``res_id`` for what Odoo said
        an email was about.  Triggering its own cron from here, rather than
        acting inline, keeps it an agent reacting to its mail, which is the
        choice the vendor makes for its orders (GAME_STATE.md 6.1).
        """
