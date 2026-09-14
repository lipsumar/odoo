# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Small things the game's models share."""
from odoo import game_clock


def is_world(cr):
    """ Whether ``cr`` is on a game world: a database whose mail never leaves it.

    Read through ``cr``, so that a clock installed in this very transaction counts.
    """
    return game_clock.clock_for(cr.dbname, cr) is not None


def instant(value):
    """ Naive UTC ISO, as ``pulse.payload`` sends datetimes (see its docstring), or ``None``. """
    return value.isoformat() if value else None


def quantity(value):
    """ 200.0 -> "200", 0.5 -> "0.5": how a person writes a quantity. """
    return f"{value:g}"


def trigger(env, xmlid, at=None):
    """ Have the game's cron ``xmlid`` run at the game instant ``at`` (or each of several), or at once.

    A future trigger does not wake the loop (``ir_cron._trigger_list`` only
    notifies for triggers already due), so it is picked up by the loop's next
    poll: the cron lags ``at`` by at most one cron tick.
    """
    cron = env.ref(xmlid, raise_if_not_found=False)
    if cron:
        cron._trigger(at)
