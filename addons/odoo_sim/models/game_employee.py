# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Employees: people the company hires, who do the work at the bench and the door.

An employee is an agent, like a customer.  It looks in Odoo for what is to be
done -- a manufacturing order Odoo says is ready, a receipt or a delivery order
Odoo says is ready -- and does it in the world, with the world's own rules: a
run at a workstation consumes the recipe, not the BoM, and nothing is packed
that is not on the shelves.  Then it records what it did in Odoo, as itself.

It works one task at a time, from nine to five, and stops at five whatever it
is doing, to carry on the next morning.  It is hired on a monthly wage, due at
the end of each month, and writes to ask for it when it is late; unpaid for
fifteen days, it leaves.  See ``odoo_sim/EMPLOYEES.md``.
"""
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import Command, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, email_normalize, format_amount, format_date

from odoo.addons.odoo_sim import utils, workday

_logger = logging.getLogger(__name__)

#: Game minutes to pack a delivery order and address the box.
PACK_MINUTES = 5.0

#: Game minutes to unpack a delivery at the door and put it on the shelves.
UNPACK_MINUTES = 5.0

#: How often an employee who has not been paid asks again.
REMIND_EVERY = timedelta(days=7)

#: How long after a salary falls due an employee who has not had it leaves.
LEAVES_AFTER = timedelta(days=15)

#: Manufacturing orders an employee takes on, once Odoo says they are ready.
WORKABLE_MO_STATES = ('confirmed', 'progress')

#: What an employee may do in Odoo.
GROUPS = ('base.group_user', 'stock.group_stock_user', 'mrp.group_mrp_user')

FIRST_NAMES = [
    "Alice", "Bruno", "Chloe", "Dmitri", "Esther", "Farid", "Greta", "Hugo", "Ines", "Jonas",
    "Keiko", "Luca", "Mina", "Nils", "Olga", "Pablo", "Quinn", "Rosa", "Samir", "Tove",
]
LAST_NAMES = [
    "Adler", "Brooks", "Castro", "Dufour", "Eriksen", "Fischer", "Garcia", "Haddad", "Ivanova",
    "Janssen", "Kowalski", "Lambert", "Moreau", "Novak", "Okafor", "Peeters", "Rossi",
]


class GameJob(models.Model):
    """A position the company can hire for, and what it pays."""
    _name = 'game.job'
    _description = "Game world: job"
    _order = 'id'

    name = fields.Char(required=True)
    wage = fields.Monetary(
        "Monthly wage", required=True,
        help="What someone hired for this job is paid for a full month.")
    currency_id = fields.Many2one(
        'res.currency', required=True, default=lambda self: self.env.company.currency_id)
    employee_ids = fields.One2many('game.employee', 'job_id', "Employees")

    _wage_positive = models.Constraint('CHECK(wage >= 0)', "A wage is not negative.")

    def _hire(self, tz, hired_by=None):
        """ Hire someone for this job, starting now, in time zone ``tz``.

        They come with an Odoo user of their own, which is who Odoo says did
        what they record, and an address at the company.  Their contract is
        the world's, not Odoo's: the wage as it stands today, paid monthly.
        They write to ``hired_by`` about it.
        """
        self.ensure_one()
        Employee = self.env['game.employee']
        name, email = Employee._new_identity()
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': name,
            'login': email,
            'email': email,
            'tz': tz.zone,
            'group_ids': [Command.set([self.env.ref(xmlid).id for xmlid in GROUPS])],
        })
        employee = Employee.create({
            'name': name,
            'user_id': user.id,
            'job_id': self.id,
            'wage': self.wage,
            'currency_id': self.currency_id.id,
            'tz': tz.zone,
            'date_start': fields.Datetime.now(),
            'write_to': hired_by.email if hired_by and hired_by.email else False,
        })
        employee._schedule_pay()
        Employee._trigger_work()
        self.env['game.world']._changed()
        return employee


class GameEmployee(models.Model):
    """Someone the company employs: they look for work in Odoo, and do it in the world."""
    _name = 'game.employee'
    _description = "Game world: employee"
    _order = 'date_start, id'

    name = fields.Char(required=True, readonly=True)
    user_id = fields.Many2one(
        'res.users', "Odoo user", required=True, readonly=True, ondelete='restrict',
        help="Who they are in Odoo: what they record there, Odoo says they did.")
    partner_id = fields.Many2one(related='user_id.partner_id')
    job_id = fields.Many2one('game.job', required=True, readonly=True, index=True, ondelete='restrict')
    wage = fields.Monetary(
        "Monthly wage", required=True, readonly=True,
        help="As agreed when they were hired, for a full month.")
    currency_id = fields.Many2one('res.currency', required=True, readonly=True)
    tz = fields.Char(
        "Time zone", required=True, readonly=True,
        help="Where their working day is from nine to five, and their months end.")
    date_start = fields.Datetime("Hired", required=True, readonly=True)
    write_to = fields.Char(
        "Writes to", readonly=True,
        help="The address of whoever hired them: where they write about their pay.")
    state = fields.Selection([
        ('employed', "Employed"),
        ('gone', "Left"),
    ], required=True, readonly=True, default='employed', index=True)
    date_left = fields.Datetime("Left", readonly=True)
    date_chase = fields.Datetime(
        "Asks for pay at", readonly=True, index=True,
        help="When they next write about a salary that has not come.")
    date_leave = fields.Datetime(
        "Leaves at", readonly=True, index=True,
        help="When they leave, if the salary they are owed has still not come.")
    reminder_count = fields.Integer("Reminders", readonly=True)
    email_id = fields.Many2one(
        'game.email', "Asked in", readonly=True, ondelete='set null',
        help="The first email about their pay, which the others follow up.")
    task_ids = fields.One2many('game.employee.task', 'employee_id', "Tasks")

    # -- hiring ----------------------------------------------------------------

    @api.model
    def _new_identity(self):
        """ ``(name, email)`` for the next person hired: a name, and an address at the company. """
        count = self.search_count([])
        first = FIRST_NAMES[count % len(FIRST_NAMES)]
        last = LAST_NAMES[(count * 7 + 3) % len(LAST_NAMES)]
        local, domain = f"{first}.{last}".lower(), self._email_domain()
        Users = self.env['res.users'].with_context(active_test=False)
        email, n = f"{local}@{domain}", 1
        while Users.search_count(['|', ('login', '=', email), ('email_normalized', '=', email)], limit=1):
            n += 1
            email = f"{local}{n}@{domain}"
        return f"{first} {last}", email

    @api.model
    def _email_domain(self):
        company = self.env.company
        if company.alias_domain_id:
            return company.alias_domain_id.name
        if address := email_normalize(company.email or ''):
            return address.rpartition('@')[2]
        return 'example.com'

    def _timezone(self):
        self.ensure_one()
        return workday.timezone(self.tz)

    # -- looking for work ------------------------------------------------------

    @api.model
    def _trigger_work(self, at=None):
        """ Have employees look for work, now or at ``at`` (once per instant). """
        cron = self.env.ref('odoo_sim.ir_cron_employees_work', raise_if_not_found=False)
        if not cron:
            return
        if at is not None and self.env['ir.cron.trigger'].sudo().search_count(
            [('cron_id', '=', cron.id), ('call_at', '=', at)], limit=1,
        ):
            return
        cron._trigger(at)

    @api.model
    def _cron_work(self):
        """ The employee agent: everyone at work and free takes on something they can do.

        Woken whenever the world changes (``game.world._changed``), whenever
        Odoo may have something new ready (``stock_move.py``), when someone is
        hired, and at the start of each working day.  Employees off work wake
        it again for their next morning.
        """
        now = fields.Datetime.now()
        self.env['game.world']._settle(now)
        for employee in self.search([('state', '=', 'employed')]):
            tz = employee._timezone()
            if workday.is_working(now, tz):
                employee._look_for_work(now)
            else:
                self._trigger_work(workday.working_start(now, tz))

    def _look_for_work(self, now):
        """ Take on the first task this employee can do now, if they are free.  Returns the task. """
        self.ensure_one()
        Task = self.env['game.employee.task']
        # Two agents at once must not both find the employee free.
        self.env.cr.execute(SQL("SELECT id FROM game_employee WHERE id = %s FOR UPDATE", self.id))
        if Task.search_count([('employee_id', '=', self.id), ('state', '=', 'working')], limit=1):
            return Task
        for _date, _id, start, record in self._work_to_do():
            # What an employee cannot do is seen before trying, not by rolling
            # back: rolling back a savepoint throws away the transaction's
            # queued notices.  The savepoint is for what could not be foreseen.
            try:
                with self.env.cr.savepoint():
                    task = start(record, now)
            except UserError as error:
                _logger.info("%s could not take on %s: %s", self.name, record.display_name, error)
                continue
            if task:
                return task
        return Task

    def _work_to_do(self):
        """ What Odoo says is ready, and nobody has taken on: ``(date, id, start, record)``, oldest first.

        Something taken on once is never taken on again, even if recording it
        in Odoo failed and Odoo still shows it as ready: the work was done.
        """
        Task = self.env['game.employee.task']
        stations = self.env['game.workstation'].search([])
        orders = self.env['mrp.production'].search([
            ('state', 'in', WORKABLE_MO_STATES),
            ('reservation_state', '=', 'assigned'),
            ('product_id', 'in', stations.product_id.ids),
            ('id', 'not in', Task._search([('production_id', '!=', False)]).subselect('production_id')),
        ])
        taken = Task._search([('picking_id', '!=', False)]).subselect('picking_id')
        at_the_door = self.env['game.shipment'].search([('state', '=', 'arrived'), ('purchase_id', '!=', False)])
        receipts = self.env['stock.picking'].search([
            ('picking_type_code', '=', 'incoming'),
            ('state', '=', 'assigned'),
            ('purchase_id', 'in', at_the_door.purchase_id.ids),
            ('id', 'not in', taken),
        ])
        deliveries = self.env['stock.picking'].search([
            ('picking_type_code', '=', 'outgoing'),
            ('state', '=', 'assigned'),
            ('id', 'not in', taken),
        ])
        work = [(order.date_start, order.id, self._start_making, order) for order in orders]
        work += [(picking.scheduled_date, picking.id, self._start_receiving, picking) for picking in receipts]
        work += [(picking.scheduled_date, picking.id, self._start_shipping, picking) for picking in deliveries]
        return sorted(work, key=lambda item: (item[0] or datetime.max, item[1]))

    def _work_until(self, now, minutes):
        """ When ``minutes`` of work begun ``now`` end, or None if this employee will have left by then. """
        end = workday.add_working_time(now, timedelta(minutes=minutes), self._timezone())
        if self.date_leave and end > self.date_leave:
            return None
        return end

    def _start_making(self, order, now):
        """ Make what ``order`` asks for, at a free workstation that makes it. """
        qty = order.product_uom_qty
        running = self.env['game.production'].search([('state', '=', 'running')])
        station = self.env['game.workstation'].search([
            ('recipe_id.product_id', '=', order.product_id.id),
            ('id', 'not in', running.workstation_id.ids),
        ], limit=1)
        if not station:
            return None
        needed = {line.product_id: line.qty * qty for line in station.recipe_id.line_ids}
        end = self._work_until(now, station.recipe_id.duration * qty)
        if end is None or not self.env['game.stock']._holds(needed):
            return None
        run = station._start(qty, order, date_end=end)
        return self._take_on('manufacture', now, end, production_id=order.id, run_id=run.id)

    def _start_receiving(self, receipt, now):
        """ Unpack the delivery at the door that ``receipt`` is for. """
        Task = self.env['game.employee.task']
        shipment = self.env['game.shipment'].search([
            ('state', '=', 'arrived'), ('purchase_id', '=', receipt.purchase_id.id),
        ]).filtered(lambda shipment: not Task._busy_with(shipment=shipment))[:1]
        end = shipment and self._work_until(now, UNPACK_MINUTES)
        if not end:
            return None
        shipment._at_the_door()
        return self._take_on('receive', now, end, picking_id=receipt.id, shipment_id=shipment.id)

    def _start_shipping(self, delivery, now):
        """ Pack what ``delivery`` asks for, and write the address Odoo has for it on the box. """
        address = self._address_on(delivery.partner_id)
        needed = defaultdict(float)
        for move in delivery.move_ids.filtered(lambda move: move.state not in ('done', 'cancel')):
            needed[move.product_id] += move.product_qty
        end = address and needed and self._work_until(now, PACK_MINUTES)
        if not end or not self.env['game.stock']._holds(needed):
            return None
        package = self.env['game.package']._new()
        for product, qty in needed.items():
            package._pack(product, qty)
        return self._take_on('ship', now, end, picking_id=delivery.id, package_id=package.id, address=address)

    @api.model
    def _address_on(self, partner):
        """ What an employee writes on a box for ``partner``: its name, and the address Odoo has for it.

        Odoo's copy, the player's: if it is wrong, the box comes back.  Empty
        if Odoo has no address at all.
        """
        if not partner or not (partner.street or partner.street2 or partner.city or partner.zip):
            return ''
        text = f"{partner.commercial_company_name or partner.name or ''}\n{partner._display_address(without_company=True)}"
        # Odoo's formats leave gaps where a field is empty ("Testville  1000"): nobody writes those.
        return '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())

    def _take_on(self, kind, now, end, **values):
        task = self.env['game.employee.task'].create(dict(
            values, employee_id=self.id, kind=kind, date_start=now, date_end=end,
        ))
        self.env['game.world']._schedule_settle(end)
        self.env['game.world']._changed()
        return task

    # -- pay -------------------------------------------------------------------

    def _salaries(self):
        """ Every salary this employee is owed, from the month they were hired: ``(last day, amount)``.

        Due at the end of each month, in their time zone.  The first month is
        paid for the days from their first day, that day included.  Endless.
        """
        self.ensure_one()
        tz = self._timezone()
        first_day = workday.local_date(self.date_start, tz)
        month = first_day.replace(day=1)
        while True:
            last_day = month + relativedelta(months=1, days=-1)
            days = (last_day - max(month, first_day)).days + 1
            yield last_day, self.currency_id.round(self.wage * days / last_day.day)
            month += relativedelta(months=1)

    def _paid(self):
        """ How much the company has paid into this employee's account at the game bank. """
        self.ensure_one()
        accounts = self.env['game.bank.account']
        mine = accounts.search([('partner_id', '=', self.partner_id.id)], limit=1)
        company = accounts.search([('partner_id', '=', self.env.company.partner_id.id)], limit=1)
        if not (mine and company):
            return 0.0
        return sum(self.env['game.bank.transaction'].search([
            ('payer_id', '=', company.id), ('payee_id', '=', mine.id),
        ]).mapped('amount'))

    def _unpaid(self):
        """ ``(last day of the month, amount owed)`` for the first month not fully paid, or None.

        Payments go to the oldest salaries first.  None only for someone who
        works for nothing, or has been paid a century ahead.
        """
        self.ensure_one()
        paid, owed = self._paid(), 0.0
        for count, (last_day, amount) in enumerate(self._salaries()):
            owed += amount
            if self.currency_id.compare_amounts(owed, paid) > 0:
                return last_day, owed - paid
            if count > 1200:
                return None
        return None

    def _schedule_pay(self):
        """ When each employee will ask for the salary they are owed, and when they will leave without it. """
        for employee in self:
            unpaid = employee._unpaid()
            if not unpaid:
                employee.write({'date_chase': False, 'date_leave': False})
                continue
            tz, last_day = employee._timezone(), unpaid[0]
            employee.write({
                'date_chase': workday.at(last_day + timedelta(days=1), workday.DAY_START, tz),
                'date_leave': workday.at(last_day + LEAVES_AFTER, workday.DAY_START, tz),
            })
            self.env['game.world']._schedule_settle([employee.date_chase, employee.date_leave])

    def _still_unpaid(self):
        """ The month this employee is still waiting to be paid for, or None if paid since it was scheduled. """
        self.ensure_one()
        unpaid = self._unpaid()
        if unpaid and workday.at(unpaid[0] + LEAVES_AFTER, workday.DAY_START, self._timezone()) <= self.date_leave:
            return unpaid
        self._schedule_pay()
        return None

    def _ask_for_pay(self):
        """ Their salary is late: each writes to ask for it, and says when they will leave without it.

        Due at ``date_chase`` (settled by ``game.world._settle``), then every
        :data:`REMIND_EVERY` until they leave.
        """
        for employee in self:
            unpaid = employee._still_unpaid()
            if not unpaid:
                continue
            last_day, amount = unpaid
            month = format_date(self.env, last_day, date_format='MMMM y')
            leaving = format_date(self.env, workday.local_date(employee.date_leave, employee._timezone()),
                                  date_format='d MMMM')
            if employee.reminder_count:
                text = self.env._(
                    "I have still not been paid %(amount)s for %(month)s. If I have not been paid by "
                    "%(leaving)s, I will not be coming in any more.",
                    amount=format_amount(self.env, amount, employee.currency_id), month=month, leaving=leaving)
            else:
                text = self.env._(
                    "My salary for %(month)s, %(amount)s, was due on %(due)s, and I have not been paid. "
                    "If I have not been paid by %(leaving)s, I will not be coming in any more.",
                    month=month, amount=format_amount(self.env, amount, employee.currency_id),
                    due=format_date(self.env, last_day, date_format='d MMMM'), leaving=leaving)
            email = employee._write_to_company(self.env._("My salary for %s", month), text)
            chase = employee.date_chase + REMIND_EVERY
            employee.write({
                'reminder_count': employee.reminder_count + 1,
                'email_id': (employee.email_id or email).id,
                'date_chase': chase if chase < employee.date_leave else False,
            })
            if employee.date_chase:
                self.env['game.world']._schedule_settle(employee.date_chase)
        if self:
            self.env['game.world']._changed()

    def _leave_unpaid(self):
        """ Unpaid for too long: each leaves, as of ``date_leave``, and says so. """
        for employee in self:
            unpaid = employee._still_unpaid()
            if not unpaid:
                continue
            month = format_date(self.env, unpaid[0], date_format='MMMM y')
            employee._write_to_company(
                self.env._("My salary for %s", month),
                self.env._("I have still not been paid for %(month)s, so I am leaving today.", month=month),
            )
            # Their Odoo user stays as it is: archiving it is the player's call.
            employee.write({
                'state': 'gone', 'date_left': employee.date_leave, 'date_chase': False, 'date_leave': False,
            })
        if self:
            self.env['game.world']._changed()

    def _write_to_company(self, subject, text):
        """ Email whoever hired this employee, following up their first email about pay. """
        self.ensure_one()
        if not self.write_to:
            _logger.warning("%s has nobody to write to, and did not send %r", self.name, subject)
            return self.env['game.email']
        parent = self.email_id
        body = Markup("<p>%s</p><p>%s</p><p>%s</p>") % (self.env._("Hello,"), text, self.name)
        try:
            with self.env.cr.savepoint():
                return self.env['game.email']._send_from(
                    self.partner_id, self.write_to, f"Re: {subject}" if parent else subject, body,
                    parent=parent or None,
                )
        except UserError as error:
            _logger.warning("%s could not write to %s: %s", self.name, self.write_to, error)
        return self.env['game.email']

    # -- what the page shows ---------------------------------------------------

    def _shown(self, now):
        """ This employee as the page shows them, as of ``now``. """
        self.ensure_one()
        tz = self._timezone()
        task = self.task_ids.filtered(lambda task: task.state == 'working')[:1]
        unpaid = self._unpaid()
        return {
            'id': self.id,
            'name': self.name,
            'job': self.job_id.name,
            'wage': self.wage,
            'currency': self.currency_id.name,
            'date_start': utils.instant(self.date_start),
            # The page's clock moves on between snapshots: it tells "not in
            # yet" from "gone home" with these two.
            'shift_start': utils.instant(workday.working_start(now, tz)),
            'shift_end': utils.instant(workday.day_end(now, tz)),
            'task': {
                'text': task.display_name,
                'date_start': utils.instant(task.date_start),
                'date_end': utils.instant(task.date_end),
            } if task else None,
            'salary_due': utils.instant(workday.at(unpaid[0] + timedelta(days=1), time(0), tz)) if unpaid else None,
            'date_leave': utils.instant(self.date_leave),
        }


