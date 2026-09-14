# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the odoo-sim game loop (see odoo_sim/DESIGN.md sections 3.3, 4.3).

The threading itself is not covered -- a stalled clock thread would still emit
well-formed pulses and pass everything here. What is covered is the bookkeeping
each tick has to get right, which is where the silent failures live.
"""
import threading
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import game_clock
from odoo.addons.base.models.ir_cron import IrCron
from odoo.addons.odoo_sim import loop
from odoo.game_clock import GameClock
from odoo.tests.common import TransactionCase
from odoo.tools import config

_MISSING = object()


class TestTickHeadroom(TransactionCase):
    """A tick must fit inside the world's clamp with room to spare."""

    def test_comfortable_tick_is_accepted(self):
        self.assertIsNone(loop.tick_headroom_error(1.0, timedelta(seconds=5)))

    def test_tick_at_the_limit_is_refused(self):
        """ A tick of exactly half the clamp has no room for one late tick. """
        self.assertIsNone(loop.tick_headroom_error(2.5, timedelta(seconds=5)))
        self.assertIsNotNone(loop.tick_headroom_error(2.6, timedelta(seconds=5)))

    def test_tick_longer_than_the_clamp_is_refused(self):
        """ The pathological case: every tick arrives after the world froze. """
        error = loop.tick_headroom_error(10.0, timedelta(seconds=5))
        self.assertIsNotNone(error)
        self.assertIn('max_gap', error)


class TestWorkersGuard(TransactionCase):
    """Prefork cannot host the loop: the master forks out from under it."""

    def test_threaded_mode_is_fine(self):
        self.assertIsNone(loop.unsupported_workers_error(0))

    def test_prefork_is_refused(self):
        error = loop.unsupported_workers_error(4)
        self.assertIsNotNone(error, "a prefork master would fork the loop's threads")
        self.assertIn('--max-cron-threads=0', error, "says what to do instead")


class TestRetentionWarning(TransactionCase):
    """DESIGN.md 5.12: the bus backlog is denominated in game seconds."""

    def test_default_retention_is_a_minute_at_k1440(self):
        warning = loop.retention_warning(60 * 60 * 24, 1440)
        self.assertIsNotNone(warning, "24 game hours is 60 real seconds at K=1440")
        self.assertIn('0:01:00', warning)
        self.assertIn(str(3600 * 1440), warning, "tells the operator what to set")

    def test_generous_retention_is_quiet(self):
        """ Sized in game seconds for a real window, there is nothing to say. """
        self.assertIsNone(loop.retention_warning(3600 * 1440, 1440))

    def test_a_slow_world_needs_no_warning(self):
        """ At K=1, game seconds are real seconds and the default is fine. """
        self.assertIsNone(loop.retention_warning(60 * 60 * 24, 1))


class TestCronTickBookkeeping(TransactionCase):
    """What a cron tick has to put back, and what would break silently.

    These run against ``threading.current_thread()`` on purpose. ``_process_jobs``
    reaches for the running thread itself, so testing through an injected
    stand-in would pass whether or not the loop restores anything -- which is
    exactly the mistake the first version of this file made.
    """

    def setUp(self):
        super().setUp()
        self.loop = loop.GameLoop(self.env.cr.dbname, 1.0, 1.0)
        thread = threading.current_thread()
        saved = {a: getattr(thread, a, _MISSING) for a in ('type', 'start_time', 'dbname')}
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved):
        thread = threading.current_thread()
        for attr, value in saved.items():
            if value is _MISSING:
                if hasattr(thread, attr):
                    delattr(thread, attr)
            else:
                setattr(thread, attr, value)

    def _no_ready_jobs(self):
        return patch.object(IrCron, '_get_all_ready_jobs', staticmethod(lambda cr: []))

    def test_mark_cron_thread_tags_it_for_the_watchdog(self):
        """ process_limit() ignores threads without both of these. """
        self.loop.mark_cron_thread()
        thread = threading.current_thread()
        self.assertEqual(thread.type, 'cron')
        self.assertIsNone(thread.start_time)
        self.assertEqual(thread.dbname, self.env.cr.dbname)

    def test_tick_restores_thread_dbname(self):
        """ Regression for DESIGN.md 5.8, and the reason the loop re-sets it.

        _process_jobs deletes thread.dbname on the way out. If the loop does not
        put it back, game_clock.current_clock() falls through to config and this
        thread silently reverts to *real* time -- while still running, still
        firing crons, and reporting nothing wrong.
        """
        self.loop.mark_cron_thread()
        with self._no_ready_jobs():
            self.loop.run_cron_tick()
        self.assertEqual(
            getattr(threading.current_thread(), 'dbname', None), self.env.cr.dbname,
            "the tick must put back the dbname _process_jobs deleted",
        )

    def test_dbname_survives_many_ticks(self):
        """ The failure is on the *first* tick, so one tick is not enough. """
        self.loop.mark_cron_thread()
        for i in range(3):
            with self._no_ready_jobs():
                self.loop.run_cron_tick()
            self.assertEqual(
                getattr(threading.current_thread(), 'dbname', None),
                self.env.cr.dbname, f"lost after tick {i + 1}",
            )

    def test_the_clock_still_reads_game_time_after_a_tick(self):
        """ The consequence, rather than the mechanism.

        current_clock() falls back to a single configured database, which is
        why 5.8 calls the deleted dbname "harmless under -d <db>". That fallback
        also makes the naive version of this test pass whether or not the tick
        restored anything, so it is disabled here: with no single database to
        fall back to, thread.dbname is the only thing left, exactly as it would
        be in a process pointed at more than one world.

        The clock is *overridden* for the assertion rather than read from the
        database, so that this runs on an ordinary test database. Without that
        it could only pass on a database sim_init had been run on, and it read
        as a test of the tick's bookkeeping while actually asserting the shape
        of whatever database it happened to be pointed at.
        """
        self.loop.mark_cron_thread()
        with self._no_ready_jobs():
            self.loop.run_cron_tick()

        game_clock.override(self.env.cr.dbname, GameClock(
            datetime(2030, 1, 1), datetime.now(), 1440, False, timedelta(seconds=5),
        ))
        self.addCleanup(game_clock.invalidate, self.env.cr.dbname)
        with patch.dict(config.options, {'db_name': []}):
            self.assertIsNotNone(
                game_clock.current_clock(),
                "this thread must still resolve its world after a tick",
            )

    def test_tick_clears_start_time(self):
        """ A left-behind start_time makes the watchdog kill an idle thread. """
        self.loop.mark_cron_thread()
        with self._no_ready_jobs():
            self.loop.run_cron_tick()
        self.assertIsNone(threading.current_thread().start_time)

    def test_a_failing_job_does_not_lose_the_dbname(self):
        """ The bookkeeping is in a finally: a crash must not skip it. """
        self.loop.mark_cron_thread()
        with patch.object(IrCron, '_process_jobs', side_effect=RuntimeError('boom')):
            self.loop.run_cron_tick()  # must not raise
        self.assertEqual(
            getattr(threading.current_thread(), 'dbname', None), self.env.cr.dbname)
        self.assertIsNone(threading.current_thread().start_time)


