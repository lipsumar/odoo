# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for employees: hiring, their working day, the work they take on, and
their pay (EMPLOYEES.md).

As everywhere in this suite, time is never frozen (see common).  An employee
only works from nine to five, so a test that needs one at work hires them in a
time zone where it is ten o'clock now (``zone_at``), and settles as of instants
relative to the task under test.

The work an employee takes on is whatever Odoo says is ready, oldest first.
On a database someone has been playing in there may be older work about, so
the orders and transfers made here are dated in the year 2000.
"""
from datetime import date, datetime, time, timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged
from odoo.tests.common import HttpCase

from odoo.addons.odoo_sim import workday
from odoo.addons.odoo_sim.tests.common import CustomerCase, zone_at

LONG_AGO = datetime(2000, 1, 1)


class EmployeeCase(CustomerCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Employee = cls.env['game.employee']
        cls.Task = cls.env['game.employee.task']
        cls.job = cls.env['game.job'].create({'name': "Test worker", 'wage': 310})
        cls.stock_location = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1).lot_stock_id
        # The BoM says what the recipe says: 10 cm of wire a clip.
        cls.bom = cls.env['mrp.bom'].create({
            'product_tmpl_id': cls.clip.product_tmpl_id.id,
            'product_qty': 1,
            'bom_line_ids': [Command.create({'product_id': cls.wire.id, 'product_qty': 0.1})],
        })
        # Where Odoo says the buyer lives, written as an employee copies it
        # onto a box -- and where the post finds it.
        cls.buyer.write({'street': "1 Test Street", 'city': "Testville", 'zip': "1000"})
        cls.address = "Test buyer\n1 Test Street\nTestville 1000"
        cls.customer.address = cls.address

    def hire(self, hour=10, job=None):
        """ Someone hired now, in a time zone where it is ``hour`` o'clock. """
        return (job or self.job)._hire(zone_at(hour), self.seller)

    def work(self, employee):
        """ Have ``employee`` look for work now, and return what they took on. """
        return employee._look_for_work(fields.Datetime.now())

    def in_odoo(self, product, qty):
        """ Odoo says there are ``qty`` more of ``product`` in stock. """
        self.env['stock.quant']._update_available_quantity(product, self.stock_location, qty)

    def ready_order(self, qty=10):
        """ A manufacturing order for clips that Odoo says is ready. """
        self.in_odoo(self.wire, qty * self.bom.bom_line_ids.product_qty)
        order = self.env['mrp.production'].create({
            'product_id': self.clip.id, 'product_qty': qty, 'bom_id': self.bom.id,
        })
        order.action_confirm()
        order.date_start = LONG_AGO
        self.assertEqual(order.reservation_state, 'assigned')
        return order

    def purchased(self, qty=100, extra=None):
        """ Wire ordered from the vendor and shipped: ``(receipt, shipment)``. """
        lines = [Command.create({'product_id': self.wire.id, 'product_qty': qty, 'price_unit': 1})]
        if extra:
            lines.append(Command.create({'product_id': extra.id, 'product_qty': 5, 'price_unit': 1}))
        order = self.env['purchase.order'].create({'partner_id': self.partner.id, 'order_line': lines})
        order.button_confirm()
        self.vendor._cron_process_orders()
        order.picking_ids.scheduled_date = LONG_AGO
        return order.picking_ids, self.env['game.shipment'].search([('purchase_id', '=', order.id)])

    def sold(self, qty=100):
        """ A delivery order for clips to the buyer, that Odoo says is ready. """
        self.in_odoo(self.clip, qty)
        sale = self.env['sale.order'].create({
            'partner_id': self.buyer.id,
            'order_line': [Command.create({'product_id': self.clip.id, 'product_uom_qty': qty})],
        })
        sale.action_confirm()
        sale.picking_ids.scheduled_date = LONG_AGO
        self.assertEqual(sale.picking_ids.state, 'assigned')
        return sale.picking_ids

    def asked_for_pay(self, employee):
        return self.Email.search([('email_from', 'ilike', employee.user_id.email)], order='id')


