# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the post: packing, sending, arriving or coming back, and customers
waiting for their goods (GAME_STATE.md 7.4 and 7.5).

As in test_customer, time is never frozen: settling is given an instant
relative to the package or order under test.
"""
from datetime import datetime, timedelta

from odoo import Command, game_clock
from odoo.exceptions import AccessError
from odoo.game_clock import GameClock
from odoo.tests import new_test_user, tagged
from odoo.tests.common import HttpCase

from odoo.addons.odoo_sim.models.game_post import address_words
from odoo.addons.odoo_sim.tests.test_customer import CustomerCase


class PostCase(CustomerCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Package = cls.env['game.package']

    def pack(self, qty, product=None):
        """ A new package on the bench, with ``qty`` of the clip (or ``product``) in it. """
        package = self.Package._new()
        package._pack(product or self.clip, qty)
        return package

    def arrive(self, package):
        """ Settle as of when ``package`` gets wherever it is going next. """
        self.world._settle(package.date_arrival)

    def complaints(self):
        return self.written("Where are our")


class TestPacking(PostCase):

    def test_an_address_is_its_words_in_order(self):
        written = "Binder & Co.\n12 Clip Lane\r\nSpringfield, OR 97477"

        self.assertEqual(address_words(written), "binder co 12 clip lane springfield or 97477")
        self.assertEqual(address_words("  BINDER & CO., 12 clip lane -- Springfield OR 97477. "), address_words(written))
        self.assertNotEqual(address_words("12 Clip Ln"), address_words("12 Clip Lane"))
        self.assertEqual(address_words("Zürich_8001"), "zürich 8001")

    def test_packing_takes_the_goods_off_the_shelves(self):
        self.give(self.clip, 120)
        package = self.pack(100)

        self.assertEqual(self.on_hand(self.clip), 20)
        self.assertEqual((package.state, package.line_ids.product_id, package.line_ids.qty), ('open', self.clip, 100))
        self.assertEqual((package.entry_ids.kind, package.entry_ids.qty), ('packed', -100))

    def test_more_of_the_same_goes_on_the_same_line(self):
        self.give(self.clip, 100)
        package = self.pack(30)
        package._pack(self.clip, 20)

        self.assertEqual(package.line_ids.qty, 50)
        self.assertEqual(self.on_hand(self.clip), 50)

    def test_what_is_not_there_cannot_be_packed(self):
        self.give(self.clip, 99)
        package = self.Package._new()
        with self.refused("not enough Test clip"):
            package._pack(self.clip, 100)

        self.assertFalse(package.line_ids)
        self.assertEqual(self.on_hand(self.clip), 99)

    def test_a_package_takes_a_positive_quantity(self):
        self.give(self.clip, 10)
        package = self.Package._new()
        for qty in (0, -1, 0.001):
            with self.subTest(qty=qty), self.refused("positive quantity"):
                package._pack(self.clip, qty)

    def test_unpacking_puts_everything_back(self):
        self.give(self.clip, 100)
        self.give(self.wire, 10)
        package = self.pack(100)
        package._pack(self.wire, 2.5)
        package._unpack()

        self.assertEqual((self.on_hand(self.clip), self.on_hand(self.wire)), (100, 10))
        self.assertEqual(package.state, 'unpacked')
        self.assertEqual(sorted(package.entry_ids.mapped('kind')), ['packed', 'packed', 'unpacked', 'unpacked'])
        with self.refused("has been unpacked"):
            package._pack(self.clip, 1)

    def test_an_empty_package_is_not_sent(self):
        with self.refused("is empty"):
            self.Package._new()._send(self.address)

    def test_a_package_is_not_sent_without_an_address(self):
        self.give(self.clip, 1)
        package = self.pack(1)
        for address in ('', ' \n\t', '-- , .'):
            with self.subTest(address=address), self.refused("Write an address"):
                package._send(address)

        self.assertEqual(package.state, 'open')

    def test_sending_writes_the_address_and_schedules_the_arrival(self):
        self.give(self.clip, 1)
        package = self.pack(1)
        package._send("  Test buyer \n 1 Test Street\n1000 Testville  \n")

        self.assertEqual(package.state, 'in_transit')
        self.assertEqual(package.address, self.address, "as written, without the stray blanks")
        self.assertEqual(package.date_arrival, package.date_posted + timedelta(hours=24))
        cron = self.env.ref('odoo_sim.ir_cron_world_settle')
        triggers = self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)])
        self.assertIn(package.date_arrival, triggers.mapped('call_at'))

    def test_a_sent_package_is_out_of_reach(self):
        self.give(self.clip, 20)
        package = self.post(10)
        with self.refused("already been sent"):
            package._pack(self.clip, 10)
        with self.refused("already been sent"):
            package._unpack()
        with self.refused("already been sent"):
            package._send(self.address)

        self.assertEqual(self.on_hand(self.clip), 10)

    def test_nobody_can_pack_through_the_orm(self):
        admin = self.env.ref('base.user_admin')
        for model in ('game.package', 'game.package.line'):
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(admin).check_access('write')

    def test_validating_a_delivery_in_odoo_ships_nothing(self):
        """ Odoo can say the clips went out. They are still on the shelf. """
        self.give(self.clip, 100)
        self.env['stock.quant']._update_available_quantity(
            self.clip, self.env['stock.warehouse'].search([], limit=1).lot_stock_id, 100)
        sale = self.env['sale.order'].create({
            'partner_id': self.buyer.id,
            'order_line': [Command.create({'product_id': self.clip.id, 'product_uom_qty': 100})],
        })
        sale.action_confirm()
        sale.picking_ids.move_ids.picked = True
        sale.picking_ids.button_validate()

        self.assertEqual(sale.picking_ids.state, 'done', "Odoo believes it")
        self.assertEqual(self.on_hand(self.clip), 100, "the world does not")


class TestDelivery(PostCase):

    def test_the_post_takes_its_time_then_the_customer_has_the_goods(self):
        self.give(self.clip, 100)
        order = self.place()
        package = self.post(100)

        self.world._settle(package.date_arrival - timedelta(seconds=1))
        self.assertEqual((package.state, order.qty_received), ('in_transit', 0))

        self.arrive(package)
        self.assertEqual((package.state, package.customer_id, package.order_id), ('delivered', self.customer, order))
        self.assertEqual(package.date_delivered, package.date_arrival)
        self.assertEqual(order.qty_received, 100)

    def test_the_address_may_be_written_any_way_with_the_same_words(self):
        self.give(self.clip, 1)
        package = self.post(1, address="TEST BUYER, 1 test street -- 1000 Testville.")
        self.arrive(package)

        self.assertEqual(package.state, 'delivered')

    def test_a_word_missing_or_out_of_place_is_somewhere_else(self):
        self.give(self.clip, 3)
        for address in (
            "1 Test Street\n1000 Testville",
            "Test buyer\n1 Test Street\n1001 Testville",
            "Test buyer\n1000 Testville\n1 Test Street",
        ):
            with self.subTest(address=address):
                package = self.post(1, address=address)
                self.arrive(package)
                self.assertEqual(package.state, 'returning')
                self.assertFalse(package.customer_id)

    def test_a_package_addressed_to_nobody_comes_back_full(self):
        self.give(self.clip, 100)
        order = self.place()
        package = self.post(100, address="Test buyer\n1 Test Street\n1001 Testville")
        back = package.date_posted + timedelta(hours=48)

        self.arrive(package)
        self.assertEqual((package.state, package.date_arrival, order.qty_received), ('returning', back, 0))

        self.arrive(package)
        self.assertEqual((package.state, package.date_returned), ('open', back))
        self.assertEqual(package.return_reason, "Nobody lives at this address.")
        self.assertEqual((package.line_ids.qty, self.on_hand(self.clip)), (100, 0), "still in the box")

        package._unpack()
        self.assertEqual(self.on_hand(self.clip), 100)

    def test_settling_late_brings_it_all_the_way_back(self):
        self.give(self.clip, 1)
        package = self.post(1, address="Nowhere")
        self.world._settle(package.date_posted + timedelta(days=3))

        self.assertEqual(package.state, 'open')

    def test_a_returned_package_can_be_readdressed_and_sent_again(self):
        self.give(self.clip, 1)
        package = self.post(1, address="Test buyer\nNowhere")
        self.world._settle(package.date_posted + timedelta(days=3))
        package._send(self.address)
        self.assertFalse(package.return_reason)
        self.arrive(package)

        self.assertEqual((package.state, package.customer_id), ('delivered', self.customer))

    def test_goods_paid_for_and_received_complete_the_order(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        package = self.post(100)
        self.arrive(package)

        self.assertEqual((order.state, order.date_delivered, order.date_chase), ('delivered', package.date_arrival, False))

    def test_goods_that_come_before_the_payment_count(self):
        """ Shipping first is the company's risk: the customer keeps the goods, and pays as agreed. """
        self.give(self.clip, 100)
        order = self.place()
        self.arrive(self.post(100))
        self.assertEqual((order.state, order.qty_received), ('requested', 100))

        received = self.read(self.invoice())
        self.world._settle(received.date_due)
        self.assertEqual(self.earned(), 8)
        self.assertEqual((order.state, order.date_delivered), ('delivered', received.date_due))

    def test_a_short_package_leaves_the_order_open_until_the_rest_comes(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        self.arrive(self.post(60))
        self.assertEqual((order.state, order.qty_received), ('paid', 60))

        self.arrive(self.post(40))
        self.assertEqual((order.state, order.qty_received), ('delivered', 100))

    def test_the_customer_keeps_whatever_else_is_in_the_box(self):
        self.give(self.clip, 100)
        self.give(self.wire, 5)
        order = self.paid_order()
        package = self.pack(100)
        package._pack(self.wire, 5)
        package._send(self.address)
        self.arrive(package)

        self.assertEqual(order.state, 'delivered')
        self.assertEqual(self.on_hand(self.wire), 0, "gone with the box")

    def test_goods_nobody_ordered_are_kept_and_count_for_nothing(self):
        self.give(self.clip, 10)
        package = self.post(10)
        self.arrive(package)

        self.assertEqual(package.state, 'delivered')
        self.assertFalse(package.order_id)
        self.assertEqual(self.place().qty_received, 0)


class TestComplaints(PostCase):

    def test_a_customer_that_paid_and_is_still_waiting_complains(self):
        order = self.paid_order()
        due = order.date_chase
        self.assertEqual(due, order.date_paid + timedelta(hours=72))

        self.world._settle(due - timedelta(seconds=1))
        self.assertFalse(self.complaints())

        self.world._settle(due)
        [complaint] = self.complaints()
        self.assertEqual(complaint.subject, "Where are our 100 Test clip?")
        body = complaint._content()['body']
        self.assertIn("have not received them", body)
        self.assertIn("1 Test Street", body, "and says where to send them")
        self.assertEqual(complaint.in_reply_to, order.email_id.message_id, "a follow-up to its order")
        self.assertEqual(complaint.delivery_ids.address, self.seller.email, "to whoever it ordered from")
        self.assertEqual((order.complaint_count, order.date_chase), (1, due + timedelta(hours=72)))

    def test_it_complains_again_until_the_goods_come(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        self.world._settle(order.date_chase)
        self.world._settle(order.date_chase)
        self.assertEqual(len(self.complaints()), 2)

        self.arrive(self.post(100))
        self.assertEqual((order.state, order.date_chase), ('delivered', False))
        self.world._settle(order.date_paid + timedelta(days=60))
        self.assertEqual(len(self.complaints()), 2)

    def test_a_short_delivery_is_complained_about(self):
        self.give(self.clip, 60)
        order = self.paid_order()
        self.arrive(self.post(60))
        self.world._settle(order.date_chase)

        [complaint] = self.complaints()
        self.assertIn("have received only 60", complaint._content()['body'])

    def test_goods_arriving_in_time_spare_the_email(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        self.arrive(self.post(100))
        self.world._settle(order.date_paid + timedelta(days=30))

        self.assertFalse(self.complaints())

    def test_nobody_complains_before_paying(self):
        order = self.place()
        self.world._settle(order.date_requested + timedelta(days=30))

        self.assertFalse(self.complaints())

    def test_a_complaint_is_scheduled(self):
        order = self.paid_order()
        cron = self.env.ref('odoo_sim.ir_cron_world_settle')
        triggers = self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id)])

        self.assertIn(order.date_chase, triggers.mapped('call_at'))


