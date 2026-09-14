# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""What the suite's tests share: a running world to test in, and the company
the tests build in it.

Nothing here assumes the database it runs on.  Every test runs in a world
pinned with ``game_clock.override``, and makes what it needs -- products, a
chart of accounts, a customer -- so the suite passes the same on an ordinary
database as on a world someone has been playing in (DESIGN.md 8).

Time is never frozen.  On a world ``fields.Datetime.now()`` is game time, which
freezegun does not pin.  So settling is always given an explicit instant, taken
relative to the record under test: "a second before this run ends", never "at
ten o'clock".
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

from odoo import Command, fields, game_clock
from odoo.addons.base.models.ir_mail_server import IrMail_Server
from odoo.exceptions import UserError
from odoo.game_clock import GameClock
from odoo.tests import new_test_user
from odoo.tests.common import TransactionCase

from odoo.addons.odoo_sim.models.game_world import CHANGED


@contextmanager
def sending():
    """ Let Odoo send, which in a world means posting -- and never SMTP.

    Under test, ``ir.mail_server._disable_send`` holds every ``mail.mail``
    back.  This lets it go, with a tripwire on ``smtplib``, so that "sent" can
    only ever mean "posted into the world".
    """
    tripwire = AssertionError("an SMTP connection was attempted")
    with patch.object(IrMail_Server, '_disable_send', return_value=False), \
            patch('smtplib.SMTP', side_effect=tripwire), \
            patch('smtplib.SMTP_SSL', side_effect=tripwire):
        yield


def zone_at(hour):
    """ A time zone in which it is ``hour`` o'clock (and some minutes) now. """
    offset = (hour - fields.Datetime.now().hour) % 24
    if offset > 12:
        offset -= 24
    # The signs of Etc/GMT zones are the other way round: Etc/GMT-3 is UTC+3.
    return pytz.timezone(f"Etc/GMT{-offset:+d}") if offset else pytz.utc


def with_chart_of_accounts(env):
    """ Give the company a chart of accounts if it has none: invoices and bank journals need one. """
    if not env.company.chart_template:
        env['account.chart.template'].try_loading('generic_coa', env.company, install_demo=False)


class SimCase(TransactionCase):
    """A test in a running world.

    Pinned for the class, and afresh for every test, so that a long run never
    sees its world stop, and a test that paused it leaves nothing behind.
    """

    #: The game instant the world is pinned at; ``None`` for wherever its clock stands.
    game_now = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(game_clock.invalidate, cls.env.cr.dbname)
        cls.world_at(cls.game_now)

    def setUp(self):
        super().setUp()
        self.world_at(self.game_now)

    @classmethod
    def world_at(cls, game_now=None, *, paused=False, silent_for=timedelta(0)):
        """ Pin the world at ``game_now``, or where its clock stands, last ticked ``silent_for`` ago.

        Its ``max_gap`` is an hour, so that a slow test does not see its own
        world die under it.
        """
        dbname, now = cls.env.cr.dbname, datetime.now()
        if game_now is None:
            clock = game_clock.clock_for(dbname, cls.env.cr)
            game_now = clock.now() if clock else now
        game_clock.override(dbname, GameClock(game_now, now - silent_for, 1.0, paused, timedelta(hours=1)))


class WorldCase(SimCase):
    """A company with a wire vendor, and a bench that makes clips from wire."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.metre = cls.env.ref('uom.product_uom_meter')
        cls.unit = cls.env.ref('uom.product_uom_unit')
        cls.spool = cls.env['uom.uom'].create({
            'name': "Test spool (50 m)", 'relative_factor': 50, 'relative_uom_id': cls.metre.id,
        })
        cls.wire, cls.clip = cls.env['product.product'].create([
            {'name': "Test wire", 'is_storable': True, 'uom_id': cls.metre.id},
            {'name': "Test clip", 'is_storable': True, 'uom_id': cls.unit.id},
        ])
        cls.recipe = cls.env['game.recipe'].create({
            'product_id': cls.clip.id,
            'duration': 2,
            'line_ids': [Command.create({'product_id': cls.wire.id, 'qty': 0.1})],
        })
        cls.station = cls.env['game.workstation'].create({'name': "Test bench", 'recipe_id': cls.recipe.id})
        cls.partner = cls.env['res.partner'].create({'name': "Test wire vendor", 'is_company': True})
        cls.vendor = cls.env['game.vendor'].create({
            'partner_id': cls.partner.id, 'lead_time': 24, 'product_ids': [Command.link(cls.wire.id)],
        })
        cls.stock = cls.env['game.stock']
        cls.world = cls.env['game.world']

    def on_hand(self, product):
        """ What exists in the world, read back from the database. """
        self.stock.invalidate_model()
        return self.stock.search([('product_id', '=', product.id)]).qty

    def ledger_total(self, product):
        return sum(self.env['game.stock.entry'].search([('product_id', '=', product.id)]).mapped('qty'))

    def give(self, product, qty):
        self.stock._apply(product, qty, 'genesis')

    @contextmanager
    def refused(self, why):
        """ ``assertRaises(UserError)``, matching ``why``.

        Odoo's ``assertRaises`` rolls a savepoint back on the way out, the way
        a request's transaction would be; unittest's ``assertRaisesRegex`` does
        not, and would leave half an action behind for the test to trip over.
        """
        with self.assertRaises(UserError) as caught:
            yield
        self.assertRegex(str(caught.exception), why)

    def notices(self):
        """ The world-changed notices queued on this transaction's bus. """
        values = self.env.cr.precommit.data.get('bus.bus.values', [])
        return [value for value in values if f'"{CHANGED}"' in value['message']]


