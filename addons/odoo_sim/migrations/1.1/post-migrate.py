# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """ 1.1 brought the game world's stock: databases upgrading into it get the
    genesis a fresh install gets from its post_init_hook. """
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['game.world']._genesis()
