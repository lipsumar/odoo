# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The player's mail on the game page: open it, and write.

Guarded like the world's other endpoints (``controllers/world.py``): employees
only, and sending only while time moves, since mail posted into a world that
nothing ticks would sit in transit -- the post office is a cron.  An email is
shown only to someone who received it or wrote it, and is a 404 to anyone
else rather than a refusal that admits it exists.  See ``odoo_sim/MAIL.md``.
"""
import werkzeug.exceptions

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools.mail import plaintext2html

from odoo.addons.odoo_sim.controllers.world import _acting, _existing, _world


def _visible(world, email_id):
    """ The email ``email_id``, if the player asking may read it. """
    email = _existing(world, 'game.email', email_id)
    if not email._visible_to(request.env.user):
        raise werkzeug.exceptions.NotFound()
    return email


class GameMail(http.Controller):

    @http.route('/game/api/mail/<int:email_id>', type='json2', auth='user', methods=['GET'])
    def email(self, email_id):
        """ One email in full: headers, body, attachments, and what a reply would say. """
        return _visible(_world(), email_id)._content()

    @http.route('/game/api/mail/<int:email_id>/read', type='json2', auth='user', methods=['POST'])
    def mark_read(self, email_id):
        """ Mark an email read.  Not an event in the world, so allowed while time stands still. """
        world = _world()
        _visible(world, email_id)._mark_read_by(request.env.user)
        return world._snapshot(request.env.user)

    @http.route('/game/api/mail/send', type='json2', auth='user', methods=['POST'])
    def send(self, to='', cc='', subject='', body='', parent_id=None):
        """ Send an email as the player.  ``body`` is plain text, as typed;
        ``parent_id`` is the email it answers, if any. """
        world = _acting()
        if not all(isinstance(value, str) for value in (to, cc, subject, body)):
            raise UserError(world.env._("An email is made of text."))
        if parent_id is not None and (isinstance(parent_id, bool) or not isinstance(parent_id, int)):
            raise UserError(world.env._("A reply answers an email, named by its id."))
        parent = _visible(world, parent_id) if parent_id else None
        user = request.env.user
        world.env['game.email']._send_from(
            user.partner_id, to, subject, plaintext2html(body) if body else '',
            cc=cc, parent=parent, author=user,
        )
        return world._snapshot(user)