class TestHiring(EmployeeCase):

    def test_someone_hired_is_someone_in_odoo_too(self):
        employee = self.hire()
        user = employee.user_id

        self.assertEqual((employee.state, employee.job_id, employee.wage), ('employed', self.job, 310))
        self.assertTrue(user.active)
        self.assertFalse(user.share, "an employee of the company")
        self.assertTrue(user.has_group('mrp.group_mrp_user') and user.has_group('stock.group_stock_user'))
        self.assertEqual(user.name, employee.name)
        self.assertTrue(user.email and user.email == user.login)
        self.assertEqual(employee.write_to, self.seller.email, "writes to whoever hired them")

    def test_two_hires_are_two_people(self):
        first, second = self.hire(), self.hire()

        self.assertNotEqual(first.name, second.name)
        self.assertNotEqual(first.user_id.email, second.user_id.email)

    def test_the_wage_is_agreed_when_hired(self):
        employee = self.hire()
        self.job.wage = 1000

        self.assertEqual(employee.wage, 310)

    def test_someone_hired_starts_looking_for_work(self):
        cron = self.env.ref('odoo_sim.ir_cron_employees_work')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        self.hire()

        self.assertGreater(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)

    def test_nobody_hires_through_the_orm(self):
        admin = self.env.ref('base.user_admin')
        for model in ('game.job', 'game.employee', 'game.employee.task'):
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(admin).check_access('write')


class TestWorkingDay(EmployeeCase):

    def test_employees_at_work_take_on_what_is_ready(self):
        self.give(self.wire, 10)
        employee = self.hire(hour=10)
        order = self.ready_order()
        self.Employee._cron_work()

        self.assertEqual(employee.task_ids.production_id, order)

    def test_nobody_works_at_night_and_they_come_in_at_nine(self):
        self.give(self.wire, 10)
        employee = self.hire(hour=20)
        self.ready_order()
        self.Employee._cron_work()

        self.assertFalse(employee.task_ids)
        morning = workday.working_start(fields.Datetime.now(), employee._timezone())
        cron = self.env.ref('odoo_sim.ir_cron_employees_work')
        self.assertTrue(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id), ('call_at', '=', morning)]))

    def test_one_task_at_a_time(self):
        self.give(self.wire, 10)
        self.give(self.clip, 100)
        employee = self.hire()
        self.ready_order()
        self.sold()
        first = self.work(employee)

        self.assertTrue(first)
        self.assertFalse(self.work(employee))
        self.assertEqual(employee.task_ids, first)

    def test_work_left_at_five_carries_on_the_next_morning(self):
        self.give(self.wire, 10)
        employee = self.hire(hour=16)
        self.ready_order(qty=60)
        task = self.work(employee)
        tz = employee._timezone()

        # 120 minutes of work, begun at four: the rest is done the next morning.
        today = workday.local_date(task.date_start, tz)
        before_five = workday.at(today, workday.DAY_END, tz) - task.date_start
        self.assertEqual(task.date_end, workday.at(today + timedelta(days=1), workday.DAY_START, tz)
                         + timedelta(minutes=120) - before_five)
        self.assertEqual(task.run_id.date_end, task.date_end, "the station waits for them too")


