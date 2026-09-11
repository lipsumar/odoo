# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The game world's HTTP surface: read it, and act in it.

Every action is a write into reality, so every action is guarded twice (see
``odoo_sim/GAME_STATE.md`` section 8):

* internal users only -- ``auth='user'`` alone would admit portal users;
* ``game_clock.is_running``, because a dead or paused world would accept a run
  and then never finish it (DESIGN.md 3.3).

The ``game.*`` models grant nobody write access, so the endpoints act through
``sudo()``: *these* routes are the permission, not an access rule.
"""
from __future__ import annotations

import werkzeug.exceptions

from odoo import game_clock, http
from odoo.exceptions import UserError
from odoo.http import request


def _world():
    """ The world as the game sees it: privileged, after checking who is asking. """
    if not request.env.user._is_internal():
        raise werkzeug.exceptions.Forbidden("The game world is for employees of the company.")
    return request.env['game.world'].sudo()


def _acting():
    """ Return the world for an action, or refuse if time is not moving. """
    world = _world()
    if not game_clock.is_running(game_clock.clock_for(request.db, request.env.cr)):
        raise UserError(world.env._("The world is not running: nothing can happen until time moves again."))
    return world


def _existing(world, model, record_id):
    record = world.env[model].browse(record_id).exists()
    if not record:
        raise werkzeug.exceptions.NotFound()
    return record


class GameWorld(http.Controller):

    @http.route('/game/api/world', type='json2', auth='user', methods=['GET'])
    def world(self):
        """ The whole world, as the page shows it (``game.world._snapshot``).

        Deliberately a pure read: it does not settle.  A run whose end has
        passed but which the cron has not yet finished is shown as running,
        and the page draws it as finishing.  Actions settle before they act,
        so none of them ever works from that stale view.
        """
        return _world()._snapshot()

    @http.route('/game/api/workstations/<int:workstation_id>/start',
                type='json2', auth='user', methods=['POST'])
    def start(self, workstation_id, qty=1, production_id=None):
        """ Press the button: start a run of ``qty`` units, optionally for an order. """
        world = _acting()
        station = _existing(world, 'game.workstation', workstation_id)
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty <= 0:
            raise UserError(world.env._("A run makes a positive quantity."))
        order = _existing(world, 'mrp.production', production_id) if production_id else None
        station._start(qty, order)
        return world._snapshot()

    @http.route('/game/api/shipments/<int:shipment_id>/accept',
                type='json2', auth='user', methods=['POST'])
    def accept(self, shipment_id):
        """ Accept a delivery at the door: its contents now exist. """
        world = _acting()
        _existing(world, 'game.shipment', shipment_id)._accept()
        return world._snapshot()
