# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    """ 1.2 brought the game bank: databases upgrading into it get the company
    account a fresh install opens in its post_init_hook, empty. """
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['game.bank.account']._company_account()