class TestManufacturing(EmployeeCase):

    def test_an_employee_makes_a_ready_order_and_marks_it_done(self):
        self.give(self.wire, 10)
        employee = self.hire()
        order = self.ready_order(qty=10)
        task = self.work(employee)

        self.assertEqual((task.kind, task.production_id, task.run_id.production_id), ('manufacture', order, order))
        self.assertEqual(task.date_end, task.date_start + timedelta(minutes=20))
        self.assertEqual(self.on_hand(self.wire), 9, "the components are off the shelf")
        with self.refused("already running"):
            self.station._start(1)

        self.world._settle(task.date_end)
        self.assertEqual((task.state, task.note, task.run_id.state), ('done', False, 'done'))
        self.assertEqual(self.on_hand(self.clip), 10)
        self.assertEqual(order.state, 'done')
        self.assertEqual(order.write_uid, employee.user_id, "Odoo says who did it")

    def test_the_world_consumes_the_recipe_whatever_the_bom_says(self):
        self.bom.bom_line_ids.product_qty = 0.5
        self.give(self.wire, 10)
        employee = self.hire()
        order = self.ready_order(qty=10)
        self.world._settle(self.work(employee).date_end)

        self.assertEqual(self.on_hand(self.wire), 9)
        self.assertEqual(order.move_raw_ids.quantity, 5, "Odoo records what its BoM says")

    def test_without_the_components_in_the_world_nothing_is_made(self):
        """ Odoo says the wire is there. It is not. """
        employee = self.hire()
        order = self.ready_order()

        self.assertFalse(self.work(employee))
        self.assertEqual(order.state, 'confirmed')
        self.assertFalse(self.station.production_ids)

    def test_an_order_odoo_says_is_waiting_is_left_alone(self):
        self.give(self.wire, 10)
        employee = self.hire()
        order = self.env['mrp.production'].create({'product_id': self.clip.id, 'product_qty': 10, 'bom_id': self.bom.id})
        order.action_confirm()

        self.assertEqual(order.reservation_state, 'confirmed')
        self.assertFalse(self.work(employee))

    def test_an_order_cancelled_meanwhile_is_made_anyway_and_not_recorded(self):
        self.give(self.wire, 10)
        employee = self.hire()
        order = self.ready_order()
        task = self.work(employee)
        order.action_cancel()
        self.world._settle(task.date_end)

        self.assertEqual(self.on_hand(self.clip), 10, "the clips exist")
        self.assertIn("not open in Odoo", task.note)

    def test_an_order_is_taken_on_once(self):
        self.give(self.wire, 20)
        employee = self.hire()
        order = self.ready_order()
        task = self.work(employee)
        order.action_cancel()
        self.world._settle(task.date_end)

        self.assertFalse(self.work(employee))

    def test_nobody_starts_what_they_cannot_finish_before_leaving(self):
        self.give(self.wire, 10)
        employee = self.hire()
        self.ready_order(qty=100)
        employee.date_leave = fields.Datetime.now() + timedelta(minutes=30)

        self.assertFalse(self.work(employee))


class TestReceiving(EmployeeCase):

    def test_a_delivery_at_the_door_is_unpacked_and_recorded(self):
        employee = self.hire()
        receipt, shipment = self.purchased(qty=100)
        self.assertEqual(receipt.state, 'assigned')
        self.assertFalse(self.work(employee), "nothing at the door yet")

        self.world._settle(shipment.date_arrival)
        task = self.work(employee)
        self.assertEqual((task.kind, task.picking_id, task.shipment_id), ('receive', receipt, shipment))
        with self.refused("is unpacking"):
            shipment._accept()
        self.assertEqual(self.on_hand(self.wire), 0, "not until it is unpacked")

        self.world._settle(task.date_end)
        self.assertEqual(self.on_hand(self.wire), 100)
        self.assertEqual((shipment.state, shipment.date_accepted), ('accepted', task.date_end))
        self.assertEqual((receipt.state, receipt.move_ids.quantity), ('done', 100))
        self.assertEqual(receipt.write_uid, employee.user_id)

    def test_a_short_delivery_is_recorded_as_it_came(self):
        """ The vendor does not sell clips: they are on the order and not in the box. """
        employee = self.hire()
        receipt, shipment = self.purchased(qty=100, extra=self.clip)
        self.world._settle(shipment.date_arrival)
        self.world._settle(self.work(employee).date_end)

        moves = receipt.move_ids
        self.assertEqual(receipt.state, 'done')
        self.assertEqual(moves.filtered(lambda move: move.product_id == self.wire).quantity, 100)
        self.assertEqual(moves.filtered(lambda move: move.product_id == self.clip).state, 'cancel')
        self.assertFalse(self.env['stock.picking'].search([('backorder_id', '=', receipt.id)]), "no backorder")

    def test_a_delivery_the_player_took_in_is_not_taken_in_again(self):
        employee = self.hire()
        receipt, shipment = self.purchased()
        self.world._settle(shipment.date_arrival)
        shipment._accept()

        self.assertFalse(self.work(employee))
        self.assertEqual(self.on_hand(self.wire), 100)


