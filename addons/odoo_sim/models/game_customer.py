# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Customers outside the company: they ask for goods, pay for them, and receive them.

A customer is an agent, like a vendor.  It asks for goods by writing to the
company, reads the invoices the company sends it, pays the ones it agrees with
through the game bank, and has its goods once the player ships them in the
world.  Confirming a sale order, posting an invoice, registering a payment and
validating a delivery in Odoo are the player *recording* all of this; the world
reads an invoice once, when the customer receives it, and never again.  See
``odoo_sim/GAME_STATE.md`` section 7.
"""
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, float_compare, float_is_zero, format_amount

#: A customer has one order open at a time: it asks again once it has its goods.
OPEN_STATES = ('requested', 'invoiced', 'paid')


def _qty(value):
    """ 200.0 -> "200", 0.5 -> "0.5": how a person writes a quantity. """
    return f"{value:g}"


class GameCustomer(models.Model):
    """An automated customer: it asks for goods, and pays for them when it agrees with the invoice."""
    _name = 'game.customer'
    _description = "Game world: customer"
    _rec_name = 'partner_id'

    partner_id = fields.Many2one(
        'res.partner', "Contact", required=True, index=True, ondelete='restrict',
        help="The company that buys from the player, and receives its invoices.")
    product_id = fields.Many2one('product.product', "Buys", required=True, ondelete='restrict')
    qty = fields.Float(
        "Quantity per order", digits='Product Unit', required=True, default=1.0,
        help="In the product's own unit of measure.")
    currency_id = fields.Many2one(
        'res.currency', required=True, default=lambda self: self.env.company.currency_id)
    max_price = fields.Monetary(
        "Pays at most, per unit", required=True,
        help="Taxes included: the most this customer will pay for one unit, all in. "
             "An invoice asking more is refused.")
    payment_delay = fields.Float(
        "Pays after (game hours)", required=True, default=0.0,
        help="Game time between receiving an invoice it agrees with and paying it.")
    interval = fields.Float(
        "Orders again after (game hours)", required=True, default=24.0,
        help="Game time between receiving its goods and asking for more.")
    next_order_date = fields.Datetime(
        "Orders next", readonly=True,
        help="When it next asks for goods. Empty: the next time the customer agent runs.")
    order_ids = fields.One2many('game.customer.order', 'customer_id', "Orders")

    _partner_uniq = models.Constraint(
        'UNIQUE(partner_id)',
        "A company is one customer in the world.",
    )
    _qty_positive = models.Constraint('CHECK(qty > 0)', "A customer orders a positive quantity.")
    _max_price_positive = models.Constraint('CHECK(max_price >= 0)', "A price is not negative.")

    @api.model_create_multi
    def create(self, vals_list):
        customers = super().create(vals_list)
        self._trigger_orders()
        return customers

    # -- asking for goods ------------------------------------------------------

    @api.model
    def _trigger_orders(self, at=None):
        cron = self.env.ref('odoo_sim.ir_cron_customer_orders', raise_if_not_found=False)
        if cron:
            cron._trigger(at)

    @api.model
    def _cron_place_orders(self):
        """ The customer agent: every customer with nothing on order asks for goods, once its time has come. """
        now = fields.Datetime.now()
        busy = self.env['game.customer.order']._search([('state', 'in', OPEN_STATES)])
        customers = self.search([
            ('id', 'not in', busy.subselect('customer_id')),
            '|', ('next_order_date', '=', False), ('next_order_date', '<=', now),
        ])
        for customer in customers:
            customer._place_order()

    def _place_order(self):
        """ Ask the company for goods, on this customer's terms. Returns the order, if one was placed. """
        self.ensure_one()
        # Two agents at once must not both find the customer with nothing on order.
        self.env.cr.execute(SQL("SELECT id FROM game_customer WHERE id = %s FOR UPDATE", self.id))
        if self.env['game.customer.order'].search_count(
            [('customer_id', '=', self.id), ('state', 'in', OPEN_STATES)], limit=1,
        ):
            return self.env['game.customer.order']
        order = self.env['game.customer.order'].create({
            'customer_id': self.id,
            'product_id': self.product_id.id,
            'qty': self.qty,
            'currency_id': self.currency_id.id,
            'max_price': self.max_price,
            'date_requested': fields.Datetime.now(),
        })
        product, uom = self.product_id.display_name, self.product_id.uom_id.name
        self._write_to_company(
            self.env._("Order: %(qty)s %(product)s", qty=_qty(self.qty), product=product),
            Markup("<p>%s</p><p>%s</p><p>%s</p>") % (
                self.env._("Hello,"),
                self.env._(
                    "We would like %(qty)s %(uom)s of %(product)s, and can pay up to %(price)s per "
                    "%(uom)s, taxes included. Please send us your invoice: we pay by bank transfer, "
                    "and expect the goods once you have our payment.",
                    qty=_qty(self.qty), uom=uom, product=product,
                    price=format_amount(self.env, self.max_price, self.currency_id),
                ),
                self.partner_id.display_name,
            ),
        )
        self.env['game.world']._changed()
        return order

    def _write_to_company(self, subject, body):
        """ Send the company an email from this customer.

        **The seam for the game's email**, which is being built separately
        (GitHub issue #4).  Everything a customer says goes through here.  Until
        mail between the world and the company exists, it lands on the
        customer's contact in Odoo, as a message from them.  When mail exists,
        only this method changes.
        """
        self.ensure_one()
        self.partner_id.message_post(
            body=body, subject=subject, author_id=self.partner_id.id,
            message_type='email', subtype_xmlid='mail.mt_note',
        )

    # -- reading invoices ------------------------------------------------------

    @api.model
    def _trigger_invoices(self):
        cron = self.env.ref('odoo_sim.ir_cron_customer_invoices', raise_if_not_found=False)
        if cron:
            cron._trigger()

    @api.model
    def _cron_read_invoices(self):
        """ Every customer reads the invoices it has been sent and not read yet.

        Triggered when the player posts a customer invoice (``account.move._post``)
        and not run inline there, so the customer stays an agent reacting to
        what it has been sent.  Today "sent" means "posted in Odoo"; once
        customers read email it will mean the email, and only this trigger
        moves.
        """
        customers = self.search([])
        if not customers:
            return
        read = self.env['game.customer.invoice']._search([('invoice_id', '!=', False)])
        invoices = self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('commercial_partner_id', 'in', customers.partner_id.ids),
            ('id', 'not in', read.subselect('invoice_id')),
        ], order='id')
        for invoice in invoices:
            self._receive_invoice(invoice)

    @api.model
    def _receive_invoice(self, invoice):
        """ The customer ``invoice`` is addressed to reads it, and decides whether to pay.

        **The seam for inbound email**: a customer that receives an invoice by
        mail comes through here.  Idempotent, and does nothing for anything but
        a posted customer invoice addressed to a customer in the world.
        """
        received = self.env['game.customer.invoice']
        if invoice.move_type != 'out_invoice' or invoice.state != 'posted':
            return received
        customer = self.search([('partner_id', '=', invoice.commercial_partner_id.id)], limit=1)
        if not customer or received.search_count([('invoice_id', '=', invoice.id)], limit=1):
            return received
        return customer._read(invoice)

    def _read(self, invoice):
        """ Take in ``invoice`` as it reads now, and decide.

        **A snapshot**, as a shipment is of a purchase order: the amount, the
        quantity of what was ordered (in the product's own unit) and the payment
        reference are copied, and the invoice is never read again.  Resetting it
        to draft and changing it afterwards does not change what the customer
        agreed to pay.
        """
        self.ensure_one()
        product = self.product_id
        qty = sum(
            (line.product_uom_id or product.uom_id)._compute_quantity(line.quantity, product.uom_id)
            for line in invoice.invoice_line_ids if line.product_id == product
        )
        orders = self.order_ids.filtered(lambda o: o.state in OPEN_STATES).sorted('id')
        order = orders.filtered(lambda o: o.state == 'requested')[:1]
        values = {
            'customer_id': self.id,
            'order_id': (order or orders[:1]).id,
            'invoice_id': invoice.id,
            'company_id': invoice.company_id.id,
            'name': invoice.name,
            'reference': invoice.payment_reference or invoice.name,
            'amount': invoice.amount_total,
            'currency_id': invoice.currency_id.id,
            'qty': qty,
            'date_received': fields.Datetime.now(),
        }
        received = self.env['game.customer.invoice']
        if reason := self._objection(order, orders, invoice, qty):
            received = received.create(dict(values, state='refused', reason=reason))
            self._write_to_company(
                self.env._("Re: %s", invoice.name),
                Markup("<p>%s</p><p>%s</p>") % (reason, self.partner_id.display_name),
            )
        else:
            received = received.create(dict(
                values, state='to_pay', date_due=values['date_received'] + timedelta(hours=self.payment_delay),
            ))
            order.state = 'invoiced'
            self.env['game.world']._schedule_settle(received.date_due)
        self.env['game.world']._changed()
        return received

    def _objection(self, order, orders, invoice, qty):
        """ Why this customer will not pay ``invoice`` for ``order``, or None if it will. """
        product = self.product_id
        if not order:
            if orders:
                return self.env._("We already have your invoice for our order, and are not paying twice.")
            return self.env._("We have not ordered anything from you.")
        currency = order.currency_id
        if invoice.currency_id != currency:
            return self.env._("We pay in %s.", currency.name)
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        if float_compare(qty, 0, precision_digits=digits) <= 0:
            return self.env._("This invoice is not for the %s we asked for.", product.display_name)
        if float_compare(qty, order.qty, precision_digits=digits) > 0:
            return self.env._(
                "We asked for %(asked)s %(product)s, not %(invoiced)s.",
                asked=_qty(order.qty), product=product.display_name, invoiced=_qty(qty),
            )
        if currency.compare_amounts(invoice.amount_total, qty * order.max_price) > 0:
            return self.env._(
                "%(amount)s for %(qty)s %(product)s is more than we pay: %(price)s each at most, taxes included.",
                amount=format_amount(self.env, invoice.amount_total, currency), qty=_qty(qty),
                product=product.display_name, price=format_amount(self.env, order.max_price, currency),
            )
        return None