class TestPostSnapshot(PostCase):

    def test_the_page_sees_the_bench_and_what_was_sent_but_not_where_it_is(self):
        self.give(self.clip, 100)
        on_the_bench = self.pack(40)
        sent = self.post(60)
        snapshot = self.world._snapshot()

        [bench] = [p for p in snapshot['packages'] if p['id'] == on_the_bench.id]
        self.assertEqual(
            {key: bench[key] for key in ('name', 'address', 'returned')},
            {'name': on_the_bench.display_name, 'address': None, 'returned': None},
        )
        self.assertEqual([(line['id'], line['qty']) for line in bench['lines']], [(self.clip.id, 40)])
        self.assertNotIn(sent.id, [p['id'] for p in snapshot['packages']])

        [posted] = [p for p in snapshot['sent_packages'] if p['id'] == sent.id]
        self.assertEqual((posted['address'], posted['date_posted']), (self.address, sent.date_posted.isoformat()))
        self.assertNotIn('state', posted, "the post has no tracking")

        self.arrive(sent)
        self.assertIn(sent.id, [p['id'] for p in self.world._snapshot()['sent_packages']], "delivered looks the same")

    def test_a_returned_package_is_back_on_the_bench_and_says_why(self):
        self.give(self.clip, 1)
        package = self.post(1, address="Nowhere")
        self.world._settle(package.date_posted + timedelta(days=3))

        [shown] = [p for p in self.world._snapshot()['packages'] if p['id'] == package.id]
        self.assertEqual((shown['returned'], shown['address']), ("Nobody lives at this address.", "Nowhere"))
        self.assertEqual(shown['date_returned'], package.date_returned.isoformat())

    def test_received_orders_leave_the_snapshot(self):
        self.give(self.clip, 100)
        order = self.paid_order()
        self.arrive(self.post(100))

        self.assertNotIn(order.id, [o['id'] for o in self.world._snapshot()['customer_orders']])


