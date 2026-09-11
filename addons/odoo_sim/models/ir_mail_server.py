# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Odoo's outgoing mail, posted into the world instead of sent.

Everything Odoo emails -- notifications, templates, "Send by Email", bounces
-- goes through ``send_email``, and ``mail.mail`` opens its session with
``_connect__`` first.  In a game world both stop here: no SMTP connection is
opened, and the message is put in the world's post (``game.email._post``),
whose post office delivers it.  See ``odoo_sim/MAIL.md``.

Core has a backstop of its own in ``_connect__``, for a world that does not
have this module installed.
"""
from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.odoo_sim.models.game_mail import is_world


class WorldPost:
    """What ``_connect__`` hands out in a world, where an SMTP session would be.

    Its callers only pass it back into ``send_email`` and ``quit()`` it
    (``mail_mail.send``).  ``from_filter`` and ``smtp_from`` are what
    ``_prepare_email_message__`` would ask a session for, and a world's post
    has neither: it relays anything, from anyone.
    """
    from_filter = False
    smtp_from = False

    def quit(self):
        pass


class IrMailServer(models.Model):
    _inherit = 'ir.mail_server'

    def _connect__(self, *args, **kwargs):  # noqa: PLW3201
        if is_world(self.env.cr):
            return WorldPost()
        return super()._connect__(*args, **kwargs)

    @api.model
    def send_email(self, message, *args, **kwargs):
        """ In a world, post ``message`` for its envelope's recipients.

        Returns the Message-Id, as a successful send does.  The recipients are
        what SMTP would have been given (``_prepare_smtp_to_list``, including
        the restrictions ``mail.mail`` puts in the context), and the message is
        altered as it would have been on its way out (``_alter_message__``:
        forged To headers applied, Bcc and internal headers removed).
        """
        if not is_world(self.env.cr):
            return super().send_email(message, *args, **kwargs)
        recipients = self._prepare_smtp_to_list(message, None)
        if not recipients:
            # What mail.mail expects when nobody valid is left (see core's
            # _prepare_email_message__).
            raise AssertionError(self.NO_VALID_RECIPIENT)
        self._alter_message__(message, message['From'], recipients)
        return self.env['game.email'].sudo()._post(message, recipients, origin='odoo').message_id

    def test_smtp_connection(self, autodetect_max_email_size=False):
        if is_world(self.env.cr):
            raise UserError(self.env._(
                "%s is a game world: its mail is delivered by the game, never by a mail server.",
                self.env.cr.dbname,
            ))
        return super().test_smtp_connection(autodetect_max_email_size=autodetect_max_email_size)
