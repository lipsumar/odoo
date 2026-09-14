# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for what the commands decide rather than parse (see odoo_sim/README.md).

What else ``game_run`` decides is in ``loop.py``, and tested in ``test_loop``.
"""
from datetime import datetime, timedelta

from odoo.game_clock import GameClock
from odoo.tests.common import TransactionCase, freeze_time

from odoo.addons.odoo_sim.cli import sim_init

ANCHOR_REAL = datetime(2026, 9, 9, 12, 0, 0)
GAME_START = ANCHOR_REAL + timedelta(days=100)


def a_world(game_now=GAME_START, rate=1440):
    return GameClock(game_now, ANCHOR_REAL, rate, max_gap=timedelta(days=365))


class TestSimInitStartingInstant(TransactionCase):
    """Which game instant a world is created, or re-created, at."""

    def test_explicit_game_start_wins(self):
        instant = sim_init.starting_instant(self.env.cr, '2031-03-04T15:30:45', None)
        self.assertEqual(instant, datetime(2031, 3, 4, 15, 30, 45))
        self.assertIsNone(instant.tzinfo, "stored naive UTC like everything else")

    def test_explicit_game_start_wins_over_an_existing_world(self):
        """ --game-start is how you deliberately move a world's clock. """
        instant = sim_init.starting_instant(self.env.cr, '2031-03-04T15:30:45', a_world())
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
        with freeze_time(ANCHOR_REAL):
            instant = sim_init.starting_instant(self.env.cr, None, a_world())
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
