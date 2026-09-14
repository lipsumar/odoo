# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the working day (``workday.py``): where a forward lands, and how
long work takes when it stops at five and carries on at nine."""
from datetime import date, datetime, time, timedelta

import pytz

from odoo.tests.common import BaseCase

from odoo.addons.odoo_sim import workday


class TestNextWorkingDay(BaseCase):
    """Where a forward lands: nine in the morning, in the player's zone."""

    brussels = workday.timezone('Europe/Brussels')

    def test_from_the_evening_it_is_tomorrow_morning(self):
        # 17:30 in Brussels, which is UTC+1 in March
        self.assertEqual(workday.next_day_start(datetime(2030, 3, 1, 16, 30), self.brussels),
                         datetime(2030, 3, 2, 8, 0))

    def test_from_the_small_hours_it_is_the_same_morning(self):
        """ Two in the morning is still the night being skipped. """
        self.assertEqual(workday.next_day_start(datetime(2030, 3, 1, 1, 0), self.brussels),
                         datetime(2030, 3, 1, 8, 0))

    def test_at_nine_sharp_it_is_the_next_day(self):
        self.assertEqual(workday.next_day_start(datetime(2030, 3, 1, 8, 0), self.brussels),
                         datetime(2030, 3, 2, 8, 0))

    def test_across_a_change_of_clocks(self):
        """ Nine is nine on the morning summer time starts: an hour earlier in UTC. """
        self.assertEqual(workday.next_day_start(datetime(2030, 3, 30, 17, 0), self.brussels),
                         datetime(2030, 3, 31, 7, 0))

    def test_the_first_zone_that_exists_wins(self):
        self.assertEqual(workday.timezone('Mars/Olympus_Mons', 'Europe/Brussels').zone, 'Europe/Brussels')
        self.assertIs(workday.timezone(None, False, ''), pytz.utc)

    def test_a_zone_is_a_name(self):
        """ Whatever a page sends as a zone, only a name is looked up. """
        self.assertIs(workday.timezone(['Europe/Brussels'], {'tz': 'UTC'}, 42), pytz.utc)


class TestWorkingTime(BaseCase):

    tz = pytz.timezone('Europe/Brussels')

    def local(self, *args):
        """ A naive UTC instant, given as local time in Brussels. """
        return workday.at(date(*args[:3]), time(*args[3:]), self.tz)

    def test_work_inside_the_day_goes_straight_through(self):
        end = workday.add_working_time(self.local(2030, 3, 4, 9, 30), timedelta(hours=1), self.tz)
        self.assertEqual(end, self.local(2030, 3, 4, 10, 30))

    def test_work_stops_at_five_and_carries_on_at_nine(self):
        end = workday.add_working_time(self.local(2030, 3, 4, 15, 0), timedelta(hours=4), self.tz)
        self.assertEqual(end, self.local(2030, 3, 5, 11, 0))

    def test_work_that_ends_at_five_ends_that_day(self):
        end = workday.add_working_time(self.local(2030, 3, 4, 16, 0), timedelta(hours=1), self.tz)
        self.assertEqual(end, self.local(2030, 3, 4, 17, 0))

    def test_work_begun_out_of_hours_starts_the_next_morning(self):
        for start in (self.local(2030, 3, 4, 20, 0), self.local(2030, 3, 5, 6, 0)):
            with self.subTest(start=start):
                end = workday.add_working_time(start, timedelta(minutes=30), self.tz)
                self.assertEqual(end, self.local(2030, 3, 5, 9, 30))

    def test_long_work_takes_days(self):
        end = workday.add_working_time(self.local(2030, 3, 4, 9, 0), timedelta(hours=20), self.tz)
        self.assertEqual(end, self.local(2030, 3, 6, 13, 0))

    def test_across_a_change_of_clocks(self):
        """ Brussels goes to summer time in the night of 30 to 31 March 2030. """
        end = workday.add_working_time(self.local(2030, 3, 30, 16, 0), timedelta(hours=2), self.tz)
        self.assertEqual(end, self.local(2030, 3, 31, 10, 0))
        self.assertEqual(end, datetime(2030, 3, 31, 8, 0), "10:00 in summer time is 08:00 UTC")

    def test_who_is_at_work(self):
        self.assertTrue(workday.is_working(self.local(2030, 3, 4, 9, 0), self.tz))
        self.assertTrue(workday.is_working(self.local(2030, 3, 4, 16, 59), self.tz))
        self.assertFalse(workday.is_working(self.local(2030, 3, 4, 17, 0), self.tz))
        self.assertFalse(workday.is_working(self.local(2030, 3, 4, 8, 59), self.tz))