class GameEmployeeTask(models.Model):
    """Something an employee took on: what they did in the world, and whether Odoo took the record of it."""
    _name = 'game.employee.task'
    _description = "Game world: employee task"
    _order = 'date_start desc, id desc'

    employee_id = fields.Many2one('game.employee', required=True, readonly=True, index=True, ondelete='restrict')
    kind = fields.Selection([
        ('manufacture', "Manufacture"),
        ('receive', "Receive"),
        ('ship', "Ship"),
    ], required=True, readonly=True)
    state = fields.Selection([
        ('working', "Working"),
        ('done', "Done"),
    ], required=True, readonly=True, default='working', index=True)
    date_start = fields.Datetime(required=True, readonly=True)
    date_end = fields.Datetime(
        required=True, readonly=True, index=True,
        help="When the work is done: working time only, so work left at five carries on at nine.")
    production_id = fields.Many2one(
        'mrp.production', "Manufacturing order", readonly=True, index='btree_not_null', ondelete='set null')
    run_id = fields.Many2one('game.production', "Run", readonly=True, index='btree_not_null', ondelete='restrict')
    picking_id = fields.Many2one(
        'stock.picking', "Transfer", readonly=True, index='btree_not_null', ondelete='set null')
    shipment_id = fields.Many2one(
        'game.shipment', "Delivery unpacked", readonly=True, index='btree_not_null', ondelete='restrict')
    package_id = fields.Many2one(
        'game.package', "Package packed", readonly=True, index='btree_not_null', ondelete='restrict')
    address = fields.Text("Address written", readonly=True)
    note = fields.Char(
        "Not recorded because", readonly=True,
        help="Why the employee could not record in Odoo what they did in the world.")

    # Taken on once, ever: what makes looking for work idempotent.
    _production_once = models.UniqueIndex(
        '(production_id) WHERE production_id IS NOT NULL', "A manufacturing order is taken on once.")
    _picking_once = models.UniqueIndex(
        '(picking_id) WHERE picking_id IS NOT NULL', "A transfer is taken on once.")

    def _compute_display_name(self):
        for task in self:
            if task.kind == 'manufacture':
                run = task.run_id
                task.display_name = self.env._(
                    "Making %(qty)s %(product)s for %(order)s",
                    qty=utils.quantity(run.qty), product=run.product_id.display_name,
                    order=task.production_id.name or self.env._("an order"))
            elif task.kind == 'receive':
                task.display_name = self.env._("Unpacking %s", task.shipment_id.display_name)
            else:
                task.display_name = self.env._(
                    "Packing %s", task.picking_id.name or task.package_id.display_name)

    @api.model
    def _busy_with(self, *, shipment=None, package=None):
        """ The employee working on ``shipment`` or ``package`` right now, if anyone is. """
        domain = [('state', '=', 'working')]
        if shipment is not None:
            domain.append(('shipment_id', '=', shipment.id))
        if package is not None:
            domain.append(('package_id', '=', package.id))
        return self.search(domain, limit=1).employee_id

    # -- done: settled when due (game.world._settle) ---------------------------

    def _finish(self):
        """ The work is done, as of ``date_end``: finish it in the world, then record it in Odoo.

        A run has finished on its own by then (``game.world._settle`` finishes
        runs first).  A delivery unpacked is on the shelves; a box packed goes
        to the post, with the address the employee wrote on it.
        """
        for task in self:
            if task.kind == 'receive':
                task.shipment_id._take_in(task.date_end)
            elif task.kind == 'ship':
                task.package_id._hand_to_post(task.address, task.date_end)
            task.state = 'done'
            task._record()
        if self:
            self.env['game.world']._changed()

    def _record(self):
        """ Record the work in Odoo, as the employee.  A refusal is noted, and changes nothing in the world. """
        self.ensure_one()
        as_employee = self.with_user(self.employee_id.user_id).sudo()
        try:
            with self.env.cr.savepoint():
                note = getattr(as_employee, f'_record_{self.kind}')()
        except Exception as error:  # noqa: BLE001 - the world has happened whatever Odoo says
            _logger.info("%s could not record %s in Odoo: %s", self.employee_id.name, self.display_name, error)
            note = str(error)
        if note:
            self.note = note

    def _record_manufacture(self):
        order = self.production_id
        if order.state not in WORKABLE_MO_STATES + ('to_close',):
            return self.env._("%s is not open in Odoo any more.", order.name or self.env._("The order"))
        qty = self.run_id.product_id.uom_id._compute_quantity(self.run_id.qty, order.product_uom_id)
        order.qty_producing = qty
        order._set_qty_producing()
        order.move_raw_ids.filtered(lambda move: move.state not in ('done', 'cancel')).picked = True
        context = {'skip_consumption': True, 'skip_backorder': True, 'skip_redirection': True}
        if order.product_uom_id.compare(qty, order.product_qty) < 0:
            context['mo_ids_to_backorder'] = order.ids
        order.with_context(**context).button_mark_done()
        if order.state != 'done':
            raise UserError(self.env._("Odoo did not mark %s done.", order.name))
        return None

    def _record_receive(self):
        return self._validate({line.product_id: line.qty for line in self.shipment_id.line_ids})

    def _record_ship(self):
        return self._validate({line.product_id: line.qty for line in self.package_id.line_ids})

    def _validate(self, quantities):
        """ Validate the transfer with ``{product: qty}`` done, in each product's unit, and no backorder.

        Each product's quantity fills its moves' demand in order, and whatever
        is left over goes on its last move: an employee records what they
        counted, not what was asked.
        """
        picking = self.picking_id
        if picking.state in ('done', 'cancel'):
            return self.env._("%s is not open in Odoo any more.", picking.name or self.env._("The transfer"))
        moves = picking.move_ids.filtered(lambda move: move.state not in ('done', 'cancel'))
        for product, product_moves in moves.grouped('product_id').items():
            left = quantities.get(product, 0.0)
            for move in product_moves:
                qty = left if move == product_moves[-1] else min(left, move.product_qty)
                left -= qty
                move.quantity = product.uom_id._compute_quantity(qty, move.product_uom)
        moves.picked = True
        picking.with_context(skip_backorder=True, picking_ids_not_to_backorder=picking.ids).button_validate()
        if picking.state != 'done':
            raise UserError(self.env._("Odoo did not validate %s.", picking.name))
        return None
