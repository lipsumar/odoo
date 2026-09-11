# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for customers: asking, invoices, payment and delivery (GAME_STATE.md 7).

The test clip is sold without taxes, so an invoice's total is what the tests
say it is.  Time is never frozen (see test_world): settling is always given an
instant relative to the record under test.

Customers read their mail: an invoice reaches one by being emailed to it, and
delivered by the post office.  Under test, ``ir.mail_server._disable_send``
holds every ``mail.mail`` back, so ``sending()`` lets Odoo send -- into the
world's post, with a tripwire on SMTP, as in test_mail.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command, game_clock
from odoo.addons.base.models.ir_mail_server import IrMail_Server
from odoo.game_clock import GameClock
from odoo.tests import new_test_user, tagged
from odoo.tests.common import HttpCase

from odoo.addons.odoo_sim.tests.test_bank import BankCase


class CustomerCase(BankCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.clip.taxes_id = False
        cls.buyer.email = 'buyer@outside.example.com'
        cls.seller = new_test_user(cls.env, 'seller', groups='base.group_user', email='seller@company.example.com')
        cls.customer = cls.env['game.customer'].create({
            'partner_id': cls.buyer.id,
            'product_id': cls.clip.id,
            'qty': 100,
            'max_price': 0.10,
            'payment_delay': 4,
            'interval': 24,
            'write_to': cls.seller.email,
        })
        cls.accounts._deposit(cls.buyer.id, 50)
        cls.Email = cls.env['game.email']
        cls.Delivery = cls.env['game.email.delivery']

    @contextmanager
    def sending(self):
        """ Let Odoo send, which in a world means posting -- and never SMTP. """
        tripwire = AssertionError("an SMTP connection was attempted")
        with patch.object(IrMail_Server, '_disable_send', return_value=False), \
                patch('smtplib.SMTP', side_effect=tripwire), \
                patch('smtplib.SMTP_SSL', side_effect=tripwire):
            yield

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
        with self.sending():
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


class TestCustomerOrders(CustomerCase):

    def test_a_customer_asks_for_goods_by_writing_to_the_company(self):
        order = self.place()

        self.assertEqual((order.state, order.product_id, order.qty, order.max_price),
                         ('requested', self.clip, 100, 0.10))
        [email] = self.written("Order: 100 Test clip")
        self.assertEqual(email.delivery_ids.address, self.seller.email)

    def test_an_order_lands_in_the_players_inbox(self):
        self.place()
        self.Delivery._deliver()

        [email] = self.written("Order")
        self.assertEqual((email.delivery_ids.route, email.delivery_ids.user_id), ('player', self.seller))

    def test_a_customer_with_nowhere_to_write_still_orders(self):
        self.customer.write_to = False
        with self.assertLogs('odoo.addons.odoo_sim.models.game_customer', 'WARNING'):
            order = self.place()

        self.assertEqual(order.state, 'requested', "and the page shows it")
        self.assertFalse(self.written("Order"))

    def test_a_customer_has_one_order_open_at_a_time(self):
        self.place()
        self.customer._cron_place_orders()

        self.assertEqual(len(self.customer.order_ids), 1)

    def test_a_customer_asks_again_a_while_after_it_has_its_goods(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        order._deliver()
        self.customer._cron_place_orders()
        self.assertEqual(len(self.customer.order_ids), 1, "not yet")

        self.assertEqual(self.customer.next_order_date, order.date_delivered + timedelta(hours=24))
        cron = self.env.ref('odoo_sim.ir_cron_customer_orders')
        triggers = self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)])
        self.assertIn(self.customer.next_order_date, triggers.mapped('call_at'))

    def test_a_new_customer_wakes_the_agent(self):
        cron = self.env.ref('odoo_sim.ir_cron_customer_orders')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        self.env['game.customer'].create({
            'partner_id': self.env['res.partner'].create({'name': "Another buyer"}).id,
            'product_id': self.clip.id, 'max_price': 1,
        })
        self.assertGreater(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)


