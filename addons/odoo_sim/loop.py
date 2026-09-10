"""The odoo-sim game loop: the threads that own game time and crons.

Kept out of ``cli/game_run.py`` so that the parts worth testing can be reached
without importing a CLI command or starting a thread.  ``cli/game_run.py`` is
the argument parsing and the bootstrap; everything below is the behaviour.

Three kinds of thread run in a game process, and the split is load-bearing:

* the **clock thread** does nothing but one ``UPDATE`` per tick;
* the **cron thread** calls ``IrCron._process_jobs``, which can block for ten
  real seconds or more on a single job (DESIGN.md 5.11) -- long enough to push
  the clock past its ``max_gap`` and silently lose game time if the two shared
  a thread;
* Odoo's own **request threads**, because this process also serves the UI.

See ``odoo_sim/DESIGN.md`` sections 3.3 and 4.3.
"""
from __future__ import annotations

import logging
import select
import threading
import time
from contextlib import closing
from datetime import timedelta

import odoo.sql_db
from odoo import game_clock
from odoo.addons.odoo_sim import pulse

_logger = logging.getLogger(__name__)

#: A tick must fit inside the world's staleness clamp with room to spare, or a
#: single late tick freezes the clock under everyone.
MIN_GAP_TO_TICK_RATIO = 2

#: Below this much *real* replay, warn that the bus backlog is uselessly short.
MIN_USEFUL_RETENTION = timedelta(hours=1)


def tick_headroom_error(clock_tick: float, max_gap: timedelta) -> str | None:
    """ Return why ``clock_tick`` is too coarse for ``max_gap``, or ``None``. """
    gap_seconds = max_gap.total_seconds()
    if clock_tick * MIN_GAP_TO_TICK_RATIO > gap_seconds:
        return (
            f"--clock-tick={clock_tick}s leaves no headroom under this world's "
            f"max_gap of {gap_seconds}s: a single late tick would freeze the "
            f"clock. Lower the tick, or raise max_gap with sim_init --force."
        )
    return None


def retention_warning(retention_seconds: int, rate: float) -> str | None:
    """ Return why the bus backlog is uselessly short on this world, or ``None``.

    ``bus.bus._gc_messages`` subtracts ``bus.gc_retention_seconds`` from
    ``fields.Datetime.now()`` and compares against ``create_date``
    (``addons/bus/models/bus.py:97-108``).  Both are game time, so the window is
    denominated in **game** seconds: the 24-hour default is sixty real seconds
    at ``K = 1440``.  That window is how far back a reconnecting client can
    replay, so on a fast world it is worth setting deliberately.

    Correct code, surprising outcome -- see DESIGN.md 5.12.
    """
    real_window = timedelta(seconds=retention_seconds / rate)
    if real_window >= MIN_USEFUL_RETENTION:
        return None
    return (
        f"bus.gc_retention_seconds is {retention_seconds} game seconds, which at "
        f"rate {rate}x is only {real_window} of real time: a client offline longer "
        f"than that cannot replay what it missed. Set it in game seconds for the "
        f"real window you want (one real hour is {int(3600 * rate)})."
    )


class GameLoop:
    """The clock and cron threads of one world."""

    def __init__(self, dbname: str, clock_tick: float, cron_tick: float, pulse_enabled: bool = True):
        self.dbname = dbname
        self.clock_tick = clock_tick
        self.cron_tick = cron_tick
        self.pulse_enabled = pulse_enabled
        self._stop = threading.Event()

    def start(self) -> None:
        for target, name in (
            (self._clock_loop, 'odoo.sim.clock'),
            (self._cron_loop, 'odoo.sim.cron'),
        ):
            thread = threading.Thread(target=target, name=name)
            thread.daemon = True
            thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- clock ------------------------------------------------------------

    def _clock_loop(self) -> None:
        """Advance game time, once per tick, forever.

        Deliberately does nothing on shutdown: a loop that stops ticking is how
        a world freezes, and the first tick after a restart carries a clamped
        catch-up, so there is no state to hand over.
        """
        _logger.info("clock: ticking %s every %ss", self.dbname, self.clock_tick)
        while not self._stop.wait(self.clock_tick):
            try:
                self.run_clock_tick()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the clock
                _logger.warning("clock: tick failed", exc_info=True)

    def run_clock_tick(self) -> None:
        """Advance the clock once, and tell everyone else about it."""
        with odoo.sql_db.db_connect(self.dbname).cursor() as cr:
            clock = game_clock.tick(cr)
            if self.pulse_enabled:
                pulse.send(cr, clock)
            cr.commit()

    # -- crons ------------------------------------------------------------

    def mark_cron_thread(self) -> None:
        """Tag a thread so that Odoo's watchdog applies to it.

        ``process_limit`` enforces ``limit_time_real_cron`` only on threads
        carrying both ``type == 'cron'`` and a ``start_time``
        (``odoo/service/server.py:509-535``).  Without the tag a runaway game
        cron would never be cut off.
        """
        thread = threading.current_thread()
        thread.type = 'cron'
        thread.start_time = None
        thread.dbname = self.dbname

    def run_cron_tick(self) -> None:
        """Run every ready job once, and put back what ``_process_jobs`` took.

        ``_process_jobs`` *deletes* ``thread.dbname`` on the way out
        (``ir_cron.py:211-213``) -- it does not restore a previous value -- so a
        loop that set it once at startup would drop back to **real** time after
        its first tick, and would keep running, keep firing crons, and report
        nothing (DESIGN.md 5.8).  Re-setting it here is the whole fix, which is
        why it has a test of its own.
        """
        from odoo.addons.base.models.ir_cron import IrCron  # noqa: PLC0415

        # threading.current_thread() and not an injected object: _process_jobs
        # reaches for the running thread itself, so anything else would make a
        # test of this pass whether or not the dbname is restored.
        thread = threading.current_thread()
        thread.start_time = time.time()
        try:
            IrCron._process_jobs(self.dbname)
        except Exception:  # noqa: BLE001 - a bad job must not kill the poller
            _logger.warning("cron: tick failed", exc_info=True)
        finally:
            thread.start_time = None
            thread.dbname = self.dbname

    def _cron_loop(self) -> None:
        self.mark_cron_thread()
        _logger.info("cron: polling %s every %ss", self.dbname, self.cron_tick)
        while not self._stop.is_set():
            try:
                # _notifydb notifies on the 'postgres' database with the world's
                # name as payload (ir_cron.py:798-803), so that is where a
                # _trigger() shows up -- NOTIFY does not cross databases.
                with closing(odoo.sql_db.db_connect('postgres').cursor()) as cr:
                    self._process_until_stopped(cr)
            except Exception:  # noqa: BLE001 - reconnect rather than die
                _logger.warning("cron: restarting the poller after a failure", exc_info=True)
                self._stop.wait(self.cron_tick)

    def _process_until_stopped(self, cr) -> None:
        pg_conn = cr._cnx
        cr.execute("SELECT pg_is_in_recovery()")
        if cr.fetchone()[0]:
            _logger.warning("cron: PG cluster in recovery, triggers will wait for a tick")
        else:
            cr.execute("LISTEN cron_trigger")
        cr.commit()

        while not self._stop.is_set():
            # Wake on a trigger, or on the tick, whichever comes first.
            select.select([pg_conn], [], [], self.cron_tick)
            try:
                pg_conn.poll()
            except Exception:
                if pg_conn.closed:
                    return
                raise
            pg_conn.notifies.clear()  # one world here, so there is nothing to filter
            self.run_cron_tick()
