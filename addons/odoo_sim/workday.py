"""The company's working day: when the next one starts, and when it ends.

A business operates by day, so the player does not sit through the night: when
they are done for the day they forward the world to the next morning
(``odoo_sim/DESIGN.md`` section 3.4).  The end of the day decides nothing for
the player -- one who keeps going past five is working late, and time simply
runs on.  Employees do stop at five, and pick up where they left off the next
morning (``odoo_sim/EMPLOYEES.md``).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytz

#: When the working day starts, in the player's time zone.  A forward lands here.
DAY_START = time(9, 0)

#: When an employee's working day ends.  No lunch break.
DAY_END = time(17, 0)


def timezone(*names: object) -> pytz.BaseTzInfo:
    """ Return the first of ``names`` that names a time zone, or UTC.  Anything but a string is skipped. """
    for name in names:
        if isinstance(name, str) and name in pytz.all_timezones_set:
            return pytz.timezone(name)
    return pytz.utc


def next_day_start(game_now: datetime, tz: pytz.BaseTzInfo) -> datetime:
    """ Return the first :data:`DAY_START` in ``tz`` strictly after ``game_now``.

    Both naive UTC, like every game instant.  From the evening that is the next
    calendar day; from the small hours it is the same one, since that is still
    the night being skipped.
    """
    local = pytz.utc.localize(game_now).astimezone(tz)
    day = local.date()
    if local.time() >= DAY_START:
        day += timedelta(days=1)
    return at(day, DAY_START, tz)


def at(day: date, moment: time, tz: pytz.BaseTzInfo) -> datetime:
    """ Return ``moment`` on ``day`` in ``tz``, as a naive UTC instant. """
    return tz.localize(datetime.combine(day, moment)).astimezone(pytz.utc).replace(tzinfo=None)


def local_date(instant: datetime, tz: pytz.BaseTzInfo) -> date:
    """ Return the calendar day naive UTC ``instant`` falls on in ``tz``. """
    return pytz.utc.localize(instant).astimezone(tz).date()


def working_start(instant: datetime, tz: pytz.BaseTzInfo) -> datetime:
    """ Return the first instant at or after ``instant`` inside a working day.

    ``instant`` itself during working hours; otherwise the next
    :data:`DAY_START`.  Every day is a working day: weekends are not modelled.
    """
    local = pytz.utc.localize(instant).astimezone(tz)
    if local.time() < DAY_START:
        return at(local.date(), DAY_START, tz)
    if local.time() >= DAY_END:
        return at(local.date() + timedelta(days=1), DAY_START, tz)
    return instant


def is_working(instant: datetime, tz: pytz.BaseTzInfo) -> bool:
    """ Whether ``instant`` is inside a working day in ``tz``. """
    return working_start(instant, tz) == instant


def day_end(instant: datetime, tz: pytz.BaseTzInfo) -> datetime:
    """ Return the :data:`DAY_END` of the working day ``instant`` is in, or the next one. """
    start = working_start(instant, tz)
    return at(local_date(start, tz), DAY_END, tz)


def add_working_time(start: datetime, duration: timedelta, tz: pytz.BaseTzInfo) -> datetime:
    """ Return when ``duration`` of work begun at ``start`` is done.

    Work stops at :data:`DAY_END` and carries on at the next
    :data:`DAY_START`, so four hours begun at three in the afternoon end at
    eleven the next morning.  Work begun outside the working day starts at the
    next one.
    """
    cursor, remaining = working_start(start, tz), duration
    while True:
        end = day_end(cursor, tz)
        if remaining <= end - cursor:
            return cursor + remaining
        remaining -= end - cursor
        cursor = working_start(end, tz)
