# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""What exists in the game world: a ledger of real events, and its balance.

Odoo's ``stock.quant`` is what the player *says* is on hand, and nothing stops
the player saying anything.  These two tables are what *is* on hand, and only
the game writes them.  See ``odoo_sim/GAME_STATE.md`` section 3.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, float_is_zero, float_round

#: Why a quantity entered or left the world.  Appended to as domains join the
#: game: a sale delivery will be the first thing that takes goods *out*.
KINDS = [
    ('genesis', "Genesis"),
    ('manufactured', "Manufactured"),
    ('consumed', "Consumed"),
    ('received', "Received"),
]


class GameStock(models.Model):
    """How much of each product physically exists in the world.

    A cache of the ledger's sum, kept for one reason: ``CHECK (qty >= 0)`` on a
    single row is what makes "you cannot consume what is not there" hold under
    concurrency.  Two players pressing a button at once both try to take the
    last metre of wire; the database lets exactly one of them.
    """
    _name = 'game.stock'
    _description = "Game world: on hand"
    _order = 'product_id'
    _rec_name = 'product_id'

    product_id = fields.Many2one(
        'product.product', required=True, readonly=True, index=True, ondelete='restrict')
    # digits makes this NUMERIC rather than float8 (fields_numeric.py), so a
    # thousand paperclips at 0.1 m each add up to exactly 100 m.
    qty = fields.Float("Quantity", digits='Product Unit', required=True, readonly=True)
    uom_id = fields.Many2one(related='product_id.uom_id')

    _product_uniq = models.Constraint(
        'UNIQUE(product_id)',
        "A product has one balance in the world.",
    )
    _qty_positive = models.Constraint(
        'CHECK(qty >= 0)',
        "Nothing can exist in a negative quantity.",
    )

    @api.model
    def _apply(self, product, qty, kind, *, date=None, production=None, shipment=None):
        """ Change how much of ``product`` exists by ``qty``, and record why.

        **The only write path into reality.**  Everything that makes goods
        appear or disappear in the world comes through here, so the ledger and
        the balance cannot disagree: both are written in the caller's
        transaction and commit or roll back together.

        ``qty`` is signed, in the product's own unit.  ``date`` is the game
        instant the event happened, which is not always now -- a production run
        that finished at 14:00 and was settled by a cron at 14:07 happened at
        14:00.  ``production`` / ``shipment`` name the world event responsible.

        Raises ``UserError`` rather than taking more than exists.
        """
        product.ensure_one()
        digits = self.env['decimal.precision'].precision_get('Product Unit')
        qty = float_round(qty, precision_digits=digits)
        if float_is_zero(qty, precision_digits=digits):
            return self.env['game.stock.entry']

        now = fields.Datetime.now()
        self.flush_model()
        values = {'product': product.id, 'qty': qty, 'uid': self.env.uid, 'now': now}
        if qty > 0:
            self.env.cr.execute(SQL("""
                INSERT INTO game_stock (product_id, qty, create_uid, create_date, write_uid, write_date)
                     VALUES (%(product)s, %(qty)s, %(uid)s, %(now)s, %(uid)s, %(now)s)
                ON CONFLICT (product_id) DO UPDATE
                        SET qty = game_stock.qty + EXCLUDED.qty,
                            write_uid = EXCLUDED.write_uid,
                            write_date = EXCLUDED.write_date
            """, **values))
        else:
            # The WHERE clause is the physics: no row changes if the world does
            # not hold enough, and the CHECK constraint backs it up.
            self.env.cr.execute(SQL("""
                UPDATE game_stock
                   SET qty = qty + %(qty)s, write_uid = %(uid)s, write_date = %(now)s
                 WHERE product_id = %(product)s AND qty + %(qty)s >= 0
            """, **values))
            if not self.env.cr.rowcount:
                self.invalidate_model(['qty'])
                have = self.search([('product_id', '=', product.id)]).qty
                raise UserError(self.env._(
                    "There is not enough %(product)s in the world: %(needed)s %(uom)s needed, "
                    "%(have)s %(uom)s on hand.",
                    product=product.display_name, needed=-qty, have=have, uom=product.uom_id.name,
                ))
        self.invalidate_model(['qty', 'write_uid', 'write_date'])

        return self.env['game.stock.entry'].create({
            'date': date or now,
            'product_id': product.id,
            'qty': qty,
            'kind': kind,
            'production_id': production.id if production else False,
            'shipment_id': shipment.id if shipment else False,
        })


class GameStockEntry(models.Model):
    """One real-world event that changed what exists.  Never edited, never deleted."""
    _name = 'game.stock.entry'
    _description = "Game world: ledger entry"
    _order = 'date desc, id desc'
    _rec_name = 'product_id'

    date = fields.Datetime(required=True, readonly=True, index=True, default=fields.Datetime.now)
    product_id = fields.Many2one(
        'product.product', required=True, readonly=True, index=True, ondelete='restrict')
    qty = fields.Float("Quantity", digits='Product Unit', required=True, readonly=True)
    uom_id = fields.Many2one(related='product_id.uom_id')
    kind = fields.Selection(KINDS, required=True, readonly=True)
    production_id = fields.Many2one(
        'game.production', "Production run", readonly=True, index='btree_not_null', ondelete='restrict')
    shipment_id = fields.Many2one(
        'game.shipment', "Shipment", readonly=True, index='btree_not_null', ondelete='restrict')