class TestCustomerInvoices(CustomerCase):

    def test_mail_reaching_a_customer_wakes_the_customers(self):
        cron = self.env.ref('odoo_sim.ir_cron_customer_mail')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        self.send(self.invoice())

        self.assertGreater(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)

    def test_an_invoice_nobody_sent_is_not_read(self):
        """ Posting is the player's bookkeeping; sending is telling the customer. """
        self.place()
        invoice = self.invoice()
        self.customer._cron_read_mail()

        self.assertFalse(self.received(invoice))

    def test_an_invoice_emailed_to_someone_else_is_not_read(self):
        self.place()
        invoice = self.invoice()
        invoice.message_unsubscribe(self.buyer.ids)
        stranger = self.env['res.partner'].create({'name': "Stranger", 'email': 'stranger@outside.example.com'})
        self.send(invoice, stranger)
        self.customer._cron_read_mail()

        self.assertFalse(self.received(invoice))

    def test_other_mail_is_read_and_left_alone(self):
        email = self.send(self.buyer, self.buyer)
        self.customer._cron_read_mail()

        self.assertTrue(email.delivery_ids.is_read)
        self.assertFalse(self.env['game.customer.invoice'].search([('customer_id', '=', self.customer.id)]))

    def test_an_invoice_it_agrees_with_is_paid_when_it_said(self):
        order = self.place()
        invoice = self.invoice(qty=100, price=0.08)
        received = self.read(invoice)

        self.assertEqual((received.state, received.amount, received.qty), ('to_pay', 8, 100))
        self.assertEqual(received.date_due, received.date_received + timedelta(hours=4))
        self.assertEqual(order.state, 'invoiced')

        self.world._settle(received.date_due - timedelta(seconds=1))
        self.assertEqual(self.earned(), 0, "not yet")

        self.world._settle(received.date_due)
        self.assertEqual(self.earned(), 8)
        self.assertEqual(self.balance(self.buyer_account), 42)
        self.assertEqual((received.state, received.transaction_id.reference, received.transaction_id.date),
                         ('paid', invoice.payment_reference, received.date_due))
        self.assertEqual((order.state, order.qty_paid, order.amount_paid), ('paid', 100, 8))

    def test_an_agreed_payment_is_scheduled(self):
        self.place()
        received = self.read(self.invoice())

        cron = self.env.ref('odoo_sim.ir_cron_world_settle')
        triggers = self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)])
        self.assertIn(received.date_due, triggers.mapped('call_at'))

    def test_an_invoice_is_read_once(self):
        self.place()
        invoice = self.invoice()
        self.read(invoice)
        self.read(invoice)
        self.env['game.customer']._receive_invoice(invoice)

        self.assertEqual(len(self.received(invoice)), 1)

    def test_what_is_paid_is_what_the_invoice_said_when_it_arrived(self):
        """ Resetting the invoice and raising the price afterwards changes nothing. """
        self.place()
        invoice = self.invoice(price=0.08)
        received = self.read(invoice)
        invoice.button_draft()
        invoice.invoice_line_ids.price_unit = 0.5
        invoice.action_post()
        self.world._settle(received.date_due)

        self.assertEqual(self.earned(), 8)

    def test_too_expensive_is_refused_and_said_so(self):
        order = self.place()
        invoice = self.invoice(price=0.2)
        received = self.read(invoice)

        self.assertEqual(received.state, 'refused')
        self.assertRegex(received.reason, r"more than we pay")
        self.assertEqual(order.state, 'requested', "still waiting for an invoice it can pay")
        [reply] = self.written(f"Re: {invoice.name}")
        self.assertEqual(reply.in_reply_to, received.email_id.message_id, "it answers the invoice's email")
        self.assertIn(reply.delivery_ids.address, (received.email_id.reply_to or received.email_id.email_from))
        self.world._settle(received.date_received + timedelta(days=30))
        self.assertEqual(self.earned(), 0)

    def test_more_than_was_asked_for_is_refused(self):
        self.place()
        self.assertRegex(self.read(self.invoice(qty=150, price=0.01)).reason, r"We asked for 100 Test clip, not 150")

    def test_an_invoice_for_something_else_is_refused(self):
        self.place()
        self.assertRegex(self.read(self.invoice(product=self.wire)).reason, r"not for the Test clip")

    def test_an_invoice_for_nothing_ordered_is_refused(self):
        self.assertRegex(self.read(self.invoice()).reason, r"not ordered anything")

    def test_one_order_is_paid_for_once(self):
        self.place()
        self.read(self.invoice())
        self.assertRegex(self.read(self.invoice()).reason, r"not paying twice")

    def test_an_invoice_for_part_of_the_order_is_paid_for_that_part(self):
        order = self.paid_order(qty=60)
        self.assertEqual((order.qty_paid, order.amount_paid), (60, 4.8))

    def test_an_invoice_to_a_customer_contact_reaches_the_customer(self):
        self.place()
        contact = self.env['res.partner'].create({
            'name': "Accounts payable", 'parent_id': self.buyer.id, 'email': 'ap@outside.example.com',
        })
        invoice = self.invoice()
        invoice.button_draft()
        invoice.partner_id = contact
        invoice.action_post()

        self.assertEqual(self.read(invoice, contact).state, 'to_pay')

    def test_a_customer_who_cannot_pay_says_so(self):
        order = self.place()
        received = self.read(self.invoice())
        self.buyer_account._pay(self.company_account, 45, "spent elsewhere")
        self.world._settle(received.date_due)

        self.assertEqual(received.state, 'refused')
        self.assertRegex(received.reason, r"cannot pay")
        self.assertEqual(order.state, 'requested')
        self.assertEqual(self.earned(), 45, "only what it spent elsewhere")

    def test_draft_invoices_and_credit_notes_are_not_read(self):
        self.place()
        draft = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.buyer.id,
            'invoice_line_ids': [Command.create({'product_id': self.clip.id, 'quantity': 1, 'price_unit': 0.08})],
        })
        refund = self.env['account.move'].create({
            'move_type': 'out_refund', 'partner_id': self.buyer.id,
            'invoice_line_ids': [Command.create({'product_id': self.clip.id, 'quantity': 1, 'price_unit': 0.08})],
        })
        refund.action_post()
        self.send(draft)
        self.send(refund)
        self.customer._cron_read_mail()

        self.assertFalse(self.received(draft) | self.received(refund))


