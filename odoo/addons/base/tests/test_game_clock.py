# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the odoo-sim accelerated game clock (see odoo_sim/DESIGN.md)."""

import secrets
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch

from odoo import fields, game_clock
from odoo.addons.base.models.ir_cron import MAX_FAIL_TIME, BadModuleState, IrCron
from odoo.cli import sim_init
from odoo.game_clock import GameClock
from odoo.tests.common import BaseCase, TransactionCase, freeze_time
from odoo.tools import config
from odoo.tests.test_cursor import TestCursor

#: Real instant every clock in this module is anchored on.
ANCHOR_REAL = datetime(2026, 9, 9, 12, 0, 0)
#: Game instant a world is usually sitting at, deliberately far from the real one.
GAME_START = ANCHOR_REAL + timedelta(days=100)
#: A clamp long enough never to bind: lets a test exercise the rate alone.
NO_CLAMP = timedelta(days=365)


@contextmanager
def not_a_game_world(dbname):
    """ Force ``dbname`` to look like an ordinary Odoo database.

    The suite cannot assume its own database is not a world: developing this
    feature means having one to hand, and running the tests against it is the
    obvious thing to do.  Anything asserting *unchanged upstream behaviour*
    has to say so rather than inherit it from the environment.
    """
    game_clock.override(dbname, None)
    try:
        yield
    finally:
        game_clock.invalidate(dbname)


def resolved_dbname():
    """ The database ``game_clock.current_clock()`` would pick right now. """
    dbname = getattr(threading.current_thread(), 'dbname', None)
    if not dbname:
        names = config['db_name']
        dbname = names[0] if len(names) == 1 else None
    return dbname


def a_clock(game_now=GAME_START, rate=60, *, last_tick=ANCHOR_REAL,
            paused=False, max_gap=NO_CLAMP):
    """ A clock reading, with the staleness clamp off unless asked for. """
    return GameClock(game_now, last_tick, rate, paused, max_gap)


