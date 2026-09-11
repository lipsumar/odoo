# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the game world's own state (see odoo_sim/GAME_STATE.md).

Every test here builds its own products, recipe, workstation and vendor, so the
suite does not depend on the paperclip scenario -- that has its own smoke test
in ``odoo_sim_paperclips``.

Time is never frozen.  The suite runs against ordinary databases and game
worlds alike (DESIGN.md 8), and on a world ``fields.Datetime.now()`` is game
time, which freezegun does not pin.  So settling is always given an explicit
instant, taken relative to the record under test: "a second before this run
ends", never "at ten o'clock".
"""
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command, game_clock
from odoo.exceptions import AccessError, UserError
from odoo.game_clock import GameClock
from odoo.tests import new_test_user, tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.odoo_sim import pulse
from odoo.addons.odoo_sim.controllers import main
from odoo.addons.odoo_sim.models.game_world import CHANGED


class WorldCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.metre = cls.env.ref('uom.product_uom_meter')
        cls.unit = cls.env.ref('uom.product_uom_unit')
        cls.spool = cls.env['uom.uom'].create({
            'name': "Test spool (50 m)", 'relative_factor': 50, 'relative_uom_id': cls.metre.id,
        })
        cls.wire, cls.clip = cls.env['product.product'].create([
            {'name': "Test wire", 'is_storable': True, 'uom_id': cls.metre.id},
            {'name': "Test clip", 'is_storable': True, 'uom_id': cls.unit.id},
        ])
        cls.recipe = cls.env['game.recipe'].create({
            'product_id': cls.clip.id,
            'duration': 2,
            'line_ids': [Command.create({'product_id': cls.wire.id, 'qty': 0.1})],
        })
        cls.station = cls.env['game.workstation'].create({'name': "Test bench", 'recipe_id': cls.recipe.id})
        cls.partner = cls.env['res.partner'].create({'name': "Test wire vendor", 'is_company': True})
        cls.vendor = cls.env['game.vendor'].create({
            'partner_id': cls.partner.id, 'lead_time': 24, 'product_ids': [Command.link(cls.wire.id)],
        })
        cls.stock = cls.env['game.stock']
        cls.world = cls.env['game.world']

    def on_hand(self, product):
        """ What exists in the world, read back from the database. """
        self.stock.invalidate_model()
        return self.stock.search([('product_id', '=', product.id)]).qty

    def ledger_total(self, product):
        return sum(self.env['game.stock.entry'].search([('product_id', '=', product.id)]).mapped('qty'))

    def give(self, product, qty):
        self.stock._apply(product, qty, 'genesis')

    @contextmanager
    def refused(self, why):
        """ ``assertRaises(UserError)``, matching ``why``.

        Odoo's ``assertRaises`` rolls a savepoint back on the way out, the way
        a request's transaction would be; unittest's ``assertRaisesRegex`` does
        not, and would leave half an action behind for the test to trip over.
        """
        with self.assertRaises(UserError) as caught:
            yield
        self.assertRegex(str(caught.exception), why)

    def notices(self):
        """ The world-changed notices queued on this transaction's bus. """
        values = self.env.cr.precommit.data.get('bus.bus.values', [])
        return [value for value in values if f'"{CHANGED}"' in value['message']]


