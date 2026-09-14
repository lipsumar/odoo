# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import models


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _action_confirm(self, merge=True, merge_into=False, create_proc=True):
        """ Let the employees know Odoo may have something new for them. """
        moves = super()._action_confirm(merge=merge, merge_into=merge_into, create_proc=create_proc)
        self.env['game.employee']._trigger_work()
        return moves

    def _action_assign(self, force_qty=False):
        """ A reservation may have made a transfer or a manufacturing order ready: see ``_action_confirm``.

        Employees read Odoo themselves, as they read it for any other work, so
        this only wakes them and says nothing about what.
        """
        result = super()._action_assign(force_qty=force_qty)
        self.env['game.employee']._trigger_work()
        return result