class TestGameClockMath(BaseCase):
    """The real-to-game mapping, with no database involved."""

    def test_disabled_is_identity(self):
        """ Outside a game world the clock must be plain real time. """
        game_clock.invalidate()
        with not_a_game_world(resolved_dbname()), freeze_time(ANCHOR_REAL):
            self.assertIsNone(game_clock.clock_for(None))
            self.assertFalse(game_clock.is_sim_database(None))
            self.assertEqual(game_clock.now(), ANCHOR_REAL)
            self.assertEqual(fields.Datetime.now(), ANCHOR_REAL)
            self.assertEqual(fields.Date.today(), ANCHOR_REAL.date())

    def test_rate_multiplier(self):
        """ Game time advances by K seconds per real second. """
        for rate in (1, 0.5, 60, 1000):
            with self.subTest(rate=rate), freeze_time(ANCHOR_REAL) as frozen:
                clock = a_clock(ANCHOR_REAL, rate)
                self.assertEqual(clock.now(), ANCHOR_REAL)
                frozen.tick(timedelta(seconds=10))
                self.assertEqual(clock.now(), ANCHOR_REAL + timedelta(seconds=10 * rate))
                frozen.tick(timedelta(seconds=5))
                self.assertEqual(clock.now(), ANCHOR_REAL + timedelta(seconds=15 * rate))

    def test_rejects_non_positive_rate(self):
        """ K <= 0 is what monotonicity is bought with; refuse it up front. """
        for rate in (0, -1):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                a_clock(rate=rate)

    def test_rejects_non_positive_max_gap(self):
        """ A zero clamp would freeze the world permanently. """
        for max_gap in (timedelta(0), timedelta(seconds=-1)):
            with self.subTest(max_gap=max_gap), self.assertRaises(ValueError):
                a_clock(max_gap=max_gap)

    def test_game_offset(self):
        """ The world sits wherever its game instant says, not at real now. """
        clock = a_clock(GAME_START, 60)
        with freeze_time(ANCHOR_REAL) as frozen:
            self.assertEqual(clock.now(), GAME_START)
            frozen.tick(timedelta(minutes=1))
            self.assertEqual(clock.now(), GAME_START + timedelta(hours=1))

    def test_monotonic(self):
        """ With K > 0 game time cannot move backwards (DESIGN.md 3.1). """
        clock = a_clock(ANCHOR_REAL, 1000)
        with freeze_time(ANCHOR_REAL) as frozen:
            previous = clock.now()
            for _ in range(50):
                frozen.tick(timedelta(milliseconds=17))
                current = clock.now()
                self.assertGreater(current, previous)
                previous = current

    def test_untended_clock_freezes(self):
        """ DESIGN.md 3.3: no tick for max_gap and the world stops ageing.

        This is what stops a killed loop or a sleeping laptop from ageing a
        world by months while nobody is playing.
        """
        clock = a_clock(GAME_START, 1440, max_gap=timedelta(seconds=5))
        with freeze_time(ANCHOR_REAL) as frozen:
            frozen.tick(timedelta(seconds=5))
            frozen_at = clock.now()
            self.assertEqual(frozen_at, GAME_START + timedelta(seconds=5 * 1440))
            for _ in range(5):
                frozen.tick(timedelta(hours=1))
                self.assertEqual(clock.now(), frozen_at, "an untended clock must stand still")

    def test_paused_clock_is_frozen(self):
        """ The explicit pause is immediate and unconditional. """
        clock = a_clock(GAME_START, 1440, paused=True)
        with freeze_time(ANCHOR_REAL) as frozen:
            self.assertEqual(clock.now(), GAME_START)
            frozen.tick(timedelta(hours=3))
            self.assertEqual(clock.now(), GAME_START)

    def test_clock_skew_does_not_rewind(self):
        """ last_tick_real is PostgreSQL's clock, `real` is this process's.

        They can disagree; game time must never move backwards because of it.
        """
        clock = a_clock(GAME_START, 1440, last_tick=ANCHOR_REAL + timedelta(seconds=30))
        with freeze_time(ANCHOR_REAL):
            self.assertEqual(clock.now(), GAME_START)

    def test_interpolation_is_continuous_across_a_tick(self):
        """ DESIGN.md 3.3: a reader sees no jump when the loop ticks.

        This is the property that lets anything -- a cursor, a browser --
        interpolate from a reading it took a moment ago.
        """
        rate, max_gap = 1440, timedelta(seconds=5)
        before = a_clock(GAME_START, rate, max_gap=max_gap)
        tick_at = ANCHOR_REAL + timedelta(seconds=3)
        # the loop's UPDATE, expressed in Python
        after = GameClock(before.game_at(tick_at), tick_at, rate, max_gap=max_gap)
        # up to the point where the *older* basis hits its own clamp
        for offset in (0, 0.5, 1, 2):
            with self.subTest(offset=offset):
                instant = tick_at + timedelta(seconds=offset)
                self.assertEqual(before.game_at(instant), after.game_at(instant))

    def test_is_running(self):
        """ The predicate write paths guard on (DESIGN.md 3.3).

        A world can be down while the database, the web workers and the HTTP
        stack are all up, which is exactly when a player must not be allowed to
        act into it.
        """
        max_gap = timedelta(seconds=5)
        live = a_clock(GAME_START, 1440, max_gap=max_gap)
        with freeze_time(ANCHOR_REAL) as frozen:
            self.assertTrue(game_clock.is_running(live))
            frozen.tick(timedelta(seconds=4))
            self.assertTrue(game_clock.is_running(live), "still within max_gap")
            frozen.tick(timedelta(seconds=2))
            self.assertFalse(game_clock.is_running(live), "no tick for longer than max_gap")

            self.assertFalse(
                game_clock.is_running(a_clock(GAME_START, 1440, paused=True)),
                "a paused world is not running, however fresh its last tick",
            )
            self.assertTrue(
                game_clock.is_running(None),
                "an ordinary Odoo database has no loop to stop",
            )

    def test_field_helpers_follow_the_clock(self):
        """ The four business-time entry points of fields_temporal. """
        clock = a_clock(datetime(2031, 3, 4, 15, 30, 45))
        with freeze_time(ANCHOR_REAL), patch.object(game_clock, 'current_clock', return_value=clock):
            self.assertEqual(fields.Datetime.now(), datetime(2031, 3, 4, 15, 30, 45))
            self.assertEqual(fields.Datetime.today(), datetime(2031, 3, 4, 0, 0, 0))
            self.assertEqual(fields.Date.today(), date(2031, 3, 4))

    def test_microseconds_are_annihilated(self):
        """ fields.Datetime.now() must still comply with the server format. """
        clock = a_clock(ANCHOR_REAL + timedelta(microseconds=123456))
        with freeze_time(ANCHOR_REAL), patch.object(game_clock, 'current_clock', return_value=clock):
            self.assertEqual(fields.Datetime.now().microsecond, 0)