class GameCustomerOrder(models.Model):
    """What a customer asked for, and how far it has got."""
    _name = 'game.customer.order'
    _description = "Game world: customer order"
    _order = 'date_requested, id'

    customer_id = fields.Many2one('game.customer', required=True, readonly=True, index=True, ondelete='restrict')
    partner_id = fields.Many2one(related='customer_id.partner_id')
    product_id = fields.Many2one('product.product', required=True, readonly=True, ondelete='restrict')
    qty = fields.Float("Quantity asked", digits='Product Unit', required=True, readonly=True)
    uom_id = fields.Many2one(related='product_id.uom_id')
    currency_id = fields.Many2one('res.currency', required=True, readonly=True)
    max_price = fields.Monetary(
        "Pays at most, per unit", readonly=True,
        help="The customer's terms when it asked, taxes included.")
    state = fields.Selection([
        ('requested', "Waiting for an invoice"),
        ('invoiced', "Invoice accepted"),
        ('paid', "Paid"),
        ('delivered', "Delivered"),
    ], required=True, readonly=True, default='requested', index=True)
    qty_paid = fields.Float(
        "Quantity paid for", digits='Product Unit', readonly=True,
        help="What the customer paid for, and so what it expects to receive.")
    amount_paid = fields.Monetary(readonly=True)
    date_requested = fields.Datetime(required=True, readonly=True)
    date_paid = fields.Datetime(readonly=True)
    date_delivered = fields.Datetime(readonly=True)
    invoice_ids = fields.One2many('game.customer.invoice', 'order_id', "Invoices received")
    entry_ids = fields.One2many('game.stock.entry', 'customer_order_id', "Ledger")

    def _compute_display_name(self):
        for order in self:
            order.display_name = self.env._(
                "%(customer)s: %(qty)s %(product)s",
                customer=order.partner_id.display_name, qty=_qty(order.qty), product=order.product_id.display_name,
            )

    def _deliver(self):
        """ Ship the order: the goods leave the world, and the customer has them.

        Only once the customer has paid -- its terms, and the company's: an
        order that has not been paid for has nothing to ship yet.  Validating
        the delivery order in Odoo is recording this, and changes nothing here.
        """
        self.ensure_one()
        # A payment may have come due since the last settle.
        self.env['game.world']._settle()
        self.env.cr.execute(SQL("SELECT id FROM game_customer_order WHERE id = %s FOR UPDATE", self.id))
        self.invalidate_recordset(['state'])
        if self.state == 'delivered':
            raise UserError(self.env._("%s has already been delivered.", self.display_name))
        if self.state != 'paid':
            raise UserError(self.env._(
                "%(customer)s has not paid for %(order)s yet.",
                customer=self.partner_id.display_name, order=self.display_name,
            ))

        now = fields.Datetime.now()
        self.env['game.stock']._apply(self.product_id, -self.qty_paid, 'delivered', date=now, customer_order=self)
        self.write({'state': 'delivered', 'date_delivered': now})
        customer = self.customer_id
        customer.next_order_date = now + timedelta(hours=customer.interval)
        customer._trigger_orders(customer.next_order_date)
        self.env['game.world']._changed()


