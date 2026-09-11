# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Workstations, and the production runs that make real goods on them.

Pressing a workstation's button is the one way a manufactured product comes
into existence.  Marking a manufacturing order done in Odoo is the player
*recording* that it happened, and the world does not read it.  See
``odoo_sim/GAME_STATE.md`` section 5.
"""
from datetime import timedelta

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL


class GameWorkstation(models.Model):
    _name = 'game.workstation'
    _description = "Game world: workstation"

    name = fields.Char(required=True)
    recipe_id = fields.Many2one('game.recipe', "Makes", required=True, ondelete='restrict')
    product_id = fields.Many2one(related='recipe_id.product_id', string="Product")
    workcenter_id = fields.Many2one(
        'mrp.workcenter', "Odoo work center", ondelete='set null',
        help="The work center that models this station in Odoo. Informational: the world never reads it.")
    production_ids = fields.One2many('game.production', 'workstation_id', "Runs")

    def _start(self, qty=1.0, mrp_production=None):
        """ Start a run of ``qty`` units: take the components now, make the goods later.

        Components leave the world when the run starts, because that is when
        they are taken off the shelf.  The product enters it when the run
        finishes, at ``date_end``, settled by :meth:`game.world._settle`.

        One run at a time: a workstation is a physical thing.

        ``mrp_production`` links the run to the manufacturing order the player
        says it is for.  It is informational only -- nothing in the world ever
        reads the order back, so an order edited or cancelled afterwards
        changes nothing here.
        """
        self.ensure_one()
        if qty <= 0:
            raise UserError(self.env._("A run makes a positive quantity."))
        if mrp_production and mrp_production.product_id != self.product_id:
            raise UserError(self.env._(
                "%(order)s is for %(product)s, but %(station)s makes %(made)s.",
                order=mrp_production.name, product=mrp_production.product_id.display_name,
                station=self.name, made=self.product_id.display_name,
            ))

        # A run due to end may be the one keeping this station busy.
        self.env['game.world']._settle()
        # Two presses at once must not both find the station free.
        self.env.cr.execute(SQL("SELECT id FROM game_workstation WHERE id = %s FOR UPDATE", self.id))
        if self.env['game.production'].search_count(
            [('workstation_id', '=', self.id), ('state', '=', 'running')], limit=1,
        ):
            raise UserError(self.env._("%s is already running.", self.name))

        now = fields.Datetime.now()
        run = self.env['game.production'].create({
            'workstation_id': self.id,
            'recipe_id': self.recipe_id.id,
            'qty': qty,
            'date_start': now,
            'date_end': now + timedelta(minutes=self.recipe_id.duration * qty),
            'production_id': mrp_production.id if mrp_production else False,
        })
        stock = self.env['game.stock']
        for line in self.recipe_id.line_ids:
            stock._apply(line.product_id, -line.qty * qty, 'consumed', date=now, production=run)

        self.env['game.world']._schedule_settle(run.date_end)
        self.env['game.world']._changed()
        return run


class GameProduction(models.Model):
    _name = 'game.production'
    _description = "Game world: production run"
    _order = 'date_start desc, id desc'

    workstation_id = fields.Many2one(
        'game.workstation', required=True, readonly=True, index=True, ondelete='restrict')
    recipe_id = fields.Many2one('game.recipe', required=True, readonly=True, ondelete='restrict')
    product_id = fields.Many2one(related='recipe_id.product_id', store=True)
    qty = fields.Float("Quantity", digits='Product Unit', required=True, readonly=True)
    uom_id = fields.Many2one(related='product_id.uom_id')
    state = fields.Selection(
        [('running', "Running"), ('done', "Done")],
        required=True, readonly=True, default='running', index=True)
    date_start = fields.Datetime(required=True, readonly=True)
    date_end = fields.Datetime(
        required=True, readonly=True, index=True,
        help="Game time at which the goods exist.")
    production_id = fields.Many2one(
        'mrp.production', "Manufacturing order", readonly=True, ondelete='set null',
        help="The order the player said this run was for. Informational: the world never reads it.")
    entry_ids = fields.One2many('game.stock.entry', 'production_id', "Ledger")

    _qty_positive = models.Constraint('CHECK(qty > 0)', "A run makes a positive quantity.")

    def _finish(self):
        """ Put the finished goods into the world, as of when the run ended. """
        stock = self.env['game.stock']
        for run in self:
            stock._apply(run.product_id, run.qty, 'manufactured', date=run.date_end, production=run)
        self.state = 'done'
