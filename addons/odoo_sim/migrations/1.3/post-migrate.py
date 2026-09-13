# Part of Odoo. See LICENSE file for full copyright and licensing details.
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields


def migrate(cr, version):
    """ 1.3 sends goods by post, and has customers complain when theirs are slow.

    Before it, the player shipped a paid order with a button, which is gone.
    An order already paid for when the world upgrades now waits for a package
    like any other, and its customer's patience runs from the upgrade.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    now = fields.Datetime.now()
    for order in env['game.customer.order'].search([('state', '=', 'paid'), ('date_chase', '=', False)]):
        order.date_chase = now + timedelta(hours=order.customer_id.complain_after)
        env['game.world']._schedule_settle(order.date_chase)