class TestLedger(WorldCase):

    def test_adding_creates_the_balance_and_records_why(self):
        entry = self.stock._apply(self.wire, 50, 'received')

        self.assertEqual(self.on_hand(self.wire), 50)
        self.assertEqual((entry.product_id, entry.qty, entry.kind), (self.wire, 50, 'received'))

    def test_taking_more_than_exists_is_refused_and_changes_nothing(self):
        self.give(self.wire, 1)
        with self.refused("not enough Test wire"):
            self.stock._apply(self.wire, -1.5, 'consumed')

        self.assertEqual(self.on_hand(self.wire), 1)
        self.assertEqual(self.ledger_total(self.wire), 1, "no entry for what did not happen")

    def test_taking_what_was_never_there_is_refused(self):
        """ No balance row at all is the same as zero, not a crash. """
        with self.refused("not enough"):
            self.stock._apply(self.clip, -1, 'consumed')

    def test_the_last_tenth_can_be_taken(self):
        """ NUMERIC, not float8: 0.3 m of wire makes three paperclips.

        In binary floating point 0.3 - 0.1 - 0.1 - 0.1 is -2.8e-17, so the
        ``qty + delta >= 0`` guard would refuse the third paperclip with the
        wire sitting right there on the bench.
        """
        self.give(self.wire, 0.3)
        for _ in range(3):
            self.stock._apply(self.wire, -0.1, 'consumed')

        self.assertEqual(self.on_hand(self.wire), 0)
        with self.assertRaises(UserError):
            self.stock._apply(self.wire, -0.1, 'consumed')

    def test_the_balance_is_the_sum_of_the_ledger(self):
        self.give(self.wire, 100)
        self.stock._apply(self.wire, -12.3, 'consumed')
        self.stock._apply(self.wire, 50, 'received')

        self.assertAlmostEqual(self.on_hand(self.wire), 137.7)
        self.assertAlmostEqual(self.ledger_total(self.wire), self.on_hand(self.wire))

    def test_nobody_can_write_reality_through_the_orm(self):
        """ Not even an administrator: the game writes through sudo() only. """
        self.give(self.wire, 5)
        balance = self.stock.search([('product_id', '=', self.wire.id)])
        admin = self.env.ref('base.user_admin')
        player = new_test_user(self.env, 'player', groups='base.group_user')

        self.assertEqual(balance.with_user(admin).qty, 5, "administrators may look, for debugging")
        for model in ('game.stock', 'game.stock.entry', 'game.production', 'game.shipment'):
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(admin).check_access('write')
        with self.assertRaises(AccessError):
            balance.with_user(admin).write({'qty': 5000})
        with self.assertRaises(AccessError):
            balance.with_user(player).read(['qty'])


class TestGenesis(WorldCase):

    def setUp(self):
        super().setUp()
        # Genesis already ran when the module was installed; start it over.
        self.env['game.stock.entry'].search([]).unlink()
        self.stock.search([]).unlink()
        self.warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)

    def test_genesis_copies_what_odoo_holds(self):
        self.env['stock.quant']._update_available_quantity(self.wire, self.warehouse.lot_stock_id, 75)
        self.world._genesis()

        self.assertEqual(self.on_hand(self.wire), 75)
        self.assertEqual(self.env['game.stock.entry'].search([('product_id', '=', self.wire.id)]).kind, 'genesis')

    def test_genesis_happens_once(self):
        """ After the first entry, Odoo's quants are claims, not the truth. """
        self.env['stock.quant']._update_available_quantity(self.wire, self.warehouse.lot_stock_id, 75)
        self.world._genesis()
        self.env['stock.quant']._update_available_quantity(self.wire, self.warehouse.lot_stock_id, 1000)
        self.world._genesis()

        self.assertEqual(self.on_hand(self.wire), 75)

    def test_genesis_ignores_what_is_not_in_the_warehouse(self):
        """ Stock at a vendor or in transit is not the company's to count. """
        vendors = self.env.ref('stock.stock_location_suppliers')
        self.env['stock.quant']._update_available_quantity(self.wire, vendors, 75)
        self.world._genesis()

        self.assertEqual(self.on_hand(self.wire), 0)


