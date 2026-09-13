# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Customers outside the company: they ask for goods, pay for them, and receive them.

A customer is an agent, like a vendor.  It asks for goods by emailing the
company, reads the invoices the company emails it, pays the ones it agrees with
through the game bank, has its goods when the post brings a package to its
address (``game_post.py``), and writes to complain when they are slow to come.
Its mail is the world's (``odoo_sim/MAIL.md``): it writes through
``game.email._send_from`` and reads what the post office delivers to it.
Confirming a sale order, posting an invoice, registering a payment and
validating a delivery in Odoo are the player *recording* all of this; the world
reads an invoice once, when the customer receives it, and never again.  See
``odoo_sim/GAME_STATE.md`` section 7.
"""
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, email_normalize, float_compare, float_is_zero, format_amount

_logger = logging.getLogger(__name__)

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
    write_to = fields.Char(
        "Writes to",
        help="The address this customer has for the company: where it sends its orders. "
             "Its replies go where the email they answer asked.")
    address = fields.Text(
        "Postal address",
        help="Where the post brings its goods, and what it tells the company when it orders. "
             "The world's own: the contact's address in Odoo is the player's copy of it.")
    complain_after = fields.Float(
        "Complains after (game hours)", required=True, default=72.0,
        help="How long after paying, and after each complaint, it waits for its goods "
             "before writing to ask where they are.")
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
    # Zero would have it complain again at the very instant it complained.
    _complain_after_positive = models.Constraint(
        'CHECK(complain_after > 0)', "A customer waits a while before complaining.")

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
        order.email_id = self._write_to_company(
            self.env._("Order: %(qty)s %(product)s", qty=_qty(self.qty), product=product),
            Markup("<p>%s</p><p>%s</p>%s<p>%s</p>") % (
                self.env._("Hello,"),
                self.env._(
                    "We would like %(qty)s %(uom)s of %(product)s, and can pay up to %(price)s per "
                    "%(uom)s, taxes included. Please send us your invoice: we pay by bank transfer, "
                    "and expect the goods once you have our payment.",
                    qty=_qty(self.qty), uom=uom, product=product,
                    price=format_amount(self.env, self.max_price, self.currency_id),
                ),
                self._ship_to(),
                self.partner_id.display_name,
            ),
        )
        self.env['game.world']._changed()
        return order

    def _ship_to(self):
        """ The paragraph of an email saying where to send the goods; empty without an address. """
        self.ensure_one()
        if not self.address:
            return Markup()
        return Markup("<p>%s<br/>%s</p>") % (
            self.env._("Please send them by post to:"),
            Markup("<br/>").join(self.address.splitlines()),
        )

    def _write_to_company(self, subject, body, parent=None):
        """ Email the company, from this customer.  Everything a customer says goes through here.

        A reply goes where the email it answers asked -- its Reply-To, else its
        sender -- threaded under it, so that Odoo files it with the invoice it
        answers.  A follow-up to one of its own emails goes where that one
        went, threaded under it too.  Anything else goes to ``write_to``.

        Nowhere to write is logged rather than raised: a customer that cannot
        reach the company still orders and pays, and the page shows its order.
        """
        self.ensure_one()
        if parent and email_normalize(parent.email_from or '') in self._mailboxes():
            to = parent.email_to
        elif parent:
            to = parent.reply_to or parent.email_from
        else:
            to = self.write_to
        if to:
            try:
                with self.env.cr.savepoint():
                    return self.env['game.email']._send_from(self.partner_id, to, subject, body, parent=parent)
            except UserError as error:
                _logger.warning("%s could not write to %s: %s", self.partner_id.display_name, to, error)
        else:
            _logger.warning("%s has no address for the company, and did not send %r",
                            self.partner_id.display_name, subject)
        return self.env['game.email']

    @api.model
    def _introduce(self, customer_id, user_id, address):
        """ Have ``customer_id`` write to ``user_id``, giving them ``address`` if they have none.

        For scenario data, whose ``<function>`` runs on every install and
        upgrade: it only fills in what is blank, so a player's own address and
        a customer's own contact are never overwritten.
        """
        customer, user = self.browse(customer_id), self.env['res.users'].browse(user_id)
        if not user.email:
            user.email = address
        if not customer.write_to:
            customer.write_to = user.email

    @api.model
    def _give_address(self, customer_id, address):
        """ Give ``customer_id`` the postal ``address`` if it has none: for scenario data, as ``_introduce``. """
        customer = self.browse(customer_id)
        if not customer.address:
            customer.address = address

    # -- receiving goods -------------------------------------------------------

    def _receive_package(self, package):
        """ The post has brought ``package`` here: this customer keeps everything in it.

        What it buys counts toward its open order, paid for or not -- shipping
        before the payment is in is the company's risk to take -- and the order
        is done once the customer has both paid and received what it paid for
        (``_complete``).  Anything else in the box, or goods arriving with no
        order open, it simply keeps.
        """
        self.ensure_one()
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        qty = sum(package.line_ids.filtered(lambda line: line.product_id == self.product_id).mapped('qty'))
        order = self.order_ids.filtered(lambda o: o.state in OPEN_STATES)[:1]
        if order and not float_is_zero(qty, precision_digits=digits):
            package.order_id = order
            order.qty_received += qty
            order._complete(package.date_arrival)
        self.env['game.world']._changed()

    # -- reading invoices ------------------------------------------------------

    @api.model
    def _trigger_mail(self):
        cron = self.env.ref('odoo_sim.ir_cron_customer_mail', raise_if_not_found=False)
        if cron:
            cron._trigger()

    def _mailboxes(self):
        """ ``{address: customer}``: the customer's own address, and its contacts'. """
        return {
            partner.email_normalized: customer
            for customer in self
            for partner in customer.partner_id | customer.partner_id.child_ids
            if partner.email_normalized
        }

    @api.model
    def _cron_read_mail(self):
        """ Every customer reads the mail that has reached it, and acts on the invoices.

        Woken by the post office whenever it delivers outside the company
        (``game.email.delivery._received``), rather than acting inside the
        post office's run, so a customer stays an agent reading its mail.  An
        email is read once: the post office's ``is_read`` says so.
        """
        mailboxes = self.search([])._mailboxes()
        if not mailboxes:
            return
        deliveries = self.env['game.email.delivery'].search([
            ('route', '=', 'outside'),
            ('state', '=', 'delivered'),
            ('is_read', '=', False),
            ('address', 'in', list(mailboxes)),
        ], order='id')
        for delivery in deliveries:
            mailboxes[delivery.address]._read_mail(delivery.email_id)
            delivery.is_read = True

    def _read_mail(self, email):
        """ Read one email.  Only an invoice means anything to a customer, so far. """
        self.ensure_one()
        if email.res_model != 'account.move':
            return
        invoice = self.env['account.move'].browse(email.res_id).exists()
        if invoice and invoice.commercial_partner_id == self.partner_id.commercial_partner_id:
            self._receive_invoice(invoice, email)

    @api.model
    def _receive_invoice(self, invoice, email=None):
        """ The customer ``invoice`` is addressed to reads it, and decides whether to pay.

        ``email`` is the one it came with, which a refusal answers.
        Idempotent, and does nothing for anything but a posted customer
        invoice addressed to a customer in the world.
        """
        received = self.env['game.customer.invoice']
        if invoice.move_type != 'out_invoice' or invoice.state != 'posted':
            return received
        customer = self.search([('partner_id', '=', invoice.commercial_partner_id.id)], limit=1)
        if not customer or received.search_count([('invoice_id', '=', invoice.id)], limit=1):
            return received
        return customer._read(invoice, email)

    def _read(self, invoice, email=None):
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
            'email_id': email.id if email else False,
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
                parent=email,
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
        ('delivered', "Received"),
    ], required=True, readonly=True, default='requested', index=True)
    qty_paid = fields.Float(
        "Quantity paid for", digits='Product Unit', readonly=True,
        help="What the customer paid for, and so what it expects to receive.")
    amount_paid = fields.Monetary(readonly=True)
    qty_received = fields.Float(
        "Quantity received", digits='Product Unit', readonly=True,
        help="What the post brought the customer while this order was open.")
    date_requested = fields.Datetime(required=True, readonly=True)
    date_paid = fields.Datetime(readonly=True)
    date_delivered = fields.Datetime("Date received", readonly=True)
    date_chase = fields.Datetime(
        "Complains at", readonly=True, index=True,
        help="When the customer, paid up and still without its goods, next writes to ask for them.")
    complaint_count = fields.Integer("Complaints", readonly=True)
    email_id = fields.Many2one(
        'game.email', "Asked in", readonly=True, ondelete='set null',
        help="The email the customer ordered with. Its complaints follow it up.")
    invoice_ids = fields.One2many('game.customer.invoice', 'order_id', "Invoices received")
    package_ids = fields.One2many('game.package', 'order_id', "Packages received")
    # Shipped straight to the customer, before goods went by post (1.2).
    entry_ids = fields.One2many('game.stock.entry', 'customer_order_id', "Ledger")

    def _compute_display_name(self):
        for order in self:
            order.display_name = self.env._(
                "%(customer)s: %(qty)s %(product)s",
                customer=order.partner_id.display_name, qty=_qty(order.qty), product=order.product_id.display_name,
            )

    def _complete(self, at):
        """ Close each order whose customer has paid and has what it paid for, as of ``at``.

        Called when a payment goes through and when a package arrives, since
        either can come first.  The customer orders again ``interval`` later.
        """
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        for order in self:
            if order.state != 'paid' or float_compare(
                order.qty_received, order.qty_paid, precision_digits=digits,
            ) < 0:
                continue
            order.write({'state': 'delivered', 'date_delivered': at, 'date_chase': False})
            customer = order.customer_id
            customer.next_order_date = at + timedelta(hours=customer.interval)
            customer._trigger_orders(customer.next_order_date)

    def _complain(self):
        """ Paid, and still without its goods: the customer writes to ask for them, and waits again.

        Due at ``date_chase`` (settled by ``game.world._settle``), and again
        every ``complain_after`` until the goods come.  A follow-up to the email
        it ordered with, so it lands on that thread.
        """
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        for order in self:
            customer = order.customer_id
            paid = order.invoice_ids.filtered(lambda received: received.state == 'paid')[:1]
            values = {
                'amount': format_amount(self.env, order.amount_paid, order.currency_id),
                'qty': _qty(order.qty_paid),
                'product': order.product_id.display_name,
                'reference': paid.reference or paid.name or '',
            }
            if float_is_zero(order.qty_received, precision_digits=digits):
                text = self.env._(
                    "We paid %(amount)s for %(qty)s %(product)s (%(reference)s), and have not received them.",
                    **values)
            else:
                text = self.env._(
                    "We paid %(amount)s for %(qty)s %(product)s (%(reference)s), and have received only %(received)s.",
                    received=_qty(order.qty_received), **values)
            customer._write_to_company(
                self.env._("Where are our %(qty)s %(product)s?", qty=values['qty'], product=values['product']),
                Markup("<p>%s</p><p>%s</p>%s<p>%s</p>") % (
                    self.env._("Hello,"), text, customer._ship_to(), customer.partner_id.display_name,
                ),
                parent=order.email_id,
            )
            order.write({
                'complaint_count': order.complaint_count + 1,
                'date_chase': order.date_chase + timedelta(hours=customer.complain_after),
            })
            self.env['game.world']._schedule_settle(order.date_chase)
        if self:
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
    email_id = fields.Many2one(
        'game.email', "Arrived with", readonly=True, ondelete='set null',
        help="The email that brought it. A refusal answers it.")

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
                    parent=received.email_id,
                )
                continue
            received.write({'state': 'paid', 'transaction_id': transaction.id})
            order = received.order_id
            order.write({
                'state': 'paid',
                'qty_paid': received.qty,
                'amount_paid': received.amount,
                'date_paid': received.date_due,
                'date_chase': received.date_due + timedelta(hours=customer.complain_after),
            })
            # The goods may have come before the money went.
            order._complete(received.date_due)
            if order.state == 'paid':
                self.env['game.world']._schedule_settle(order.date_chase)


class GameEmailDelivery(models.Model):
    _inherit = 'game.email.delivery'

    def _received(self):
        """ Mail has reached mailboxes outside the company: some may be customers'. """
        super()._received()
        if self:
            self.env['game.customer']._trigger_mail()