class GameCustomerInvoice(models.Model):
    """An invoice as the customer received it: a copy, and what the customer decided."""
    _name = 'game.customer.invoice'
    _description = "Game world: invoice received by a customer"
    _order = 'date_received desc, id desc'

    customer_id = fields.Many2one('game.customer', required=True, readonly=True, index=True, ondelete='restrict')
    order_id = fields.Many2one(
        'game.customer.order', readonly=True, index='btree_not_null', ondelete='restrict',
        help="The order the customer took this invoice to be for.")
    invoice_id = fields.Many2one(
        'account.move', "Invoice", readonly=True, index='btree_not_null', ondelete='set null',
        help="The invoice this is a copy of. Read once, when it was received, and never again.")
    company_id = fields.Many2one(
        'res.company', required=True, readonly=True,
        help="The company that sent it, and so the one the customer pays.")
    name = fields.Char("Number", readonly=True)
    reference = fields.Char(
        "Payment reference", readonly=True,
        help="What the customer writes on its payment, so the company can tell what it is for.")
    amount = fields.Monetary("Total", readonly=True)
    currency_id = fields.Many2one('res.currency', required=True, readonly=True)
    qty = fields.Float(
        "Quantity invoiced", digits='Product Unit', readonly=True,
        help="Of the product the customer buys, in its own unit of measure.")
    state = fields.Selection([
        ('to_pay', "To pay"),
        ('paid', "Paid"),
        ('refused', "Refused"),
    ], required=True, readonly=True, index=True)
    reason = fields.Char("Why it was refused", readonly=True)
    date_received = fields.Datetime(required=True, readonly=True)
    date_due = fields.Datetime("Pays at", readonly=True, index=True)
    transaction_id = fields.Many2one('game.bank.transaction', "Payment", readonly=True, ondelete='restrict')

    # A customer reads an invoice once, which is what makes reading idempotent.
    _invoice_uniq = models.Constraint(
        'UNIQUE(invoice_id)',
        "A customer reads an invoice once.",
    )

    def _pay(self):
        """ Pay each invoice, now due, as agreed.

        A customer that cannot cover it says so, and waits for another
        invoice: the payment is refused rather than raised, so that one
        customer's empty account does not stop the world settling.
        """
        accounts = self.env['game.bank.account']
        for received in self:
            customer = received.customer_id
            try:
                with self.env.cr.savepoint():
                    transaction = accounts._of(customer.partner_id)._pay(
                        accounts._company_account(received.company_id),
                        received.amount, received.reference, date=received.date_due,
                    )
            except UserError:
                reason = self.env._(
                    "We cannot pay %s at the moment.",
                    format_amount(self.env, received.amount, received.currency_id),
                )
                received.write({'state': 'refused', 'reason': reason})
                received.order_id.state = 'requested'
                customer._write_to_company(
                    self.env._("Re: %s", received.name),
                    Markup("<p>%s</p><p>%s</p>") % (reason, customer.partner_id.display_name),
                )
                continue
            received.write({'state': 'paid', 'transaction_id': transaction.id})
            received.order_id.write({
                'state': 'paid',
                'qty_paid': received.qty,
                'amount_paid': received.amount,
                'date_paid': received.date_due,
            })


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        """ Let the customers know there may be invoices to read. """
        posted = super()._post(soft=soft)
        if any(move.move_type == 'out_invoice' for move in posted):
            self.env['game.customer']._trigger_invoices()
        return posted