class TestWorkstation(WorldCase):

    def test_a_run_takes_its_components_at_once_and_makes_goods_when_it_ends(self):
        self.give(self.wire, 50)
        run = self.station._start(10)

        self.assertEqual(run.date_end - run.date_start, timedelta(minutes=20), "2 game minutes a clip")
        self.assertEqual(self.on_hand(self.wire), 49, "a metre of wire is on the bench")
        self.assertEqual(self.on_hand(self.clip), 0, "nothing is made yet")

        self.world._settle(run.date_end - timedelta(seconds=1))
        self.assertEqual(run.state, 'running')
        self.assertEqual(self.on_hand(self.clip), 0)

        self.world._settle(run.date_end)
        self.assertEqual(run.state, 'done')
        self.assertEqual(self.on_hand(self.clip), 10)

    def test_goods_exist_from_when_the_run_ended_not_when_it_was_noticed(self):
        self.give(self.wire, 1)
        run = self.station._start(1)
        self.world._settle(run.date_end + timedelta(hours=3))

        made = run.entry_ids.filtered(lambda e: e.kind == 'manufactured')
        self.assertEqual(made.date, run.date_end)

    def test_settling_twice_makes_the_goods_once(self):
        self.give(self.wire, 1)
        run = self.station._start(1)
        self.world._settle(run.date_end)
        self.world._settle(run.date_end + timedelta(hours=1))

        self.assertEqual(self.on_hand(self.clip), 1)

    def test_a_station_does_one_thing_at_a_time(self):
        self.give(self.wire, 10)
        run = self.station._start(1)
        with self.refused("already running"):
            self.station._start(1)

        self.world._settle(run.date_end)
        self.station._start(1)  # free again

    def test_starting_a_station_whose_run_is_due_finishes_that_run_first(self):
        """ An action sees the world as of when it was taken, not as of the last cron. """
        self.give(self.wire, 10)
        run = self.station._start(1)
        # Pretend the run was started long ago and the cron has not caught up.
        run.date_end = run.date_start - timedelta(minutes=1)

        self.station._start(1)
        self.assertEqual(run.state, 'done')
        self.assertEqual(self.on_hand(self.clip), 1)

    def test_no_wire_no_paperclips(self):
        """ Conservation: a run without its components does not start at all. """
        self.give(self.wire, 0.25)
        with self.refused("not enough Test wire"):
            self.station._start(3)

        self.assertFalse(self.station.production_ids, "no run was left behind")
        self.assertEqual(self.on_hand(self.wire), 0.25)

    def test_the_world_follows_its_recipe_not_the_bom(self):
        """ The BoM is the player's model. Here it claims a clip needs no wire. """
        self.env['mrp.bom'].create({'product_tmpl_id': self.clip.product_tmpl_id.id, 'product_qty': 1})
        with self.refused("not enough Test wire"):
            self.station._start(1)

    def test_marking_an_order_done_makes_nothing_real(self):
        """ Odoo can say it made five clips. The world still has none. """
        bom = self.env['mrp.bom'].create({
            'product_tmpl_id': self.clip.product_tmpl_id.id,
            'product_qty': 1,
            'bom_line_ids': [Command.create({'product_id': self.wire.id, 'product_qty': 0.1})],
        })
        order = self.env['mrp.production'].create({'product_id': self.clip.id, 'product_qty': 5, 'bom_id': bom.id})
        order.action_confirm()
        order.button_mark_done()

        self.assertEqual(order.state, 'done')
        self.assertEqual(self.clip.qty_available, 5, "Odoo believes it")
        self.assertEqual(self.on_hand(self.clip), 0, "the world does not")

    def test_a_run_can_name_its_order(self):
        self.give(self.wire, 1)
        order = self.env['mrp.production'].create({'product_id': self.clip.id, 'product_qty': 5})
        run = self.station._start(1, order)
        order.action_cancel()

        self.assertEqual(run.production_id, order)
        self.world._settle(run.date_end)
        self.assertEqual(self.on_hand(self.clip), 1, "the order is never read back")

    def test_a_run_cannot_name_an_order_for_something_else(self):
        self.give(self.wire, 1)
        order = self.env['mrp.production'].create({'product_id': self.wire.id, 'product_qty': 5})
        with self.refused("is for Test wire"):
            self.station._start(1, order)

    def test_a_run_makes_a_positive_quantity(self):
        for qty in (0, -1):
            with self.subTest(qty=qty), self.assertRaises(UserError):
                self.station._start(qty)

    def test_starting_a_run_schedules_its_end(self):
        """ The settle cron is triggered at the end, so the goods land on time. """
        self.give(self.wire, 1)
        run = self.station._start(1)

        cron = self.env.ref('odoo_sim.ir_cron_world_settle')
        triggers = self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)])
        self.assertIn(run.date_end, triggers.mapped('call_at'))

    def test_starting_and_finishing_tell_the_pages(self):
        self.give(self.wire, 1)
        self.env.cr.precommit.data.pop('bus.bus.values', None)
        run = self.station._start(1)
        self.assertTrue(self.notices(), "starting consumed wire")

        self.env.cr.precommit.data.pop('bus.bus.values', None)
        self.world._settle(run.date_end)
        notices = self.notices()
        self.assertTrue(notices, "finishing made a clip")
        self.assertIn(pulse.CHANNEL, notices[0]['channel'])
        self.assertEqual(json.loads(notices[0]['message'])['payload'], {}, "the notice carries nothing")


