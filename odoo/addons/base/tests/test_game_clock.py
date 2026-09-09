# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the odoo-sim accelerated game clock (see odoo_sim/DESIGN.md)."""

import secrets
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch

from odoo import fields, game_clock
from odoo.addons.base.models.ir_cron import MAX_FAIL_TIME, BadModuleState, IrCron
from odoo.game_clock import GameClock
from odoo.tests.common import BaseCase, TransactionCase, freeze_time
from odoo.tests.test_cursor import TestCursor

ANCHOR_REAL = datetime(2026, 9, 9, 12, 0, 0)


class TestGameClockMath(BaseCase):
    """The real-to-game mapping, with no database involved."""

    def test_disabled_is_identity(self):
        """ Outside a game world the clock must be plain real time. """
        game_clock.invalidate()
        with freeze_time(ANCHOR_REAL):
            self.assertIsNone(game_clock.clock_for(None))
            self.assertFalse(game_clock.is_sim_database(None))
            self.assertEqual(game_clock.now(), ANCHOR_REAL)
            self.assertEqual(fields.Datetime.now(), ANCHOR_REAL)
            self.assertEqual(fields.Date.today(), ANCHOR_REAL.date())

    def test_rate_multiplier(self):
        """ Game time advances by K seconds per real second. """
        for rate in (1, 0.5, 60, 1000):
            with self.subTest(rate=rate), freeze_time(ANCHOR_REAL) as frozen:
                clock = GameClock(ANCHOR_REAL, ANCHOR_REAL, rate)
                self.assertEqual(clock.now(), ANCHOR_REAL)
                frozen.tick(timedelta(seconds=10))
                self.assertEqual(clock.now(), ANCHOR_REAL + timedelta(seconds=10 * rate))
                frozen.tick(timedelta(seconds=5))
                self.assertEqual(clock.now(), ANCHOR_REAL + timedelta(seconds=15 * rate))

    def test_rejects_non_positive_rate(self):
        """ K <= 0 is what monotonicity is bought with; refuse it up front. """
        for rate in (0, -1):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                GameClock(ANCHOR_REAL, ANCHOR_REAL, rate)

    def test_anchor_game_offset(self):
        """ The world may start ahead of the real instant it is anchored on. """
        anchor_game = ANCHOR_REAL + timedelta(days=100)
        clock = GameClock(ANCHOR_REAL, anchor_game, 60)
        with freeze_time(ANCHOR_REAL) as frozen:
            self.assertEqual(clock.now(), anchor_game)
            frozen.tick(timedelta(minutes=1))
            self.assertEqual(clock.now(), anchor_game + timedelta(hours=1))

    def test_monotonic(self):
        """ With K > 0 game time cannot move backwards (DESIGN.md 3.1). """
        clock = GameClock(ANCHOR_REAL, ANCHOR_REAL, 1000)
        with freeze_time(ANCHOR_REAL) as frozen:
            previous = clock.now()
            for _ in range(50):
                frozen.tick(timedelta(milliseconds=17))
                current = clock.now()
                self.assertGreater(current, previous)
                previous = current

    def test_field_helpers_follow_the_clock(self):
        """ The four business-time entry points of fields_temporal. """
        anchor_game = datetime(2031, 3, 4, 15, 30, 45)
        clock = GameClock(ANCHOR_REAL, anchor_game, 60)
        with freeze_time(ANCHOR_REAL), patch.object(game_clock, 'current_clock', return_value=clock):
            self.assertEqual(fields.Datetime.now(), anchor_game)
            self.assertEqual(fields.Datetime.today(), datetime(2031, 3, 4, 0, 0, 0))
            self.assertEqual(fields.Date.today(), date(2031, 3, 4))

    def test_microseconds_are_annihilated(self):
        """ fields.Datetime.now() must still comply with the server format. """
        clock = GameClock(ANCHOR_REAL, ANCHOR_REAL + timedelta(microseconds=123456), 60)
        with freeze_time(ANCHOR_REAL), patch.object(game_clock, 'current_clock', return_value=clock):
            self.assertEqual(fields.Datetime.now().microsecond, 0)