@contextmanager
def game_world(cr, clock):
    """ Make the database behind ``cr`` a game world for the duration.

    Python-side only: no table, no SQL function.
    """
    game_clock.override(cr.dbname, clock)
    cr._now = None
    try:
        yield clock
    finally:
        game_clock.invalidate(cr.dbname)
        cr._now = None


@contextmanager
def installed_game_world(cr, game_now=GAME_START, rate=60, max_gap=NO_CLAMP):
    """ Install a real clock -- table, row and SQL function -- for the duration.

    All three are transactional, so the test's savepoint undoes them.
    """
    cr.execute("SET search_path = public, pg_catalog")
    clock = game_clock.install(cr, game_now, rate, max_gap)
    cr._now = None
    try:
        yield clock
    finally:
        game_clock.invalidate(cr.dbname)
        cr._now = None


class TestGameClockDatabase(TransactionCase):
    """The clock as seen by the ORM and by PostgreSQL."""

    def _sql_now(self):
        """ Read public.now() and pg_catalog.now() at the same real instant. """
        self.env.cr.execute("""
            SELECT (pg_catalog.now() AT TIME ZONE 'UTC'),
                   (now() AT TIME ZONE 'UTC')
        """)
        return self.env.cr.fetchone()

    def test_sql_and_python_agree(self):
        """ The generated public.now() implements the same formula as Python.

        Compared at the *same* real instant -- PostgreSQL's now() is the
        transaction start time, and this transaction opened at setUpClass, so
        the two would legitimately differ if each read its own wall clock.
        """
        for rate in (1, 60, 1000):
            with self.subTest(rate=rate), installed_game_world(self.env.cr, rate=rate) as clock:
                sql_real, sql_game = self._sql_now()
                self.assertAlmostEqual(
                    sql_game, clock.game_at(sql_real), delta=timedelta(milliseconds=1),
                    msg="public.now() disagrees with GameClock.game_at()",
                )
                # and the unqualified now() really did resolve to public.now()
                self.assertNotAlmostEqual(sql_game, sql_real, delta=timedelta(days=1))

    def test_sql_and_python_agree_when_clamped(self):
        """ The clamp must bind identically on both sides (DESIGN.md 3.3). """
        max_gap = timedelta(seconds=2)
        with installed_game_world(self.env.cr, rate=1440, max_gap=max_gap):
            # simulate a loop that died an hour ago
            self.env.cr.execute("""
                UPDATE public.game_clock
                   SET last_tick_real = pg_catalog.now() - INTERVAL '1 hour'
            """)
            game_clock.invalidate(self.env.cr.dbname)
            clock = game_clock.clock_for(self.env.cr.dbname, cr=self.env.cr)
            sql_real, sql_game = self._sql_now()

            self.assertEqual(sql_game, clock.game_at(sql_real))
            self.assertEqual(
                sql_game, clock.game_now + max_gap * 1440,
                "a dead loop must accrue exactly max_gap, no more",
            )

    def test_sql_and_python_agree_when_paused(self):
        """ A paused world is frozen for raw SQL too, not just for Python. """
        with installed_game_world(self.env.cr, rate=1440) as clock:
            paused = game_clock.set_paused(self.env.cr, True)
            self.assertTrue(paused.paused)
            sql_real, sql_game = self._sql_now()
            self.assertEqual(sql_game, paused.game_now)
            self.assertEqual(sql_game, paused.game_at(sql_real))
            self.assertGreaterEqual(paused.game_now, clock.game_now)

    def test_tick_advances_game_time(self):
        """ One tick moves the world forward by the real interval times K. """
        with installed_game_world(self.env.cr, rate=1440) as clock:
            self.env.cr.execute("""
                UPDATE public.game_clock
                   SET last_tick_real = pg_catalog.now() - INTERVAL '2 seconds'
            """)
            ticked = game_clock.tick(self.env.cr)
            self.assertEqual(ticked.game_now, clock.game_now + timedelta(seconds=2 * 1440))

    def test_tick_clamps_a_long_absence(self):
        """ Restarting after a crash is an ordinary, clamped tick. """
        max_gap = timedelta(seconds=5)
        with installed_game_world(self.env.cr, rate=1440, max_gap=max_gap) as clock:
            self.env.cr.execute("""
                UPDATE public.game_clock
                   SET last_tick_real = pg_catalog.now() - INTERVAL '3 days'
            """)
            ticked = game_clock.tick(self.env.cr)
            self.assertEqual(ticked.game_now, clock.game_now + max_gap * 1440)

    def test_tick_does_not_advance_a_paused_world(self):
        """ A running loop must not age a world the player paused. """
        with installed_game_world(self.env.cr, rate=1440) as clock:
            game_clock.set_paused(self.env.cr, True)
            self.env.cr.execute("""
                UPDATE public.game_clock
                   SET last_tick_real = pg_catalog.now() - INTERVAL '1 hour'
            """)
            ticked = game_clock.tick(self.env.cr)
            self.assertTrue(ticked.paused)
            self.assertGreaterEqual(ticked.game_now, clock.game_now)
            self.assertLess(ticked.game_now, clock.game_now + timedelta(minutes=1))

    def test_resume_does_not_accrue_the_paused_stretch(self):
        """ last_tick_real moves while paused, so resuming does not jump. """
        with installed_game_world(self.env.cr, rate=1440):
            paused = game_clock.set_paused(self.env.cr, True)
            self.env.cr.execute("""
                UPDATE public.game_clock
                   SET last_tick_real = pg_catalog.now() - INTERVAL '1 hour'
            """)
            resumed = game_clock.set_paused(self.env.cr, False)
            self.assertFalse(resumed.paused)
            self.assertEqual(resumed.game_now, paused.game_now)

    def test_records_follow_game_time(self):
        """ create_date and write_date carry game time, not real time. """
        clock = a_clock(GAME_START, 60)
        with freeze_time(ANCHOR_REAL) as frozen, game_world(self.env.cr, clock):
            partner = self.env['res.partner'].create({'name': 'Sim Partner'})
            self.assertEqual(partner.create_date, clock.now())
            self.assertEqual(partner.write_date, clock.now())
            # 100 game days away from the real clock
            self.assertEqual(partner.create_date - datetime.now(), timedelta(days=100))

            frozen.tick(timedelta(minutes=1))
            self.env.cr._now = None
            partner.write({'name': 'Sim Partner renamed'})
            partner.flush_recordset()
            # one real minute later is one game hour later
            self.assertEqual(partner.write_date, partner.create_date + timedelta(hours=1))

    def test_cursor_now_is_the_game_clock(self):
        """ cr.now() is the single chokepoint for every record timestamp. """
        clock = a_clock(GAME_START, 60)
        with freeze_time(ANCHOR_REAL), game_world(self.env.cr, clock):
            self.assertEqual(self.env.cr.now(), clock.now())

        # Outside a game world a real cursor falls back to SELECT now(), i.e.
        # to unchanged upstream behaviour -- SQL time, which freezegun cannot
        # reach, and which is nowhere near the game time asserted above.
        #
        # Only checkable on an ordinary database: this cursor sets search_path
        # to public first on a world (sql_db.py:383-390), so even the fallback
        # would resolve to public.now() and come back on game time.
        if game_clock.clock_for(self.env.cr.dbname) is not None:
            self.skipTest("the test database is itself a game world")
        self.env.cr._now = None
        self.assertAlmostEqual(self.env.cr.now(), datetime.now(), delta=timedelta(minutes=5))

    def test_test_cursor_now_is_the_game_clock(self):
        """ Same, for the cursor used under registry test mode.

        TestCursor.now() never queries the database, so the SQL public.now()
        override alone would be invisible to it.
        """
        clock = a_clock(GAME_START, 60)
        with self.enter_registry_test_mode(), self.registry.cursor() as cr:
            self.assertIsInstance(cr, TestCursor)
            with freeze_time(ANCHOR_REAL), game_world(cr, clock):
                self.assertEqual(cr.now(), clock.now())
            with not_a_game_world(cr.dbname), freeze_time(ANCHOR_REAL):
                cr._now = None
                self.assertEqual(cr.now(), ANCHOR_REAL)

    def test_context_today_follows_the_clock(self):
        """ Date.context_today() reads the clock, then converts to the user tz. """
        clock = a_clock(datetime(2031, 3, 4, 23, 30))
        user = self.env.user.with_context(tz='Australia/Sydney')
        with freeze_time(ANCHOR_REAL), patch.object(game_clock, 'current_clock', return_value=clock):
            # 2031-03-04 23:30 UTC is already the 5th in Sydney (UTC+11)
            self.assertEqual(fields.Date.context_today(user), date(2031, 3, 5))


