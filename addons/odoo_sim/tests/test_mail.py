# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the world's post (see odoo_sim/MAIL.md).

Every class pins a running world with ``game_clock.override``, at a game
instant a few years off, so that the suite passes on an ordinary database as
on a world (DESIGN.md 8) and so that game time is visibly not real time.

Under test, ``ir.mail_server._disable_send`` holds every ``mail.mail`` back.
``sending()`` lets Odoo send where a test needs it to -- with a tripwire on
``smtplib``, so that "sent" can only ever mean "posted into the world".
"""
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from unittest.mock import patch

from odoo import game_clock
from odoo.addons.base.models.ir_mail_server import IrMail_Server
from odoo.exceptions import AccessError, UserError
from odoo.game_clock import GameClock
from odoo.tests import new_test_user, tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.odoo_sim.controllers import main

GAME_NOW = datetime(2031, 5, 4, 9, 0, 0)
DOMAIN = 'sim-mail.example.com'
CUSTOMER = 'carla@outside.example.com'


class MailCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dbname = cls.env.cr.dbname
        cls.addClassCleanup(game_clock.invalidate, cls.dbname)
        cls.world_at(GAME_NOW)

        # The company's mail: its domain, and one alias that makes products.
        cls.alias_domain = cls.env['mail.alias.domain'].create({'name': DOMAIN})
        cls.env.company.alias_domain_id = cls.alias_domain
        cls.env['mail.alias'].create({
            'alias_name': 'hello',
            'alias_domain_id': cls.alias_domain.id,
            'alias_model_id': cls.env['ir.model']._get_id('product.template'),
            'alias_contact': 'everyone',
        })

        # A player whose address is on the company's own domain, as employees' are.
        cls.player = new_test_user(
            cls.env, 'player', groups='base.group_user', password='player-password',
            name="Pat Player", email=f'player@{DOMAIN}', notification_type='email',
        )
        cls.other_player = new_test_user(
            cls.env, 'other', groups='base.group_user', password='other-password', email=f'other@{DOMAIN}',
        )
        cls.customer = cls.env['res.partner'].create({'name': "Carla Customer", 'email': CUSTOMER})
        cls.Email = cls.env['game.email']
        cls.Delivery = cls.env['game.email.delivery']

    def setUp(self):
        super().setUp()
        # Afresh for every test, so that a long run never sees its world stop.
        self.world_at(GAME_NOW)

    @classmethod
    def world_at(cls, game_now, *, paused=False):
        now = datetime.now()
        game_clock.override(cls.dbname, GameClock(game_now, now, 1.0, paused, timedelta(hours=1)))

    @contextmanager
    def sending(self):
        """ Let Odoo send, which in a world means posting -- and never SMTP. """
        tripwire = AssertionError("an SMTP connection was attempted")
        with patch.object(IrMail_Server, '_disable_send', return_value=False), \
                patch('smtplib.SMTP', side_effect=tripwire), \
                patch('smtplib.SMTP_SSL', side_effect=tripwire):
            yield

    def mail(self, to, subject="Hello", body="<p>Hello there</p>", sender=None, **kwargs):
        """ Someone outside writes to ``to``. """
        return self.Email._send_from(sender or self.customer, to, subject, body, **kwargs)

    def deliver(self):
        return self.Delivery._deliver()

    def inbox(self, user):
        return self.Email._mailbox(user)['inbox']


class TestOutgoing(MailCase):
    """What Odoo sends goes into the world's post, and nowhere else."""

    def test_odoo_mail_is_posted_and_never_reaches_smtp(self):
        mail = self.env['mail.mail'].create({
            'email_from': self.player.email_formatted,
            'email_to': CUSTOMER,
            'subject': "Your order",
            'body_html': "<p>It has shipped.</p>",
            'auto_delete': False,
        })
        with self.sending():
            mail.send()

        self.assertEqual(mail.state, 'sent', mail.failure_reason)
        posted = self.Email.search([('message_id', '=', mail.message_id)])
        self.assertEqual(posted.origin, 'odoo')
        self.assertEqual(posted.subject, "Your order")
        self.assertEqual(posted.preview, "It has shipped.")
        self.assertEqual(posted.delivery_ids.mapped('address'), [CUSTOMER])
        self.assertEqual(posted.delivery_ids.route, 'outside')
        self.assertEqual(posted.delivery_ids.state, 'in_transit', "delivering is the post office's job")

    def test_outside_a_world_odoo_sends_as_it_always_did(self):
        game_clock.override(self.dbname, None)
        mail_server = self.env['ir.mail_server']
        message = mail_server._build_email__('shop@outside.example.com', [CUSTOMER], "Hi", "Hi")
        message_id = mail_server.send_email(message)  # test mode: upstream skips the SMTP part

        self.assertEqual(message_id, message['Message-Id'])
        self.assertFalse(self.Email.search([('message_id', '=', message_id)]))

    def test_bcc_is_on_the_envelope_not_on_the_letter(self):
        mail_server = self.env['ir.mail_server']
        message = mail_server._build_email__(
            'shop@outside.example.com', [CUSTOMER], "Hi", "Hi", email_bcc=['auditor@outside.example.com'])
        with self.sending():
            message_id = mail_server.send_email(message)
        posted = self.Email.search([('message_id', '=', message_id)])

        self.assertIn('auditor@outside.example.com', posted.delivery_ids.mapped('address'))
        self.assertIsNone(posted._message()['Bcc'], "the other recipients are not told")

    def test_a_notification_says_what_it_is_about(self):
        acme = self.env['res.partner'].create({'name': "Acme"})
        with self.sending():
            note = acme.message_post(
                body="Call them back?", partner_ids=self.player.partner_id.ids,
                message_type='comment', subtype_xmlid='mail.mt_comment',
            )
        posted = self.Email.search([('message_id', '=', note.message_id)])

        self.assertEqual(posted.delivery_ids.user_id, self.player)
        self.assertEqual((posted.res_model, posted.res_id), ('res.partner', acme.id))

    def test_the_post_is_stamped_with_game_time(self):
        """ ``_build_email__`` writes a real ``utcnow()``; the world's post says game time. """
        posted = self.mail(self.player.email)

        self.assertLess(abs(posted.date - GAME_NOW), timedelta(hours=1))
        stamped = parsedate_to_datetime(posted._message()['Date']).replace(tzinfo=None)
        self.assertEqual(stamped, posted.date)

    def test_a_preview_is_what_a_person_would_read(self):
        """ Odoo's layout pads its preheader with invisible characters; a preview drops them. """
        posted = self.mail(self.player.email, body="<p>Yes, by Friday.</p>" + "&#847;&zwnj;&nbsp;" * 20 + "<p>Carla</p>")

        self.assertEqual(posted.preview, "Yes, by Friday. Carla")

    def test_posting_wakes_the_post_office(self):
        cron = self.env.ref('odoo_sim.ir_cron_post_office')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        self.mail(self.player.email)

        self.assertGreater(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)