class TestForward(TransactionCase):
    """A forward moves the world from one due event to the next (DESIGN.md 3.4).

    The loop's own cursor and cron runner are swapped for this transaction's,
    through registry test mode, so jobs run here and roll back with the test.
    The world's other crons are switched off: only this test's may run.
    """

    START = datetime(2030, 3, 1, 16, 0)
    TARGET = datetime(2030, 3, 2, 8, 0)

    def setUp(self):
        super().setUp()
        cr = self.env.cr
        self.env['ir.cron'].search([]).active = False
        cr.execute("SET search_path = public, pg_catalog")
        game_clock.install(cr, self.START, 60, timedelta(days=365))
        self.addCleanup(game_clock.invalidate, cr.dbname)
        # A job reads the clock through the cache; running out of time mid-test
        # would send it to a connection that cannot see this transaction.
        self.startPatcher(patch.object(game_clock, 'CACHE_TTL', 3600))
        self.loop = loop.GameLoop(cr.dbname, 1.0, 1.0)

    def cron(self, name, code=''):
        """ A cron that only ever runs when triggered. """
        return self.env['ir.cron'].create({
            'name': f"forward test: {name}",
            'state': 'code',
            'code': code,
            'model_id': self.env.ref('base.model_res_partner').id,
            'user_id': self.env.uid,
            'interval_number': 1,
            'interval_type': 'days',
            'nextcall': datetime(2100, 1, 1),
        })

    def process_jobs(self):
        with self.registry.cursor() as cr:
            IrCron._process_jobs_loop(cr, job_ids=[job['id'] for job in IrCron._get_all_ready_jobs(cr)])

    def forward(self, process_jobs=None):
        game_clock.start_forward(self.env.cr, self.TARGET)
        self.env.flush_all()
        with self.enter_registry_test_mode():
            self.loop.run_forward(cursor=self.registry.cursor, process_jobs=process_jobs or self.process_jobs)
        self.env.invalidate_all()

    def partners(self, name):
        return self.env['res.partner'].search([('name', '=', f"forward test: {name}")])

    def test_everything_happens_when_it_was_due(self):
        """ Including what an event sets off in turn, from *its* instant. """
        later = self.cron('later', "env['res.partner'].create({'name': 'forward test: later'})")
        evening = self.cron('evening', f"""
partner = env['res.partner'].create({{'name': 'forward test: evening'}})
env['ir.cron'].browse({later.id})._trigger(partner.create_date + datetime.timedelta(hours=2))
""")
        evening._trigger(datetime(2030, 3, 1, 23, 0))
        tomorrow = self.cron('tomorrow', "env['res.partner'].create({'name': 'forward test: tomorrow'})")
        tomorrow._trigger(self.TARGET + timedelta(hours=2))

        self.forward()

        self.assertEqual(self.partners('evening').create_date, datetime(2030, 3, 1, 23, 0))
        self.assertEqual(self.partners('later').create_date, datetime(2030, 3, 2, 1, 0),
                         "two hours after the evening job, not two hours after the morning")
        self.assertFalse(self.partners('tomorrow'), "due after the morning: not yet")
        clock = game_clock.read(self.env.cr)
        self.assertEqual(clock.game_now, self.TARGET)
        self.assertFalse(clock.forwarding, "time runs again")

    def test_an_event_at_the_target_happens_before_time_runs_again(self):
        self.cron('nine', "env['res.partner'].create({'name': 'forward test: nine'})")._trigger(self.TARGET)
        self.forward()
        self.assertEqual(self.partners('nine').create_date, self.TARGET)

    def test_a_job_that_stays_ready_does_not_hold_the_world(self):
        """ Jobs that never stop being ready are run a bounded number of times. """
        self.cron('stuck')._trigger(self.START)
        runs = []
        with self.assertLogs(loop._logger.name, 'WARNING'):
            self.forward(process_jobs=lambda: runs.append(1))
        self.assertEqual(len(runs), loop.MAX_PASSES_PER_INSTANT)
        self.assertEqual(game_clock.read(self.env.cr).game_now, self.TARGET)

    def test_a_world_that_is_not_forwarding_takes_no_step(self):
        self.assertIsNone(loop.forward_step(self.env.cr))