class TestVendor(WorldCase):

    def order(self, *lines, partner=None):
        """ A purchase order approved by the player: ``(product, qty, uom)`` lines. """
        order = self.env['purchase.order'].create({
            'partner_id': (partner or self.partner).id,
            'order_line': [
                Command.create({'product_id': product.id, 'product_qty': qty, 'product_uom_id': uom.id})
                for product, qty, uom in lines
            ],
        })
        order.button_confirm()
        return order

    def shipments(self, order):
        return self.env['game.shipment'].search([('purchase_id', '=', order.id)])

    def test_approving_an_order_wakes_the_vendor_agent(self):
        cron = self.env.ref('odoo_sim.ir_cron_vendor_agent')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        self.order((self.wire, 1, self.spool))

        self.assertGreater(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)

    def test_the_vendor_ships_in_the_products_own_unit(self):
        """ Two spools are 100 m of wire, which is what the world holds. """
        order = self.order((self.wire, 2, self.spool))
        self.vendor._cron_process_orders()

        shipment = self.shipments(order)
        self.assertEqual(shipment.state, 'in_transit')
        self.assertEqual(shipment.line_ids.product_id, self.wire)
        self.assertEqual(shipment.line_ids.qty, 100)
        self.assertEqual(shipment.date_arrival - shipment.date_shipped, timedelta(hours=24))

    def test_the_vendor_ships_an_order_once(self):
        order = self.order((self.wire, 1, self.spool))
        self.vendor._cron_process_orders()
        self.vendor._cron_process_orders()

        self.assertEqual(len(self.shipments(order)), 1)

    def test_a_shipment_is_what_was_ordered_then(self):
        """ Editing the order afterwards does not change what is on the truck. """
        order = self.order((self.wire, 1, self.spool))
        self.vendor._cron_process_orders()
        order.order_line.product_qty = 20

        self.assertEqual(self.shipments(order).line_ids.qty, 50)

    def test_the_vendor_ships_only_what_it_sells(self):
        order = self.order((self.wire, 1, self.spool), (self.clip, 100, self.unit))
        self.vendor._cron_process_orders()

        self.assertEqual(self.shipments(order).line_ids.product_id, self.wire)

    def test_nobody_ships_an_order_to_a_company_that_is_not_a_vendor(self):
        stranger = self.env['res.partner'].create({'name': "Not a vendor"})
        order = self.order((self.wire, 1, self.spool), partner=stranger)
        self.vendor._cron_process_orders()

        self.assertFalse(self.shipments(order))

    def test_an_order_to_a_vendor_contact_reaches_the_vendor(self):
        contact = self.env['res.partner'].create({'name': "Sales desk", 'parent_id': self.partner.id})
        order = self.order((self.wire, 1, self.spool), partner=contact)
        self.vendor._cron_process_orders()

        self.assertEqual(self.shipments(order).vendor_id, self.vendor)

    def test_a_draft_order_is_not_shipped(self):
        order = self.env['purchase.order'].create({
            'partner_id': self.partner.id,
            'order_line': [Command.create({'product_id': self.wire.id, 'product_qty': 1})],
        })
        self.vendor._cron_process_orders()

        self.assertFalse(self.shipments(order))

    def test_a_shipment_arrives_after_the_lead_time_and_exists_once_accepted(self):
        order = self.order((self.wire, 1, self.spool))
        self.vendor._cron_process_orders()
        shipment = self.shipments(order)

        with self.refused("has not arrived yet"):
            shipment._accept()

        self.world._settle(shipment.date_arrival - timedelta(seconds=1))
        self.assertEqual(shipment.state, 'in_transit')
        self.world._settle(shipment.date_arrival)
        self.assertEqual(shipment.state, 'arrived')
        self.assertEqual(self.on_hand(self.wire), 0, "at the door is not in the world yet")

        shipment._accept()
        self.assertEqual(shipment.state, 'accepted')
        self.assertEqual(self.on_hand(self.wire), 50)
        self.assertEqual(shipment.entry_ids.kind, 'received')

        with self.refused("already been accepted"):
            shipment._accept()
        self.assertEqual(self.on_hand(self.wire), 50)

    def test_validating_the_receipt_makes_nothing_real(self):
        """ Odoo can say 50 m arrived. The world has none until the delivery is accepted. """
        order = self.order((self.wire, 1, self.spool))
        receipt = order.picking_ids
        receipt.move_ids.picked = True
        receipt.button_validate()

        self.assertEqual(receipt.state, 'done')
        self.assertEqual(self.wire.qty_available, 50, "Odoo believes it")
        self.assertEqual(self.on_hand(self.wire), 0, "the world does not")


