# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The paperclip scenario, played once honestly (see odoo_sim/GAME_STATE.md 12).

The point is not the mechanics -- ``odoo_sim``'s own suite covers those -- but
that the scenario's two halves agree: a player who does in Odoo exactly what
happened in the world moves Odoo by exactly what happened in the world, goods
and money alike.

Movements, not totals: the database under test may be a world someone has been
playing in, where Odoo and the world already disagree (DESIGN.md 8).
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.tests.common import TransactionCase


class TestPaperclips(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.wire = cls.env.ref('odoo_sim_paperclips.product_wire')
        cls.clip = cls.env.ref('odoo_sim_paperclips.product_paperclip')
        cls.vendor = cls.env.ref('odoo_sim_paperclips.vendor_wire')
        cls.bench = cls.env.ref('odoo_sim_paperclips.workstation_paperclip_bench')
        cls.customer = cls.env.ref('odoo_sim_paperclips.customer_binder')
        cls.world = cls.env['game.world']
        cls.account = cls.env['game.bank.account']._company_account()
        cls.lines = cls.env['account.bank.statement.line']

    def setUp(self):
        super().setUp()
        # Let whatever a played world has in flight land first, or this test's
        # own settling would count it (and a busy bench would refuse the run).
        self.world._settle(fields.Datetime.now() + timedelta(days=3650))
        self.lines._game_bank_import(self.account)
        self.start = {product: self.levels(product) for product in (self.wire, self.clip)}
        self.money_start = self.money()

    def on_hand(self, product):
        self.env['game.stock'].invalidate_model()
        return self.env['game.stock'].search([('product_id', '=', product.id)]).qty

    def levels(self, product):
        """ ``(what Odoo says, what exists)`` for ``product``. """
        product.invalidate_recordset(['qty_available'])
        return product.qty_available, self.on_hand(product)

    def money(self):
        """ ``(what the bank journal says, what is in the bank)``. """
        self.account.invalidate_recordset(['balance'])
        journal_lines = self.lines.search([('journal_id', '=', self.account.journal_id.id)])
        return sum(journal_lines.mapped('amount')), self.account.balance

    def moved(self, product):
        """ How far each side has moved since the test began. """
        (odoo, world), (odoo_start, world_start) = self.levels(product), self.start[product]
        return odoo - odoo_start, world - world_start

    def earned(self):
        (odoo, bank), (odoo_start, bank_start) = self.money(), self.money_start
        return odoo - odoo_start, bank - bank_start

    def assertOdooMovedWithTheWorld(self):
        for product in (self.wire, self.clip):
            with self.subTest(product=product.name):
                odoo, world = self.moved(product)
                self.assertEqual(odoo, world)

    def buy_a_spool(self):
        """ Order a spool, take delivery of it, and record the receipt. """
        order = self.env['purchase.order'].create({
            'partner_id': self.vendor.partner_id.id,
            'order_line': [(0, 0, {'product_id': self.wire.id})],
        })
        order.button_confirm()
        self.vendor._cron_process_orders()
        shipment = self.env['game.shipment'].search([('purchase_id', '=', order.id)])
        self.world._settle(shipment.date_arrival)
        shipment._accept()
        order.picking_ids.move_ids.picked = True
        order.picking_ids.button_validate()
        return order

    def make_paperclips(self, qty):
        """ Make ``qty`` paperclips at the bench for an order, and record it. """
        mo = self.env['mrp.production'].create({'product_id': self.clip.id, 'product_qty': qty})
        mo.action_confirm()
        run = self.bench._start(qty, mo)
        self.world._settle(run.date_end)
        mo.button_mark_done()
        return mo

    def test_the_bom_starts_out_true(self):
        """ The player's model of the recipe starts out matching the world's. """
        bom = self.env.ref('odoo_sim_paperclips.bom_paperclip')
        recipe = self.env.ref('odoo_sim_paperclips.recipe_paperclip')

        self.assertEqual(bom.bom_line_ids.product_id, recipe.line_ids.product_id)
        self.assertEqual(bom.bom_line_ids.product_qty / bom.product_qty, recipe.line_ids.qty)
        self.assertEqual(bom.operation_ids.time_cycle_manual, recipe.duration)
        self.assertEqual(bom.operation_ids.workcenter_id, self.bench.workcenter_id)

    def test_buy_a_spool_make_paperclips_and_record_it_all(self):
        # Buy one spool. The vendor's pricelist puts it in spools already.
        order = self.env['purchase.order'].create({
            'partner_id': self.vendor.partner_id.id,
            'order_line': [(0, 0, {'product_id': self.wire.id})],
        })
        self.assertEqual(order.order_line.product_uom_id, self.env.ref('odoo_sim_paperclips.uom_spool'))
        order.button_confirm()

        # The vendor ships 50 m; it arrives; the player takes delivery ...
        self.vendor._cron_process_orders()
        shipment = self.env['game.shipment'].search([('purchase_id', '=', order.id)])
        self.world._settle(shipment.date_arrival)
        shipment._accept()
        self.assertEqual(self.moved(self.wire)[1], 50)

        # ... and records it.
        order.picking_ids.move_ids.picked = True
        order.picking_ids.button_validate()
        self.assertOdooMovedWithTheWorld()

        # Make ten paperclips at the bench, for an order ...
        mo = self.env['mrp.production'].create({'product_id': self.clip.id, 'product_qty': 10})
        mo.action_confirm()
        run = self.bench._start(10, mo)
        self.world._settle(run.date_end)
        self.assertEqual(self.moved(self.clip)[1], 10)
        self.assertEqual(self.moved(self.wire)[1], 49)

        # ... and record that too.
        mo.button_mark_done()
        self.assertEqual(mo.state, 'done')
        self.assertOdooMovedWithTheWorld()

    def test_sell_paperclips_and_get_paid(self):
        # Binder & Co. asks for paperclips (the customer agent's cron, run by hand).
        wanted = self.customer.order_ids.filtered(lambda o: o.state != 'delivered')
        if wanted.state not in (False, 'requested'):
            self.skipTest("Binder & Co. already has an order under way in this world")
        wanted = wanted or self.customer._place_order()
        self.assertEqual((wanted.product_id, wanted.qty), (self.clip, 200))

        self.buy_a_spool()
        self.make_paperclips(wanted.qty)

        # Quote, confirm, invoice.
        sale = self.env['sale.order'].create({
            'partner_id': self.customer.partner_id.id,
            'order_line': [Command.create({'product_id': self.clip.id, 'product_uom_qty': wanted.qty})],
        })
        sale.action_confirm()
        invoice = sale._create_invoices()
        invoice.action_post()

        # The customer reads the invoice, agrees, and pays when it said it would ...
        self.customer._cron_read_invoices()
        received = self.env['game.customer.invoice'].search([('invoice_id', '=', invoice.id)])
        self.assertEqual(received.state, 'to_pay', received.reason)
        self.world._settle(received.date_due)
        self.assertEqual(self.earned(), (0, invoice.amount_total), "in the bank; Odoo has not heard yet")

        # ... the bank feed tells Odoo ...
        line = self.lines._game_bank_import(self.account)
        self.assertEqual((line.amount, line.payment_ref, line.partner_id),
                         (invoice.amount_total, invoice.payment_reference, self.customer.partner_id))
        self.assertEqual(self.earned(), (invoice.amount_total, invoice.amount_total))

        # ... and the player ships, and records it.
        wanted._deliver()
        sale.picking_ids.move_ids.picked = True
        sale.picking_ids.button_validate()
        self.assertEqual(wanted.state, 'delivered')
        self.assertOdooMovedWithTheWorld()
