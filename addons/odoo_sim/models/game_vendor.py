# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Vendors outside the company, and the shipments they send it.

A purchased product enters the world when the player accepts its delivery.
Validating the receipt in Odoo is the player *recording* that it arrived, and
the world does not read it.  See ``odoo_sim/GAME_STATE.md`` section 6.
"""
from datetime import timedelta

from odoo import Command, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, float_is_zero


class GameVendor(models.Model):
    """An automated supplier: it reads the orders it is sent, and ships them."""
    _name = 'game.vendor'
    _description = "Game world: vendor"
    _rec_name = 'partner_id'

    partner_id = fields.Many2one(
        'res.partner', "Contact", required=True, index=True, ondelete='restrict',
        help="The company the player sends purchase orders to.")
    lead_time = fields.Float(
        "Lead time (game hours)", required=True, default=24.0,
        help="Game time between the vendor receiving an order and its delivery arriving.")
    product_ids = fields.Many2many(
        'product.product', string="Catalogue",
        help="What this vendor can actually ship. Anything else on an order is not delivered.")
    shipment_ids = fields.One2many('game.shipment', 'vendor_id', "Shipments")

    _partner_uniq = models.Constraint(
        'UNIQUE(partner_id)',
        "A company is one vendor in the world.",
    )

    @api.model
    def _cron_process_orders(self):
        """ The vendor agent: every vendor ships every confirmed order it has not yet shipped.

        Triggered when the player confirms a purchase order (``purchase_order.py``)
        and not run inline there, so that the vendor stays an agent reacting to
        what it has been sent.  Today "sent" means "confirmed in Odoo"; once
        vendors read email it will mean the email, and only this trigger moves.
        """
        vendors = self.search([])
        if not vendors:
            return
        # purchase_id IS NOT NULL: a NULL in the subquery would make NOT IN
        # match nothing at all.
        shipped = self.env['game.shipment']._search([('purchase_id', '!=', False)])
        orders = self.env['purchase.order'].search([
            ('state', '=', 'purchase'),
            ('partner_id.commercial_partner_id', 'in', vendors.partner_id.ids),
            ('id', 'not in', shipped.subselect('purchase_id')),
        ], order='date_approve, id')
        for order in orders:
            vendor = vendors.filtered(lambda v: v.partner_id == order.partner_id.commercial_partner_id)
            vendor._ship(order)

    def _ship(self, order):
        """ Ship ``order`` as it reads now, and return the shipment (if any).

        **A snapshot.**  The lines are copied, converted to each product's own
        unit (two spools become 100 m), and never read from the order again:
        the vendor has its copy of what was ordered, and the player editing the
        purchase order afterwards does not change what is on the truck.

        Lines for products outside the vendor's catalogue are not shipped.  The
        player finds out the way anyone would, when the delivery is short.
        """
        self.ensure_one()
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        lines = [
            Command.create({'product_id': line.product_id.id, 'qty': line.product_uom_qty})
            for line in order.order_line
            if line.product_id in self.product_ids
            and not float_is_zero(line.product_uom_qty, precision_digits=digits)
        ]
        if not lines:
            return self.env['game.shipment']
        now = fields.Datetime.now()
        shipment = self.env['game.shipment'].create({
            'vendor_id': self.id,
            'purchase_id': order.id,
            'date_shipped': now,
            'date_arrival': now + timedelta(hours=self.lead_time),
            'line_ids': lines,
        })
        self.env['game.world']._schedule_settle(shipment.date_arrival)
        self.env['game.world']._changed()
        return shipment


class GameShipment(models.Model):
    _name = 'game.shipment'
    _description = "Game world: shipment"
    _order = 'date_arrival, id'

    vendor_id = fields.Many2one('game.vendor', required=True, readonly=True, index=True, ondelete='restrict')
    purchase_id = fields.Many2one(
        'purchase.order', "Purchase order", readonly=True, index='btree_not_null', ondelete='set null',
        help="The order this ships. Read once, when it was shipped, and never again.")
    state = fields.Selection([
        ('in_transit', "In transit"),
        ('arrived', "Arrived"),
        ('accepted', "Accepted"),
    ], required=True, readonly=True, default='in_transit', index=True)
    date_shipped = fields.Datetime(required=True, readonly=True)
    date_arrival = fields.Datetime(required=True, readonly=True, index=True)
    date_accepted = fields.Datetime(readonly=True)
    line_ids = fields.One2many('game.shipment.line', 'shipment_id', "Contents", readonly=True)
    entry_ids = fields.One2many('game.stock.entry', 'shipment_id', "Ledger")

    # One shipment per order, which is what makes the vendor agent idempotent.
    _purchase_uniq = models.Constraint(
        'UNIQUE(purchase_id)',
        "A vendor ships an order once.",
    )

    def _compute_display_name(self):
        for shipment in self:
            shipment.display_name = shipment.purchase_id.name or self.env._("Shipment %s", shipment.id)

    def _accept(self):
        """ Accept the delivery: its contents now exist in the world. """
        self.ensure_one()
        # It may have arrived since the last settle.
        self.env['game.world']._settle()
        self.env.cr.execute(SQL("SELECT id FROM game_shipment WHERE id = %s FOR UPDATE", self.id))
        self.invalidate_recordset(['state'])
        if self.state == 'in_transit':
            raise UserError(self.env._("%s has not arrived yet.", self.display_name))
        if self.state == 'accepted':
            raise UserError(self.env._("%s has already been accepted.", self.display_name))

        stock = self.env['game.stock']
        for line in self.line_ids:
            stock._apply(line.product_id, line.qty, 'received', shipment=self)
        self.write({'state': 'accepted', 'date_accepted': fields.Datetime.now()})
        self.env['game.world']._changed()


class GameShipmentLine(models.Model):
    _name = 'game.shipment.line'
    _description = "Game world: shipment line"
    _rec_name = 'product_id'

    shipment_id = fields.Many2one('game.shipment', required=True, index=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', required=True, readonly=True, ondelete='restrict')
    qty = fields.Float(
        "Quantity", digits='Product Unit', required=True, readonly=True,
        help="In the product's own unit of measure, whatever unit it was ordered in.")
    uom_id = fields.Many2one(related='product_id.uom_id')