class TestShipping(EmployeeCase):

    def test_a_delivery_order_is_packed_addressed_posted_and_recorded(self):
        self.give(self.clip, 100)
        employee = self.hire()
        delivery = self.sold(100)
        task = self.work(employee)
        package = task.package_id

        self.assertEqual((task.kind, task.picking_id), ('ship', delivery))
        self.assertEqual((package.state, package.line_ids.product_id, package.line_ids.qty), ('open', self.clip, 100))
        self.assertEqual(self.on_hand(self.clip), 0, "packed")
        with self.refused("is packing"):
            package._send("Somewhere")

        self.world._settle(task.date_end)
        self.assertEqual((package.state, package.address, package.date_posted), ('in_transit', self.address, task.date_end))
        self.assertEqual((delivery.state, delivery.write_uid), ('done', employee.user_id))

        order = self.place()
        self.world._settle(package.date_arrival)
        self.assertEqual((package.state, order.qty_received), ('delivered', 100))

    def test_a_wrong_address_in_odoo_sends_the_box_to_nobody(self):
        self.give(self.clip, 100)
        employee = self.hire()
        self.buyer.street = "2 Test Street"
        self.sold(100)
        task = self.work(employee)
        self.world._settle(task.date_end)
        self.world._settle(task.package_id.date_arrival)
        package = task.package_id

        self.assertEqual(package.state, 'returning')

    def test_no_address_in_odoo_nothing_packed(self):
        self.give(self.clip, 100)
        employee = self.hire()
        self.sold(100)
        self.buyer.write({'street': False, 'city': False, 'zip': False})

        self.assertFalse(self.work(employee))
        self.assertEqual(self.on_hand(self.clip), 100)

    def test_not_enough_in_the_world_nothing_packed(self):
        self.give(self.clip, 99)
        employee = self.hire()
        self.sold(100)
        Package = self.env['game.package']
        packages = Package.search_count([])

        self.assertFalse(self.work(employee))
        self.assertEqual((self.on_hand(self.clip), Package.search_count([])), (99, packages))


class TestPay(EmployeeCase):

    def hired_on(self, day):
        """ Someone hired at ten in the morning, UTC, on ``day``. """
        employee = self.hire()
        employee.write({'tz': 'UTC', 'date_start': datetime.combine(day, time(10))})
        employee._schedule_pay()
        return employee

    def test_hired_on_the_fifth_and_never_paid_they_leave_on_the_fifteenth_of_next_month(self):
        employee = self.hired_on(date(2030, 1, 5))

        self.assertEqual(employee._unpaid(), (date(2030, 1, 31), 270), "27 days of 31")
        self.assertEqual(employee.date_chase, datetime(2030, 2, 1, 9))
        self.assertEqual(employee.date_leave, datetime(2030, 2, 15, 9))

    def test_they_ask_for_their_salary_then_leave_without_it(self):
        employee = self.hired_on(date(2030, 1, 5))
        self.world._settle(datetime(2030, 2, 1, 9) - timedelta(seconds=1))
        self.assertFalse(self.asked_for_pay(employee))

        self.world._settle(datetime(2030, 2, 1, 9))
        [first] = self.asked_for_pay(employee)
        self.assertEqual(first.subject, "My salary for January 2030")
        self.assertEqual(first.delivery_ids.address, self.seller.email)
        body = first._content()['body']
        self.assertIn("was due on 31 January", body)
        self.assertIn("by 15 February", body)
        self.assertEqual(employee.date_chase, datetime(2030, 2, 8, 9))

        self.world._settle(datetime(2030, 2, 8, 9))
        first, second = self.asked_for_pay(employee)
        self.assertEqual(second.in_reply_to, first.message_id)
        self.assertFalse(employee.date_chase, "the next reminder would be the day they leave")

        self.world._settle(datetime(2030, 2, 15, 9))
        *_asked, goodbye = self.asked_for_pay(employee)
        self.assertIn("I am leaving today", goodbye._content()['body'])
        self.assertEqual((employee.state, employee.date_left), ('gone', datetime(2030, 2, 15, 9)))
        self.assertTrue(employee.user_id.active, "archiving their user is up to the player")
        self.assertNotIn(employee.id, [shown['id'] for shown in self.world._snapshot()['employees']])

    def test_someone_who_has_been_paid_waits_for_the_next_month(self):
        employee = self.hired_on(date(2030, 1, 5))
        self.accounts._deposit(self.env.company.partner_id.id, 1000)
        self.company_account._pay(self.accounts._of(employee.partner_id), 270, "January")
        self.world._settle(datetime(2030, 2, 1, 9))

        self.assertFalse(self.asked_for_pay(employee))
        self.assertEqual((employee.date_chase, employee.date_leave), (datetime(2030, 3, 1, 9), datetime(2030, 3, 15, 9)))

    def test_nobody_leaves_once_paid(self):
        employee = self.hired_on(date(2030, 1, 5))
        self.world._settle(datetime(2030, 2, 1, 9))
        self.accounts._deposit(self.env.company.partner_id.id, 1000)
        self.company_account._pay(self.accounts._of(employee.partner_id), 270, "January")
        self.world._settle(datetime(2030, 2, 15, 9))

        self.assertEqual(employee.state, 'employed')
        self.assertEqual(employee.date_leave, datetime(2030, 3, 15, 9))