class TestSimInitStartingInstant(TransactionCase):
    """Which game instant a world is created, or re-created, at."""

    def test_explicit_game_start_wins(self):
        instant = sim_init.starting_instant(self.env.cr, '2031-03-04T15:30:45', None)
        self.assertEqual(instant, datetime(2031, 3, 4, 15, 30, 45))
        self.assertIsNone(instant.tzinfo, "stored naive UTC like everything else")

    def test_explicit_game_start_wins_over_an_existing_world(self):
        """ --game-start is how you deliberately move a world's clock. """
        existing = a_clock(GAME_START, 1440)
        instant = sim_init.starting_instant(self.env.cr, '2031-03-04T15:30:45', existing)
        self.assertEqual(instant, datetime(2031, 3, 4, 15, 30, 45))

    def test_a_bad_game_start_is_rejected(self):
        with self.assertRaises(ValueError):
            sim_init.starting_instant(self.env.cr, 'the fourth of March', None)

    def test_an_existing_world_carries_its_instant_across(self):
        """ DESIGN.md 7-8: --force must not rewind a world to the present.

        Changing the rate between runs is the supported escape hatch, and it
        only works if the world keeps the time it had reached. Anchoring on real
        now instead would move game time backwards by however far the world had
        got -- here, a hundred game days -- and break the monotonicity that
        write_date ordering and optimistic concurrency rely on.
        """
        existing = a_clock(GAME_START, 1440)
        with freeze_time(ANCHOR_REAL):
            instant = sim_init.starting_instant(self.env.cr, None, existing)
        self.assertEqual(instant, GAME_START)
        self.assertGreater(
            instant, datetime.now(),
            "the world's own instant, not the real one it would rewind to",
        )

    def test_a_fresh_world_starts_at_the_database_clock(self):
        """ And on the database's clock, not this process's. """
        instant = sim_init.starting_instant(self.env.cr, None, None)
        self.env.cr.execute("SELECT (pg_catalog.now() AT TIME ZONE 'UTC')")
        self.assertAlmostEqual(
            instant, self.env.cr.fetchone()[0], delta=timedelta(seconds=1),
        )