class TestSnapshot(WorldCase):

    def test_the_snapshot_shows_the_world_as_the_page_needs_it(self):
        self.give(self.wire, 10)
        order = self.env['mrp.production'].create({'product_id': self.clip.id, 'product_qty': 5})
        order.action_confirm()
        run = self.station._start(3, order)
        snapshot = self.world._snapshot()

        stock = {line['name']: line['qty'] for line in snapshot['stock']}
        self.assertEqual(stock['Test wire'], 9.7)
        self.assertEqual(stock['Test clip'], 0, "listed at zero: the station makes it")

        station = next(s for s in snapshot['workstations'] if s['id'] == self.station.id)
        self.assertEqual(station['product']['name'], "Test clip")
        self.assertEqual(station['recipe'], [{'id': self.wire.id, 'name': "Test wire", 'uom': 'm', 'qty': 0.1}])
        self.assertEqual(station['run']['date_end'], run.date_end.isoformat())
        self.assertEqual(station['run']['order'], {'id': order.id, 'name': order.name})
        self.assertIn(order.id, [o['id'] for o in station['orders']])

    def test_accepted_shipments_leave_the_snapshot(self):
        order = self.env['purchase.order'].create({
            'partner_id': self.partner.id,
            'order_line': [Command.create({'product_id': self.wire.id, 'product_qty': 1, 'product_uom_id': self.spool.id})],
        })
        order.button_confirm()
        self.vendor._cron_process_orders()
        shipment = self.env['game.shipment'].search([('purchase_id', '=', order.id)])

        listed = [s for s in self.world._snapshot()['shipments'] if s['id'] == shipment.id]
        self.assertEqual(listed[0]['state'], 'in_transit')
        self.assertEqual(listed[0]['lines'][0]['qty'], 50)

        self.world._settle(shipment.date_arrival)
        shipment._accept()
        self.assertNotIn(shipment.id, [s['id'] for s in self.world._snapshot()['shipments']])


