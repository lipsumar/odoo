# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The post for goods: packages the player packs, addresses and sends.

The player takes a box, puts goods in it -- they leave the shelves -- writes an
address on it by hand, and hands it to the post.  The post carries it for a
while, then looks for someone living at that address: a customer has its
goods, and a box addressed to nobody comes back to the company, still full.
The post knows nothing of orders, invoices or payments, and neither does the
box.  See ``odoo_sim/GAME_STATE.md`` section 7.4.
"""
import re
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, float_is_zero, float_round

#: Game hours the post takes to carry a package, and to carry one back.
TRANSIT_HOURS = 24.0

#: A package the player has handed to the post, wherever it is now.  The page
#: does not say which: the post has no tracking, and a customer that has not
#: had its goods says so by email.
SENT_STATES = ('in_transit', 'delivered', 'returning')


def address_words(text):
    """ The words of a postal address, as the post reads it.

    Case, punctuation and line breaks do not matter; every word, and their
    order, does: ``"Binder & Co., 12 Clip Lane"`` is ``"BINDER & CO.\\n12 clip
    lane"``, and ``"12 Clip Ln"`` is somewhere else.
    """
    return ' '.join(re.findall(r'[^\W_]+', (text or '').casefold()))


def _qty(value):
    return f"{value:g}"


class GamePackage(models.Model):
    """A box: goods packed at the company, and the address written on it."""
    _name = 'game.package'
    _description = "Game world: package"
    _order = 'id desc'

    state = fields.Selection([
        ('open', "On the bench"),
        ('in_transit', "In the post"),
        ('delivered', "Delivered"),
        ('returning', "Coming back"),
        ('unpacked', "Unpacked"),
    ], required=True, readonly=True, default='open', index=True)
    address = fields.Text(
        "Addressed to", readonly=True,
        help="What the player wrote on the box when sending it, as they wrote it.")
    line_ids = fields.One2many('game.package.line', 'package_id', "Contents", readonly=True)
    date_posted = fields.Datetime("Sent", readonly=True)
    date_arrival = fields.Datetime(
        "Arrives", readonly=True, index=True,
        help="When the post reaches the address; coming back, when it reaches the company.")
    customer_id = fields.Many2one(
        'game.customer', "Delivered to", readonly=True, index='btree_not_null', ondelete='restrict')
    order_id = fields.Many2one(
        'game.customer.order', "Counted toward", readonly=True, index='btree_not_null', ondelete='restrict',
        help="The order the customer took these goods to be for.")
    date_delivered = fields.Datetime(readonly=True)
    return_reason = fields.Char("Returned because", readonly=True)
    date_returned = fields.Datetime(readonly=True)
    entry_ids = fields.One2many('game.stock.entry', 'package_id', "Ledger")

    def _compute_display_name(self):
        for package in self:
            package.display_name = self.env._("Package %s", package.id)

    # -- at the company: the player's actions ----------------------------------

    @api.model
    def _new(self):
        """ Take an empty box. """
        package = self.create({})
        self.env['game.world']._changed()
        return package

    def _on_the_bench(self):
        """ Lock the box, and refuse unless it is still at the company. """
        self.ensure_one()
        # A box that came back since the last settle is on the bench again.
        self.env['game.world']._settle()
        self.env.cr.execute(SQL("SELECT id FROM game_package WHERE id = %s FOR UPDATE", self.id))
        self.invalidate_recordset(['state'])
        if self.state == 'unpacked':
            raise UserError(self.env._("%s has been unpacked.", self.display_name))
        if self.state != 'open':
            raise UserError(self.env._("%s has already been sent.", self.display_name))

    def _pack(self, product, qty):
        """ Put ``qty`` of ``product``, in its own unit, in the box: it leaves the shelves. """
        self.ensure_one()
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        qty = float_round(qty, precision_digits=digits)
        if float_is_zero(qty, precision_digits=digits) or qty < 0:
            raise UserError(self.env._("Put a positive quantity in the package."))
        self._on_the_bench()

        self.env['game.stock']._apply(product, -qty, 'packed', package=self)
        line = self.line_ids.filtered(lambda line: line.product_id == product)
        if line:
            line.qty += qty
        else:
            self.env['game.package.line'].create({'package_id': self.id, 'product_id': product.id, 'qty': qty})
        self.env['game.world']._changed()

    def _unpack(self):
        """ Put everything in the box back on the shelves, and the box away. """
        self._on_the_bench()
        stock = self.env['game.stock']
        for line in self.line_ids:
            stock._apply(line.product_id, line.qty, 'unpacked', package=self)
        self.state = 'unpacked'
        self.env['game.world']._changed()

    def _send(self, address):
        """ Write ``address`` on the box and hand it to the post.

        The address is not checked: the post office takes whatever is written
        on a box, and finds out when it gets there.
        """
        self._on_the_bench()
        address = '\n'.join(line.strip() for line in (address or '').strip().splitlines())
        if not address_words(address):
            raise UserError(self.env._("Write an address on %s before sending it.", self.display_name))
        if not self.line_ids:
            raise UserError(self.env._("%s is empty: there is nothing to send.", self.display_name))

        now = fields.Datetime.now()
        self.write({
            'state': 'in_transit',
            'address': address,
            'date_posted': now,
            'date_arrival': now + timedelta(hours=TRANSIT_HOURS),
            'return_reason': False,
            'date_returned': False,
        })
        self.env['game.world']._schedule_settle(self.date_arrival)
        self.env['game.world']._changed()

    # -- in the post: settled when due (game.world._settle) ---------------------

    def _arrive(self):
        """ The post reaches each package's address, as of its arrival time.

        Whoever lives there has the package.  A box addressed to nobody sets off
        back to the company.
        """
        residents = {}
        for customer in self.env['game.customer'].search([('address', '!=', False)], order='id'):
            residents.setdefault(address_words(customer.address), customer)
        residents.pop('', None)
        for package in self:
            customer = residents.get(address_words(package.address))
            if customer:
                package.write({
                    'state': 'delivered', 'customer_id': customer.id, 'date_delivered': package.date_arrival,
                })
                customer._receive_package(package)
            else:
                package.write({
                    'state': 'returning',
                    'date_arrival': package.date_arrival + timedelta(hours=TRANSIT_HOURS),
                    'return_reason': self.env._("Nobody lives at this address."),
                })
                self.env['game.world']._schedule_settle(package.date_arrival)

    def _come_back(self):
        """ Returned to sender: the box is on the bench again, still full, to re-address or unpack. """
        for package in self:
            package.write({'state': 'open', 'date_returned': package.date_arrival})


class GamePackageLine(models.Model):
    _name = 'game.package.line'
    _description = "Game world: package contents"
    _rec_name = 'product_id'

    package_id = fields.Many2one('game.package', required=True, index=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', required=True, readonly=True, ondelete='restrict')
    qty = fields.Float(
        "Quantity", digits='Product Unit', required=True, readonly=True,
        help="In the product's own unit of measure.")
    uom_id = fields.Many2one(related='product_id.uom_id')

    def _compute_display_name(self):
        for line in self:
            line.display_name = f"{_qty(line.qty)} {line.uom_id.name} {line.product_id.display_name}"