class TestGameClockCron(TransactionCase):
    """Crons become due on game time for free, once cr.now() is the clock."""

    def _make_cron(self, nextcall):
        return self.env['ir.cron'].create({
            'name': f'Game clock cron {secrets.token_urlsafe(8)}',
            'state': 'code',
            'code': '',
            'model_id': self.env.ref('base.model_res_partner').id,
            'user_id': self.env.uid,
            'active': True,
            'interval_number': 1,
            'interval_type': 'hours',
            'nextcall': nextcall,
        })

    def test_cron_becomes_ready_on_game_time(self):
        """ A job 30 game-minutes out fires after 30 real seconds at K=3600. """
        clock = a_clock(ANCHOR_REAL, 3600)
        with freeze_time(ANCHOR_REAL) as frozen, game_world(self.env.cr, clock):
            cron = self._make_cron(clock.now() + timedelta(minutes=30))
            self.env.flush_all()

            ready = {job['id'] for job in IrCron._get_all_ready_jobs(self.env.cr)}
            self.assertNotIn(cron.id, ready, "job is 30 game-minutes away")

            # one real second is one game hour
            frozen.tick(timedelta(seconds=1))
            self.env.cr._now = None

            ready = {job['id'] for job in IrCron._get_all_ready_jobs(self.env.cr)}
            self.assertIn(cron.id, ready, "job should be due one game hour later")

    def test_check_modules_state_stays_on_one_clock(self):
        """ Regression for DESIGN.md 5.1.

        _check_modules_state compared real time against nextcall/write_date,
        which are on the cursor's clock. On a game world that delta goes
        negative, BadModuleState is raised unconditionally and every cron on
        the database stops. It must compare game time to game time.
        """
        clock = a_clock(GAME_START, 60)
        with freeze_time(ANCHOR_REAL), game_world(self.env.cr, clock):
            self.env.cr.execute("""
                UPDATE ir_module_module SET state = 'to upgrade' WHERE name = 'base'
            """)
            now = self.env.cr.now()

            # Jobs stuck for well over MAX_FAIL_TIME in game time: the module
            # states are zombies and must be reset.
            stale = [{'nextcall': now - MAX_FAIL_TIME - timedelta(hours=1), 'write_date': None}]
            with patch('odoo.modules.loading.reset_modules_state') as reset:
                IrCron._check_modules_state(self.env.cr, stale)
            reset.assert_called_once()

            # Jobs that only just became due: a module install is genuinely
            # under way, leave it alone.
            fresh = [{'nextcall': now - timedelta(minutes=1), 'write_date': None}]
            with self.assertRaises(BadModuleState):
                IrCron._check_modules_state(self.env.cr, fresh)

    def test_process_jobs_deletes_thread_dbname(self):
        """ Regression for DESIGN.md 5.8.

        _process_jobs sets thread.dbname on the way in and *deletes* it on the
        way out -- it does not restore a previous value. A game loop that sets
        it once at startup therefore drops back to real time after its first
        tick, silently. The loop re-sets it after every call; this test is what
        notices if that stops being necessary, or stops being enough.
        """
        dbname = self.env.cr.dbname
        thread = threading.current_thread()
        had_dbname = hasattr(thread, 'dbname')
        previous = getattr(thread, 'dbname', None)
        try:
            thread.dbname = dbname
            # no ready jobs: we are testing the bookkeeping, not the jobs
            with patch.object(IrCron, '_get_all_ready_jobs', staticmethod(lambda cr: [])):
                IrCron._process_jobs(dbname)
            self.assertFalse(
                hasattr(thread, 'dbname'),
                "_process_jobs no longer deletes thread.dbname -- if that is "
                "deliberate, the game loop no longer needs to re-set it",
            )
        finally:
            if had_dbname:
                thread.dbname = previous
            elif hasattr(thread, 'dbname'):
                del thread.dbname
