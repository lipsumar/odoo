# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""No real email comes into a world.

The mirror of ``ir_mail_server.py``: a world's incoming mail is what its own
post office hands Odoo's gateway (``game.email.delivery._hand_to_odoo``), so an
incoming mail server configured on the database -- say, on a copy of a real
company's -- must not pour real mail into it.  See ``odoo_sim/MAIL.md``.
"""
from odoo import models
from odoo.exceptions import UserError

from odoo.addons.odoo_sim.models.game_mail import is_world


class FetchmailServer(models.Model):
    _inherit = 'fetchmail.server'

    def _world_refusal(self):
        return UserError(self.env._(
            "%s is a game world: no real email comes into it. Its mail is the game's own.",
            self.env.cr.dbname,
        ))

    def _connect__(self, allow_archived=False):  # noqa: PLW3201
        if is_world(self.env.cr):
            raise self._world_refusal()
        return super()._connect__(allow_archived=allow_archived)

    def _fetch_mail(self, batch_limit=50):
        """ Fetch nothing.  The refusal is returned, not raised, as upstream
        returns its failures: the button raises it, the cron lets it go. """
        if is_world(self.env.cr):
            return self._world_refusal()
        return super()._fetch_mail(batch_limit=batch_limit)