class BankCase(WorldCase):
    """The company's account at the game bank, fed into a bank journal, and a buyer's."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with_chart_of_accounts(cls.env)
        cls.accounts = cls.env['game.bank.account']
        cls.lines = cls.env['account.bank.statement.line']
        cls.company_account = cls.accounts._company_account()
        cls.buyer = cls.env['res.partner'].create({'name': "Test buyer", 'is_company': True})
        cls.buyer_account = cls.accounts._of(cls.buyer)

    def setUp(self):
        super().setUp()
        self.company_start = self.balance(self.company_account)

    def balance(self, account):
        account.invalidate_recordset(['balance'])
        return account.balance

    def earned(self):
        """ How far the company's balance has moved since the test began. """
        return self.balance(self.company_account) - self.company_start

    def transactions(self, account):
        return self.env['game.bank.transaction'].search(
            ['|', ('payer_id', '=', account.id), ('payee_id', '=', account.id)])


class CustomerCase(BankCase):
    """The buyer as a customer in the world, who reads its mail.

    The test clip is sold without taxes, so an invoice's total is what the
    tests say it is.  An invoice reaches the customer by being emailed to it,
    and delivered by the post office.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.clip.taxes_id = False
        cls.buyer.email = 'buyer@outside.example.com'
        cls.seller = new_test_user(cls.env, 'seller', groups='base.group_user', email='seller@company.example.com')
        cls.address = "Test buyer\n1 Test Street\n1000 Testville"
        cls.customer = cls.env['game.customer'].create({
            'partner_id': cls.buyer.id,
            'product_id': cls.clip.id,
            'qty': 100,
            'max_price': 0.10,
            'payment_delay': 4,
            'interval': 24,
            'complain_after': 72,
            'write_to': cls.seller.email,
            'address': cls.address,
        })
        cls.accounts._deposit(cls.buyer.id, 50)
        cls.Email = cls.env['game.email']
        cls.Delivery = cls.env['game.email.delivery']

    def place(self):
        self.customer._cron_place_orders()
        return self.customer.order_ids.filtered(lambda o: o.state != 'delivered')

    def invoice(self, qty=100, price=0.08, product=None):
        """ Post a customer invoice to the buyer, the way the player would. """
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_line_ids': [Command.create({
                'product_id': (product or self.clip).id, 'quantity': qty, 'price_unit': price, 'tax_ids': False,
            })],
        })
        invoice.action_post()
        return invoice

    def received(self, invoice):
        return self.env['game.customer.invoice'].search([('invoice_id', '=', invoice.id)])

    def send(self, record, partner=None):
        """ Email ``record`` to the buyer (or ``partner``), and deliver the post. Returns the email. """
        with sending():
            message = record.message_post(
                body="<p>Please find it attached.</p>", partner_ids=(partner or self.buyer).ids,
                message_type='comment', subtype_xmlid='mail.mt_comment',
            )
        self.Delivery._deliver()
        return self.Email.search([('message_id', '=', message.message_id)])

    def read(self, invoice, partner=None):
        """ Email ``invoice``, and have the customer read its mail. """
        self.send(invoice, partner)
        self.customer._cron_read_mail()
        return self.received(invoice)

    def written(self, subject):
        """ What the customer has emailed, with ``subject`` in its subject. """
        return self.Email.search([('email_from', 'ilike', self.buyer.email), ('subject', 'ilike', subject)])

    def paid_order(self, qty=100):
        """ An order the customer has asked for, been invoiced and paid for. """
        order = self.place()
        received = self.read(self.invoice(qty=qty))
        self.world._settle(received.date_due)
        return order

    def post(self, qty, address=None, product=None):
        """ Pack ``qty`` of the clip (or ``product``) in a new package, and send it to the buyer (or ``address``). """
        package = self.env['game.package']._new()
        package._pack(product or self.clip, qty)
        package._send(self.address if address is None else address)
        return package