@tagged('-at_install', 'post_install')
class TestPostApi(PostCase, HttpCase):
    """Packing and sending over real HTTP, with the clock pinned as in test_world's TestWorldApi."""

    def setUp(self):
        super().setUp()
        self.addCleanup(game_clock.invalidate, self.env.cr.dbname)
        self.world_running()
        self.authenticate('admin', 'admin')

    def world_running(self, paused=False):
        now = datetime.now()
        game_clock.override(self.env.cr.dbname, GameClock(now, now, 1.0, paused, timedelta(hours=1)))

    def call(self, url, body=None):
        return self.url_open(url, json=body or {}, method='POST')

    def test_taking_packing_and_sending_a_package(self):
        self.give(self.clip, 100)
        response = self.call('/game/api/packages')
        self.assertEqual(response.status_code, 200, response.text)
        package_id = max(p['id'] for p in response.json()['packages'])

        response = self.call(f'/game/api/packages/{package_id}/pack', {'product_id': self.clip.id, 'qty': 100})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.on_hand(self.clip), 0)

        response = self.call(f'/game/api/packages/{package_id}/send', {'address': self.address})
        self.assertEqual(response.status_code, 200, response.text)
        world = response.json()
        self.assertNotIn(package_id, [p['id'] for p in world['packages']])
        self.assertIn(package_id, [p['id'] for p in world['sent_packages']])

    def test_unpacking_a_package(self):
        self.give(self.clip, 10)
        package = self.pack(10)
        response = self.call(f'/game/api/packages/{package.id}/unpack')

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.on_hand(self.clip), 10)

    def test_what_is_not_there_is_not_packed(self):
        package = self.Package._new()
        response = self.call(f'/game/api/packages/{package.id}/pack', {'product_id': self.clip.id, 'qty': 5})

        self.assertEqual(response.status_code, 422)
        self.assertIn("not enough Test clip", response.json()['message'])
        self.assertFalse(package.line_ids)

    def test_nonsense_is_refused(self):
        self.give(self.clip, 10)
        url = f'/game/api/packages/{self.Package._new().id}'
        for body in (
            {'qty': 1},
            {'product_id': str(self.clip.id), 'qty': 1},
            {'product_id': True, 'qty': 1},
            {'product_id': self.clip.id, 'qty': '1'},
            {'product_id': self.clip.id, 'qty': 0},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.call(f'{url}/pack', body).status_code, 422)
        self.assertEqual(self.call(f'{url}/pack', {'product_id': 999999999, 'qty': 1}).status_code, 404)
        self.assertEqual(self.call(f'{url}/send', {'address': 42}).status_code, 422)
        self.assertEqual(self.call('/game/api/packages/999999999/unpack').status_code, 404)
        self.assertEqual(self.on_hand(self.clip), 10)

    def test_nothing_is_packed_in_a_paused_world(self):
        self.world_running(paused=True)
        before = self.Package.search_count([])
        response = self.call('/game/api/packages')

        self.assertEqual(response.status_code, 422)
        self.assertIn("not running", response.json()['message'])
        self.assertEqual(self.Package.search_count([]), before)

    def test_the_post_is_for_employees(self):
        new_test_user(self.env, 'portal_buyer', groups='base.group_portal', password='portal-password')
        self.authenticate('portal_buyer', 'portal-password')

        self.assertEqual(self.call('/game/api/packages').status_code, 403)