class TestCustomerDelivery(CustomerCase):

    def test_shipping_takes_the_goods_out_of_the_world(self):
        self.give(self.clip, 120)
        order = self.paid_order()
        order._deliver()

        self.assertEqual(order.state, 'delivered')
        self.assertEqual(self.on_hand(self.clip), 20)
        self.assertEqual((order.entry_ids.kind, order.entry_ids.qty), ('delivered', -100))

    def test_what_ships_is_what_was_paid_for(self):
        self.give(self.clip, 100)
        order = self.paid_order(qty=60)
        order._deliver()

        self.assertEqual(self.on_hand(self.clip), 40)

    def test_nothing_ships_before_it_is_paid_for(self):
        self.give(self.clip, 100)
        order = self.place()
        with self.refused("has not paid"):
            order._deliver()

        received = self.read(self.invoice())
        with self.refused("has not paid"):
            order._deliver()

        # Shipping settles first: a payment that has come due counts.
        received.date_due = received.date_received - timedelta(minutes=1)
        order._deliver()
        self.assertEqual(order.state, 'delivered')

    def test_what_does_not_exist_cannot_be_shipped(self):
        self.give(self.clip, 99)
        order = self.paid_order()
        with self.refused("not enough Test clip"):
            order._deliver()

        self.assertEqual(order.state, 'paid')
        self.assertEqual(self.on_hand(self.clip), 99)

    def test_an_order_ships_once(self):
        self.give(self.clip, 200)
        order = self.paid_order()
        order._deliver()
        with self.refused("already been delivered"):
            order._deliver()

        self.assertEqual(self.on_hand(self.clip), 100)

    def test_validating_a_delivery_in_odoo_ships_nothing(self):
        """ Odoo can say the clips went out. They are still on the shelf. """
        self.give(self.clip, 100)
        self.env['stock.quant']._update_available_quantity(
            self.clip, self.env['stock.warehouse'].search([], limit=1).lot_stock_id, 100)
        sale = self.env['sale.order'].create({
            'partner_id': self.buyer.id,
            'order_line': [Command.create({'product_id': self.clip.id, 'product_uom_qty': 100})],
        })
        sale.action_confirm()
        sale.picking_ids.move_ids.picked = True
        sale.picking_ids.button_validate()

        self.assertEqual(sale.picking_ids.state, 'done', "Odoo believes it")
        self.assertEqual(self.on_hand(self.clip), 100, "the world does not")