class TestAddressing(MailCase):

    def test_every_address_goes_where_it_lives(self):
        """ An employee's address wins even on the company's domain; the rest is
        Odoo's if it is an alias or on an alias domain, and outside otherwise. """
        new_test_user(self.env, 'portal', groups='base.group_portal', email='portal@outside.example.com')
        posted = self.mail([
            f'PLAYER@{DOMAIN}', f'hello@{DOMAIN}', f'nobody@{DOMAIN}', f'catchall@{DOMAIN}',
            'portal@outside.example.com', 'someone@elsewhere.example.com',
        ])
        nobody = self.env['res.users']

        self.assertEqual({d.address: (d.route, d.user_id) for d in posted.delivery_ids}, {
            f'player@{DOMAIN}': ('player', self.player),
            f'hello@{DOMAIN}': ('odoo', nobody),
            f'nobody@{DOMAIN}': ('odoo', nobody),
            f'catchall@{DOMAIN}': ('odoo', nobody),
            'portal@outside.example.com': ('outside', nobody),
            'someone@elsewhere.example.com': ('outside', nobody),
        })

    def test_a_typo_is_refused_not_dropped(self):
        with self.assertRaisesRegex(UserError, "Not an email address: not-an-address"):
            self.mail(f'player@{DOMAIN}, not-an-address')
        with self.assertRaisesRegex(UserError, "no recipient"):
            self.mail('')