class TestEmployeeSnapshot(EmployeeCase):

    def test_the_page_sees_jobs_employees_and_who_is_doing_what(self):
        self.give(self.wire, 10)
        employee = self.hire()
        order = self.ready_order()
        task = self.work(employee)
        snapshot = self.world._snapshot()

        [job] = [job for job in snapshot['jobs'] if job['id'] == self.job.id]
        self.assertEqual((job['name'], job['wage']), ("Test worker", 310))
        [shown] = [shown for shown in snapshot['employees'] if shown['id'] == employee.id]
        self.assertEqual(shown['task']['text'], f"Making 10 Test clip for {order.name}")
        self.assertEqual(shown['task']['date_end'], task.date_end.isoformat())
        self.assertEqual(shown['date_leave'], employee.date_leave.isoformat())
        [station] = [station for station in snapshot['workstations'] if station['id'] == self.station.id]
        self.assertEqual(station['run']['worker'], employee.name)

    def test_the_page_sees_who_is_unpacking_and_packing(self):
        self.give(self.clip, 100)
        unpacker, packer = self.hire(), self.hire()
        _receipt, shipment = self.purchased()
        self.world._settle(shipment.date_arrival)
        self.work(unpacker)
        self.sold()
        package = self.work(packer).package_id
        snapshot = self.world._snapshot()

        [door] = [shown for shown in snapshot['shipments'] if shown['id'] == shipment.id]
        [bench] = [shown for shown in snapshot['packages'] if shown['id'] == package.id]
        self.assertEqual((door['worker'], bench['worker']), (unpacker.name, packer.name))


@tagged('-at_install', 'post_install')
class TestEmployeeApi(EmployeeCase, HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')

    def hire_url(self, job, body=None):
        return self.url_open(f'/game/api/jobs/{job}/hire', json=body or {}, method='POST')

    def test_hiring_someone_from_the_page(self):
        before = set(self.Employee.search([]).ids)
        response = self.hire_url(self.job.id, {'tz': 'Asia/Tokyo'})

        self.assertEqual(response.status_code, 200, response.text)
        [employee] = self.Employee.search([('id', 'not in', list(before))])
        self.assertEqual((employee.tz, employee.job_id), ('Asia/Tokyo', self.job))
        self.assertIn(employee.id, [shown['id'] for shown in response.json()['employees']])

    def test_nobody_is_hired_in_a_paused_world_or_for_no_job(self):
        self.assertEqual(self.hire_url(999999999).status_code, 404)
        self.world_at(paused=True)
        response = self.hire_url(self.job.id)
        self.assertEqual(response.status_code, 422)
        self.assertIn("not running", response.json()['message'])

    def test_hiring_is_for_employees(self):
        new_test_user(self.env, 'portal_boss', groups='base.group_portal', password='portal-password')
        self.authenticate('portal_boss', 'portal-password')

        self.assertEqual(self.hire_url(self.job.id).status_code, 403)