@tagged('-at_install', 'post_install')
class TestWorldApi(WorldCase, HttpCase):
    """The world's endpoints, over real HTTP.

    The clock is pinned with ``game_clock.override`` as in ``test_controllers``,
    with a ``max_gap`` of an hour so that a slow test run does not see its own
    world die under it.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(game_clock.invalidate, self.env.cr.dbname)
        self.world_running()
        self.authenticate('admin', 'admin')

    def world_running(self, *, paused=False, silent_for=timedelta(0)):
        now = datetime.now()
        game_clock.override(self.env.cr.dbname, GameClock(
            now, now - silent_for, 1.0, paused, timedelta(hours=1),
        ))

    def post(self, url, body=None):
        return self.url_open(url, json=body, method='POST')

    def start(self, **body):
        return self.post(f'/game/api/workstations/{self.station.id}/start', body)

    def test_pressing_the_button_starts_a_run(self):
        self.give(self.wire, 10)
        response = self.start(qty=3)

        self.assertEqual(response.status_code, 200, response.text)
        world = response.json()
        station = next(s for s in world['workstations'] if s['id'] == self.station.id)
        self.assertEqual(station['run']['qty'], 3)
        self.assertEqual(self.on_hand(self.wire), 9.7)

    def test_a_run_can_be_started_for_an_order(self):
        self.give(self.wire, 1)
        order = self.env['mrp.production'].create({'product_id': self.clip.id, 'product_qty': 5})
        self.assertEqual(self.start(qty=1, production_id=order.id).status_code, 200)

        self.assertEqual(self.station.production_ids.production_id, order)

    def test_nothing_happens_in_a_paused_world(self):
        self.give(self.wire, 10)
        self.world_running(paused=True)
        response = self.start(qty=1)

        self.assertEqual(response.status_code, 422)
        self.assertIn("not running", response.json()['message'])
        self.assertFalse(self.station.production_ids)
        self.assertEqual(self.on_hand(self.wire), 10)

    def test_nothing_happens_in_a_world_nobody_is_ticking(self):
        """ The run would start, and no cron would ever finish it (DESIGN.md 3.3). """
        self.give(self.wire, 10)
        self.world_running(silent_for=timedelta(hours=2))

        self.assertEqual(self.start(qty=1).status_code, 422)
        self.assertFalse(self.station.production_ids)

    def test_a_refused_run_leaves_nothing_behind(self):
        self.give(self.wire, 0.1)
        response = self.start(qty=2)

        self.assertEqual(response.status_code, 422)
        self.assertIn("not enough Test wire", response.json()['message'])
        self.assertFalse(self.station.production_ids)
        self.assertEqual(self.on_hand(self.wire), 0.1)

    def test_a_quantity_is_a_positive_number(self):
        self.give(self.wire, 10)
        for qty in ('3', True, 0, -2):
            with self.subTest(qty=qty):
                self.assertEqual(self.start(qty=qty).status_code, 422)
        self.assertFalse(self.station.production_ids)

    def test_an_unknown_workstation_is_not_found(self):
        self.assertEqual(self.post('/game/api/workstations/999999/start', {}).status_code, 404)

    def test_accepting_a_delivery(self):
        order = self.env['purchase.order'].create({
            'partner_id': self.partner.id,
            'order_line': [Command.create({'product_id': self.wire.id, 'product_qty': 2, 'product_uom_id': self.spool.id})],
        })
        order.button_confirm()
        self.vendor._cron_process_orders()
        shipment = self.env['game.shipment'].search([('purchase_id', '=', order.id)])
        url = f'/game/api/shipments/{shipment.id}/accept'

        self.assertEqual(self.post(url).status_code, 422, "not arrived yet")
        self.world._settle(shipment.date_arrival)
        response = self.post(url)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.on_hand(self.wire), 100)
        self.assertNotIn(shipment.id, [s['id'] for s in response.json()['shipments']])

    def test_reading_the_world(self):
        self.give(self.wire, 4)
        world = self.url_open('/game/api/world').json()

        self.assertIn({'id': self.wire.id, 'name': "Test wire", 'uom': 'm', 'qty': 4.0}, world['stock'])

    def test_the_world_is_for_employees(self):
        """ ``auth='user'`` admits portal users too; the world does not. """
        new_test_user(self.env, 'customer', groups='base.group_portal', password='customer-password')
        self.authenticate('customer', 'customer-password')

        self.assertEqual(self.url_open('/game/api/world').status_code, 403)
        self.assertEqual(self.start(qty=1).status_code, 403)

    def test_the_page_carries_the_world(self):
        """ The first state arrives with the document, like the first clock reading. """
        with patch.object(main, '_built_assets', return_value=([], [])):
            page = self.url_open('/game').text
        bootstrap = json.loads(re.search(r'var odooSim = (\{.*\});', page).group(1))

        self.assertEqual(bootstrap['changed_type'], CHANGED)
        self.assertIn(self.station.id, [s['id'] for s in bootstrap['world']['workstations']])