class TestDelivery(MailCase):

    def test_mail_reaches_the_player_when_the_post_office_runs(self):
        posted = self.mail(self.player.email)
        self.assertEqual(self.inbox(self.player), [], "in transit")

        self.deliver()
        [listed] = self.inbox(self.player)
        self.assertEqual(
            (listed['id'], listed['subject'], listed['preview'], listed['unread']),
            (posted.id, "Hello", "Hello there", True),
        )
        self.assertEqual(self.inbox(self.other_player), [], "one player's mail is not another's")

    def test_an_alias_works_as_it_always_did(self):
        """ The issue's own example: mail to an alias makes a record. """
        posted = self.mail(f'hello@{DOMAIN}', subject="Acme widget")
        self.deliver()

        self.assertEqual(posted.delivery_ids.state, 'delivered')
        widget = self.env['product.template'].search([('name', '=', "Acme widget")])
        self.assertTrue(widget, "Odoo's gateway made the record, as fetchmail would have")
        self.assertIn("Hello there", widget.message_ids.filtered(lambda m: m.message_type == 'email').body)

    def test_an_alias_in_bcc_is_found_by_delivered_to(self):
        """ Bcc leaves no trace in the headers; the receiving server says who it came for. """
        mail_server = self.env['ir.mail_server']
        message = mail_server._build_email__(
            CUSTOMER, [self.player.email], "Blind widget", "Hi", email_bcc=[f'hello@{DOMAIN}'])
        mail_server.send_email(message)
        self.deliver()

        self.assertTrue(self.env['product.template'].search([('name', '=', "Blind widget")]))

    def test_a_reply_lands_on_the_thread_it_answers(self):
        """ The player answers a notification from their inbox, and Odoo files the answer. """
        acme = self.env['res.partner'].create({'name': "Acme"})
        with self.sending():
            note = acme.message_post(
                body="Call them back?", partner_ids=self.player.partner_id.ids,
                message_type='comment', subtype_xmlid='mail.mt_comment',
            )
        self.deliver()
        notification = self.Email.search([('message_id', '=', note.message_id)])
        self.assertIn(f'catchall@{DOMAIN}', notification.reply_to, "Odoo asks for answers at its catchall")

        reply = notification._reply_defaults()
        self.Email._send_from(
            self.player.partner_id, reply['to'], reply['subject'], "<p>Called them.</p>",
            parent=notification, author=self.player,
        )
        self.deliver()

        answer = acme.message_ids.filtered(lambda m: "Called them" in (m.body or ''))
        self.assertEqual(answer.author_id, self.player.partner_id)

    def test_what_odoo_cannot_route_fails_and_says_why(self):
        posted = self.mail([f'nobody@{DOMAIN}', self.player.email])
        self.deliver()

        failed = posted.delivery_ids.filtered(lambda d: d.route == 'odoo')
        self.assertEqual(failed.state, 'failed')
        self.assertIn("No possible route", failed.failure)
        self.assertEqual(
            posted.delivery_ids.filtered(lambda d: d.route == 'player').state, 'delivered',
            "the rest of the post is not held up",
        )

    def test_nothing_is_delivered_twice(self):
        self.mail(f'hello@{DOMAIN}', subject="Only once")
        self.assertTrue(self.deliver())
        self.assertFalse(self.deliver())

        self.assertEqual(self.env['product.template'].search_count([('name', '=', "Only once")]), 1)

    def test_the_post_office_delivers_one_email_at_a_time_if_asked(self):
        first, second = self.mail(self.player.email), self.mail(self.player.email)

        self.assertEqual(self.Delivery._deliver(limit=1).email_id, first)
        self.assertEqual(self.Delivery._deliver(limit=1).email_id, second)

    def test_agents_hear_their_mail(self):
        heard = []
        with patch.object(type(self.Delivery), '_received', autospec=True, side_effect=heard.append):
            posted = self.mail([CUSTOMER, self.player.email], sender=self.player.partner_id)
            self.deliver()

        [batch] = heard
        self.assertEqual(batch.mapped('address'), [CUSTOMER], "only outside mailboxes")
        self.assertEqual(batch.email_id, posted)

    def test_nobody_writes_the_post_through_the_orm(self):
        admin = self.env.ref('base.user_admin')
        for model in ('game.email', 'game.email.delivery'):
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(admin).check_access('write')
        with self.assertRaises(AccessError):
            self.Email.with_user(self.player).check_access('read')


class TestNothingComesIn(MailCase):

    def test_an_incoming_mail_server_fetches_nothing(self):
        server = self.env['fetchmail.server'].create({
            'name': "A real inbox", 'server_type': 'imap', 'server': 'imap.example.com',
            'user': 'someone', 'password': 'secret',
        })
        tripwire = AssertionError("a real mail server was contacted")
        with patch('imaplib.IMAP4', side_effect=tripwire), patch('imaplib.IMAP4_SSL', side_effect=tripwire):
            with self.assertRaisesRegex(UserError, "no real email comes into it"):
                server.button_confirm_login()
            with self.assertRaisesRegex(UserError, "no real email comes into it"):
                server.fetch_mail()


