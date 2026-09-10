"""The world pulse: what the game loop tells everyone else about game time.

A client cannot observe ticks, so it cannot use the staleness clamp that stops
an untended world from ageing -- with the clamp it would freeze after
``max_gap`` on a perfectly healthy world, and without it, it would run away
when the world stopped.  It has to be told instead.  See ``DESIGN.md`` 3.3.

Kept out of ``cli/game_run.py`` so that it can be imported and tested without
loading a CLI command.
"""
from __future__ import annotations

import typing
from datetime import datetime

from odoo import api, game_clock

if typing.TYPE_CHECKING:
    from odoo.game_clock import GameClock
    from odoo.sql_db import BaseCursor

#: Bus channel and notification type of the world pulse.  Consumed by the UI.
CHANNEL = 'odoo_sim.world'
TYPE = 'odoo_sim.pulse'


def payload(clock: GameClock, real: datetime | None = None) -> dict:
    """ Return what a client needs to track this world.

    ``running`` is evaluated here rather than by the client, so that the lock a
    UI applies and the guard a write path enforces cannot drift apart.

    ``server_real_now`` lets a client correct for clock skew when
    *interpolating*, which has to agree with the server's value.  It must not
    be used to decide *liveness*: comparing a server timestamp against a
    client's wall clock makes the decision skew-sensitive in both directions.
    Liveness belongs on the client's own monotonic clock, measured from when a
    pulse arrived.

    **The three datetimes are naive UTC ISO strings, with no offset**, matching
    the convention every other datetime Odoo sends a browser follows
    (``deserializeDateTime`` parses server values with ``zone: "utc"``,
    ``addons/web/static/src/core/l10n/dates.js:709``).  Deliberate, and pinned
    by a test, because both ways of "improving" it break a consumer:

    * Appending ``Z`` would be more self-describing but would make this the one
      payload an Odoo client must treat differently from all the others -- and
      would double up with the ``+ 'Z'`` a client already has to add.
    * Emitting Odoo's SQL datetime format would let clients reuse
      ``deserializeDateTime`` verbatim, but it truncates to the second, and at
      ``K = 1440`` one second of ``last_tick_real`` is twenty-four game
      minutes of error in the basis.

    So: ISO, microseconds, no offset.  A JavaScript consumer must parse these
    as UTC explicitly -- ``DateTime.fromISO(v, { zone: "utc" })``, or
    ``Date.parse(v + "Z")``.  Plain ``Date.parse`` of an offsetless date-*time*
    reads it as **local** time (the language parses date-only strings as UTC
    and date-times as local, which is as inconsistent as it sounds).  That
    error half-cancels: differences taken within one payload are still right,
    so the clock ticks at the correct rate while showing an absolute time wrong
    by the browser's offset times ``K`` -- 120 game days at UTC+2, ``K =
    1440``.  A casual test passes.

    **Do not reach for Odoo's ``deserializeDateTime``** despite the convention
    it documents above.  It is wrong here twice: it is ``fromSQL``, so the
    ``T`` separator gives an Invalid DateTime, and it then does
    ``.setZone(tz || "default")``, converting into the user's timezone -- the
    opposite of what an interpolation basis wants.  Failing loudly on the first
    is the only mercy; a variant that fixed only the parse would silently
    return local time.

    Note also that ``isoformat()`` omits the fractional part entirely when
    microseconds happen to be zero, so roughly one tick in a million
    serialises as ``"2026-09-09T12:00:00"``.  Valid ISO; parses fine; breaks
    anything that pattern-matches the shape instead of parsing it.
    """
    return {
        'game_now': clock.game_now.isoformat(),
        'last_tick_real': clock.last_tick_real.isoformat(),
        'rate': clock.rate,
        'paused': clock.paused,
        'max_gap': clock.max_gap.total_seconds(),
        'running': game_clock.is_running(clock, real),
        'server_real_now': (real if real is not None else datetime.now()).isoformat(),
    }


def send(cr: BaseCursor, clock: GameClock | None) -> bool:
    """ Queue a pulse on ``cr``'s transaction.  Returns whether one was queued.

    **Called on every tick, unconditionally** -- including while the world is
    paused.  Sending only on change is the obvious optimisation and it destroys
    the mechanism: silence is the only thing that can tell a client the world
    died, so a suppressed pulse is indistinguishable from a stopped loop.

    Sending regardless of pause is what gives a client three unambiguous states
    from one signal: pulses with ``running`` true is live, pulses with
    ``running`` false is paused, silence is dead.  A client never has to infer
    "paused or lost contact".

    The bus writes a row at precommit and notifies at postcommit
    (``addons/bus/models/bus.py:118-166``), so the caller's commit publishes it.
    """
    if clock is None:
        return False
    env = api.Environment(cr, api.SUPERUSER_ID, {})
    if 'bus.bus' not in env:
        return False
    env['bus.bus']._sendone(CHANNEL, TYPE, payload(clock))
    return True
