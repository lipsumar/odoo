# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The world as a whole: time-driven events, the page's view of it, and genesis.

See ``odoo_sim/GAME_STATE.md`` sections 3.3, 7 and 8.
"""
from odoo import api, fields, models
from odoo.tools import SQL

from odoo.addons.odoo_sim import pulse

#: Bus notification type saying "the world changed; fetch it again".  Rides the
#: pulse's channel, and **carries nothing**: any websocket, logged in or not,
#: may subscribe to any string channel (``bus/models/ir_websocket.py``), so
#: the state itself is fetched from ``GET /game/api/world`` behind a login.
CHANGED = 'odoo_sim.world_changed'

#: Manufacturing orders a workstation offers to link a run to.
OPEN_MO_STATES = ('confirmed', 'progress', 'to_close')


def _instant(value):
    """ Naive UTC ISO, as ``pulse.payload`` sends datetimes (see its docstring). """
    return value.isoformat() if value else None


class GameWorld(models.AbstractModel):
    _name = 'game.world'
    _description = "Game world"

    # -- time-driven events ------------------------------------------------

    @api.model
    def _settle(self, now=None):
        """ Make everything that was due by ``now`` (game time) have happened.

        Runs that have ended put their goods into the world; shipments whose
        arrival time has passed are at the door.  Run by a cron triggered at
        each due instant, and at the start of every player action, so that an
        action always sees the world as of the moment it was taken -- the cron
        alone lags by up to one cron tick (``cron-tick x rate`` game seconds).

        Rows another transaction is already settling are skipped rather than
        waited on; that transaction will finish them.
        """
        now = now or fields.Datetime.now()
        runs = self._lock_due('game.production', 'running', 'date_end', now)
        runs._finish()
        shipments = self._lock_due('game.shipment', 'in_transit', 'date_arrival', now)
        shipments.state = 'arrived'
        if runs or shipments:
            self._changed()
        return runs, shipments

    def _lock_due(self, model, state, date_field, now):
        records = self.env[model]
        records.flush_model(['state', date_field])
        self.env.cr.execute(SQL(
            "SELECT id FROM %s WHERE state = %s AND %s <= %s ORDER BY %s, id FOR UPDATE SKIP LOCKED",
            SQL.identifier(records._table), state, SQL.identifier(date_field), now,
            SQL.identifier(date_field),
        ))
        records = records.browse(id_ for id_, in self.env.cr.fetchall())
        records.invalidate_recordset(['state'])
        return records.filtered(lambda r: r.state == state)

    @api.model
    def _cron_settle(self):
        self._settle()

    @api.model
    def _schedule_settle(self, at):
        """ Have the settle cron run at game instant ``at``.

        A future trigger does not wake the loop (``ir_cron._trigger_list`` only
        notifies for triggers already due), so it is picked up by the loop's
        next poll: settling lags ``at`` by at most one cron tick.
        """
        cron = self.env.ref('odoo_sim.ir_cron_world_settle', raise_if_not_found=False)
        if cron:
            cron._trigger(at)

    @api.model
    def _changed(self):
        """ Tell every open page the world changed.

        The bus only publishes at commit, so a page that fetches on hearing
        this sees the whole transaction's effect.  A transaction that changes
        several things sends several notices; the page coalesces the fetches
        rather than this trying to deduplicate them (a flush clears
        ``precommit.data`` mid-transaction, so a flag kept there would not
        hold anyway).
        """
        if 'bus.bus' in self.env:
            self.env['bus.bus'].sudo()._sendone(pulse.CHANNEL, CHANGED, {})

    # -- genesis -------------------------------------------------------------

    @api.model
    def _genesis(self):
        """ Assume Odoo was telling the truth, once, when the world began.

        A world is created on a database that may already hold stock.  Copying
        the internal quants in as ``genesis`` entries makes reality and Odoo
        agree at the start, so every later difference between them is one the
        game caused.  Does nothing once the ledger has any entry at all.
        """
        if self.env['game.stock.entry'].search_count([], limit=1):
            return
        stock = self.env['game.stock']
        for product, qty in self.env['stock.quant']._read_group(
            [('location_id.usage', '=', 'internal')], ['product_id'], ['quantity:sum'],
        ):
            if qty > 0:
                stock._apply(product, qty, 'genesis')

    # -- what the page shows -----------------------------------------------

    @api.model
    def _snapshot(self, user=None):
        """ The whole world, as ``GET /game/api/world`` hands it to the page.

        One projection for one screen (UI_DESIGN.md 9.4), not a generic read.
        Datetimes are naive UTC ISO, exactly as in the pulse, so the page
        parses both with one function.

        Given ``user``, it carries their mail too (``mail``, MAIL.md): the page
        is one player's view of the world, and their inbox is on it.
        """
        workstations = self.env['game.workstation'].search([])
        vendors = self.env['game.vendor'].search([])
        running = self.env['game.production'].search([('state', '=', 'running')])
        shipments = self.env['game.shipment'].search([('state', '!=', 'accepted')])
        balances = self.env['game.stock'].search([])

        # Everything the world knows how to make, use or buy is listed even at
        # zero: an empty shelf is worth seeing.
        products = (
            balances.product_id
            | workstations.recipe_id.product_id
            | workstations.recipe_id.line_ids.product_id
            | vendors.product_ids
        )
        on_hand = {balance.product_id: balance.qty for balance in balances}

        def product(record):
            return {'id': record.id, 'name': record.display_name, 'uom': record.uom_id.name}

        def run(record):
            if not record:
                return None
            return {
                'id': record.id,
                'qty': record.qty,
                'date_start': _instant(record.date_start),
                'date_end': _instant(record.date_end),
                'order': {'id': record.production_id.id, 'name': record.production_id.name}
                if record.production_id else None,
            }

        orders = self.env['mrp.production'].search([
            ('product_id', 'in', workstations.product_id.ids),
            ('state', 'in', OPEN_MO_STATES),
        ], order='date_start, id')

        snapshot = {
            'stock': [
                dict(product(record), qty=on_hand.get(record, 0.0))
                for record in products.sorted('display_name')
            ],
            'workstations': [{
                'id': station.id,
                'name': station.name,
                'product': product(station.product_id),
                'duration': station.recipe_id.duration,
                'recipe': [
                    dict(product(line.product_id), qty=line.qty)
                    for line in station.recipe_id.line_ids
                ],
                'run': run(running.filtered(lambda r: r.workstation_id == station)[:1]),
                'orders': [
                    {'id': order.id, 'name': order.name, 'qty': order.product_qty}
                    for order in orders if order.product_id == station.product_id
                ],
            } for station in workstations],
            'shipments': [{
                'id': shipment.id,
                'vendor': shipment.vendor_id.partner_id.display_name,
                'order': shipment.purchase_id.name or None,
                'state': shipment.state,
                'date_shipped': _instant(shipment.date_shipped),
                'date_arrival': _instant(shipment.date_arrival),
                'lines': [dict(product(line.product_id), qty=line.qty) for line in shipment.line_ids],
            } for shipment in shipments],
        }
        if user is not None:
            snapshot['mail'] = self.env['game.email']._mailbox(user)
        return snapshot
