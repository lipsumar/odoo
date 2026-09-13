"""The company's working day: when the next one starts.

A business operates by day, so the player does not sit through the night: when
they are done for the day they forward the world to the next morning
(``odoo_sim/DESIGN.md`` section 3.4).  The end of the day decides nothing -- a
player who keeps going past five is working late, and time simply runs on.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pytz

#: When the working day starts, in the player's time zone.  A forward lands here.
DAY_START = time(9, 0)


def timezone(*names: str | None) -> pytz.BaseTzInfo:
    """ Return the first of ``names`` that is a time zone, or UTC. """
    for name in names:
        if name in pytz.all_timezones_set:
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
    start = tz.localize(datetime.combine(day, DAY_START))
    return start.astimezone(pytz.utc).replace(tzinfo=None)