class TestCustomerSnapshot(CustomerCase):

    def test_the_snapshot_shows_the_bank_and_what_customers_want(self):
        order = self.place()
        received = self.read(self.invoice(price=0.2))
        snapshot = self.world._snapshot()

        self.assertIn("Test clip", [line['name'] for line in snapshot['stock']], "a customer wants it")
        shown = next(o for o in snapshot['customer_orders'] if o['id'] == order.id)
        self.assertEqual(
            {key: shown[key] for key in ('customer', 'qty', 'max_price', 'state')},
            {'customer': "Test buyer", 'qty': 100, 'max_price': 0.10, 'state': 'requested'},
        )
        self.assertEqual((shown['invoice']['id'], shown['invoice']['state']), (received.id, 'refused'))
        self.assertIn("more than we pay", shown['invoice']['reason'])

        bank = snapshot['bank']
        self.assertEqual((bank['number'], bank['currency']),
                         (self.company_account.number, self.company_account.currency_id.name))

    def test_the_bank_shows_money_in_and_out_from_the_company_side(self):
        self.buyer_account._pay(self.company_account, 10, "in")
        self.company_account._pay(self.buyer_account, 4, "out")
        bank = self.world._snapshot()['bank']

        self.assertEqual(bank['balance'], self.company_start + 6)
        self.assertEqual(
            [(t['amount'], t['counterparty'], t['reference']) for t in bank['transactions'][:2]],
            [(-4, "Test buyer", "out"), (10, "Test buyer", "in")],
        )

    def test_delivered_orders_leave_the_snapshot(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        order._deliver()

        self.assertNotIn(order.id, [o['id'] for o in self.world._snapshot()['customer_orders']])


@tagged('-at_install', 'post_install')
class TestCustomerApi(CustomerCase, HttpCase):
    """Shipping over real HTTP, with the clock pinned as in test_world's TestWorldApi."""

    def setUp(self):
        super().setUp()
        self.addCleanup(game_clock.invalidate, self.env.cr.dbname)
        now = datetime.now()
        game_clock.override(self.env.cr.dbname, GameClock(now, now, 1.0, False, timedelta(hours=1)))
        self.authenticate('admin', 'admin')

    def deliver(self, order_id):
        return self.url_open(f'/game/api/customer_orders/{order_id}/deliver', json={}, method='POST')

    def test_shipping_a_paid_order(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        response = self.deliver(order.id)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(order.id, [o['id'] for o in response.json()['customer_orders']])
        self.assertEqual(self.on_hand(self.clip), 0)

    def test_shipping_an_unpaid_order_is_refused(self):
        self.give(self.clip, 100)
        response = self.deliver(self.place().id)

        self.assertEqual(response.status_code, 422)
        self.assertIn("has not paid", response.json()['message'])
        self.assertEqual(self.on_hand(self.clip), 100)

    def test_an_unknown_order_is_not_found(self):
        self.assertEqual(self.deliver(999999).status_code, 404)