@tagged('-at_install', 'post_install')
class TestMailApi(MailCase, HttpCase):
    """The player's mail over real HTTP."""

    def setUp(self):
        super().setUp()
        self.authenticate('player', 'player-password')

    def post(self, url, body=None):
        return self.url_open(url, json=body, method='POST')

    def send(self, **body):
        return self.post('/game/api/mail/send', body)

    def received(self, **kwargs):
        posted = self.mail(self.player.email, **kwargs)
        self.deliver()
        return posted

    def test_the_world_carries_the_players_mail(self):
        posted = self.received(subject="Welcome")
        mail = self.url_open('/game/api/world').json()['mail']

        self.assertIn(f'player@{DOMAIN}', mail['address'])
        self.assertEqual(mail['unread'], 1)
        self.assertEqual([m['id'] for m in mail['inbox']], [posted.id])
        self.assertEqual(mail['sent'], [])

    def test_the_page_carries_the_mail(self):
        posted = self.received()
        with patch.object(main, '_built_assets', return_value=([], [])):
            page = self.url_open('/game').text
        bootstrap = json.loads(re.search(r'var odooSim = (\{.*\});', page).group(1))

        self.assertEqual(bootstrap['world']['mail']['inbox'][0]['id'], posted.id)

    def test_opening_an_email(self):
        posted = self.received(subject="Quote", body='<p>Hi <b>Pat</b></p><script>alert("x")</script>')
        opened = self.url_open(f'/game/api/mail/{posted.id}').json()

        self.assertIn('<b>Pat</b>', opened['body'])
        self.assertNotIn('<script', opened['body'], "an email is someone else's HTML")
        self.assertEqual(opened['reply']['subject'], "Re: Quote")
        self.assertIn(CUSTOMER, opened['reply']['to'])

    def test_opening_marks_nothing_reading_does(self):
        posted = self.received()
        self.url_open(f'/game/api/mail/{posted.id}')
        self.assertEqual(self.Email._mailbox(self.player)['unread'], 1, "GET is a read")

        mail = self.post(f'/game/api/mail/{posted.id}/read').json()['mail']
        self.assertEqual(mail['unread'], 0)
        self.assertFalse(mail['inbox'][0]['unread'])

    def test_mail_is_private(self):
        theirs = self.mail(self.other_player.email)
        self.deliver()

        self.assertEqual(self.url_open(f'/game/api/mail/{theirs.id}').status_code, 404)
        self.assertEqual(self.post(f'/game/api/mail/{theirs.id}/read').status_code, 404)
        self.assertEqual(self.send(to=CUSTOMER, body="Me too", parent_id=theirs.id).status_code, 404)

    def test_sending_an_email(self):
        response = self.send(to=CUSTOMER, subject="Price list", body="Here it is.\nPat")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['mail']['sent'][0]['subject'], "Price list")
        posted = self.Email.search([('author_id', '=', self.player.id)])
        self.assertEqual(posted.origin, 'player')
        self.assertIn(f'player@{DOMAIN}', posted.email_from)
        self.assertEqual(posted.delivery_ids.route, 'outside')
        self.assertIn("Here it is.", posted._content()['body'])

    def test_an_answer_threads(self):
        original = self.received(subject="Quote")
        self.assertEqual(self.send(to=CUSTOMER, subject="Re: Quote", body="Yes", parent_id=original.id).status_code, 200)

        answer = self.Email.search([('author_id', '=', self.player.id)])
        self.assertEqual(answer.in_reply_to, original.message_id)
        self.assertIn(original.message_id, answer.references)

    def test_nothing_is_sent_from_a_world_that_stands_still(self):
        self.world_at(GAME_NOW, paused=True)
        response = self.send(to=CUSTOMER, subject="Hello", body="Hi")

        self.assertEqual(response.status_code, 422)
        self.assertIn("not running", response.json()['message'])
        self.assertFalse(self.Email.search([('author_id', '=', self.player.id)]))

    def test_an_email_is_for_someone(self):
        for to, why in (('', "no recipient"), ('not-an-address', "Not an email address")):
            with self.subTest(to=to):
                response = self.send(to=to, subject="Hello", body="Hi")
                self.assertEqual(response.status_code, 422)
                self.assertIn(why, response.json()['message'])

    def test_a_player_without_an_address_cannot_write(self):
        self.player.email = False
        response = self.send(to=CUSTOMER, subject="Hello", body="Hi")

        self.assertEqual(response.status_code, 422)
        self.assertIn("no email address", response.json()['message'])

    def test_mail_is_for_employees(self):
        new_test_user(self.env, 'customer', groups='base.group_portal', password='customer-password')
        self.authenticate('customer', 'customer-password')

        self.assertEqual(self.url_open('/game/api/world').status_code, 403)
        self.assertEqual(self.send(to=CUSTOMER, body="Hi").status_code, 403)