@contextmanager
def game_world(cr, clock):
    """ Make the database behind ``cr`` a game world for the duration. """
    game_clock.override(cr.dbname, clock)
    cr._now = None
    try:
        yield clock
    finally:
        game_clock.invalidate(cr.dbname)
        cr._now = None


class TestGameClockDatabase(TransactionCase):
    """The clock as seen by the ORM and by PostgreSQL."""

    def test_sql_and_python_agree(self):
        """ The generated public.now() implements the same formula as Python.

        Compared at the *same* real instant -- PostgreSQL's now() is the
        transaction start time, and this transaction opened at setUpClass, so
        the two would legitimately differ if each read its own wall clock.

        The function and the search_path are both transactional, so the test
        savepoint rolls them back.
        """
        for rate in (1, 60, 1000):
            with self.subTest(rate=rate):
                clock = GameClock(ANCHOR_REAL, ANCHOR_REAL + timedelta(days=100), rate)
                self.env.cr.execute("SET search_path = public, pg_catalog")
                self.env.cr.execute(clock.sql_function())
                self.env.cr.execute("""
                    SELECT (pg_catalog.now() AT TIME ZONE 'UTC'),
                           (now() AT TIME ZONE 'UTC')
                """)
                sql_real, sql_game = self.env.cr.fetchone()
                self.assertAlmostEqual(
                    sql_game, clock.game_at(sql_real), delta=timedelta(milliseconds=1),
                    msg="public.now() disagrees with GameClock.game_at()",
                )
                # and the unqualified now() really did resolve to public.now()
                self.assertNotAlmostEqual(sql_game, sql_real, delta=timedelta(days=1))

    def test_records_follow_game_time(self):
        """ create_date and write_date carry game time, not real time. """
        clock = GameClock(ANCHOR_REAL, ANCHOR_REAL + timedelta(days=100), 60)
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
        clock = GameClock(ANCHOR_REAL, ANCHOR_REAL + timedelta(days=100), 60)
        with freeze_time(ANCHOR_REAL), game_world(self.env.cr, clock):
            self.assertEqual(self.env.cr.now(), clock.now())

        # Outside a game world a real cursor falls back to SELECT now(), i.e.
        # to unchanged upstream behaviour -- SQL time, which freezegun cannot
        # reach, and which is nowhere near the game time asserted above.
        self.env.cr._now = None
        self.assertAlmostEqual(self.env.cr.now(), datetime.now(), delta=timedelta(minutes=5))

    def test_test_cursor_now_is_the_game_clock(self):
        """ Same, for the cursor used under registry test mode.

        TestCursor.now() never queries the database, so the SQL public.now()
        override alone would be invisible to it.
        """
        clock = GameClock(ANCHOR_REAL, ANCHOR_REAL + timedelta(days=100), 60)
        with self.enter_registry_test_mode(), self.registry.cursor() as cr:
            self.assertIsInstance(cr, TestCursor)
            with freeze_time(ANCHOR_REAL), game_world(cr, clock):
                self.assertEqual(cr.now(), clock.now())
            with freeze_time(ANCHOR_REAL):
                cr._now = None
                self.assertEqual(cr.now(), ANCHOR_REAL)

    def test_context_today_follows_the_clock(self):
        """ Date.context_today() reads the clock, then converts to the user tz. """
        clock = GameClock(ANCHOR_REAL, datetime(2031, 3, 4, 23, 30), 60)
        user = self.env.user.with_context(tz='Australia/Sydney')
        with freeze_time(ANCHOR_REAL), patch.object(game_clock, 'current_clock', return_value=clock):
            # 2031-03-04 23:30 UTC is already the 5th in Sydney (UTC+11)
            self.assertEqual(fields.Date.context_today(user), date(2031, 3, 5))


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
        clock = GameClock(ANCHOR_REAL, ANCHOR_REAL, 3600)
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
        clock = GameClock(ANCHOR_REAL, ANCHOR_REAL + timedelta(days=100), 60)
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
