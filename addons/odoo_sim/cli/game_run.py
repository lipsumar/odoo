"""``odoo-bin game_run`` -- the odoo-sim game loop.

This process owns game progression.  It is the only thing allowed to fire crons
on its world (``--max-cron-threads=0`` is forced, so ``cron_spawn`` iterates
``range(0)``), and the only thing that advances the clock: game time is an
accumulator that moves when this loop ticks it and stands still otherwise, so a
world does not age while nobody is playing.

Three kinds of thread run here, and the split is load-bearing:

* the **clock thread** does nothing but one ``UPDATE`` per tick;
* the **cron thread** calls ``IrCron._process_jobs``, which can block for ten
  real seconds or more on a single job (DESIGN.md 5.11) -- long enough to push
  the clock past its ``max_gap`` and silently lose game time if the two shared
  a thread;
* Odoo's own **request threads**, because this process also serves the UI.

See ``odoo_sim/DESIGN.md`` sections 3.3 and 4.3.
"""
import logging
import optparse
import select
import sys
import threading
import time
from contextlib import closing
from datetime import timedelta

import odoo
import odoo.service.server
import odoo.sql_db
import odoo.tools.config
from odoo import api, game_clock
from odoo.addons.odoo_sim import pulse
from odoo.cli import Command
from odoo.orm.registry import Registry

_logger = logging.getLogger(__name__)


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
                with odoo.sql_db.db_connect(self.dbname).cursor() as cr:
                    clock = game_clock.tick(cr)
                    self._send_pulse(cr, clock)
                    cr.commit()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the clock
                _logger.warning("clock: tick failed", exc_info=True)

    def _send_pulse(self, cr, clock) -> None:
        """Publish the world's basis and liveness, in the tick's own transaction.

        See :mod:`odoo.addons.odoo_sim.pulse` for why this is unconditional --
        including while paused -- and why ``running`` is decided server-side.
        """
        if self.pulse_enabled:
            pulse.send(cr, clock)

    # -- crons ------------------------------------------------------------

    def _cron_loop(self) -> None:
        thread = threading.current_thread()
        # process_limit() enforces limit_time_real_cron only on threads carrying
        # both of these (odoo/service/server.py:509-535).  Without them a
        # runaway game cron would never be cut off.
        thread.type = 'cron'
        thread.start_time = None
        thread.dbname = self.dbname

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
        from odoo.addons.base.models.ir_cron import IrCron  # noqa: PLC0415

        pg_conn = cr._cnx
        cr.execute("SELECT pg_is_in_recovery()")
        if cr.fetchone()[0]:
            _logger.warning("cron: PG cluster in recovery, triggers will wait for a tick")
        else:
            cr.execute("LISTEN cron_trigger")
        cr.commit()

        thread = threading.current_thread()
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

            thread.start_time = time.time()
            try:
                IrCron._process_jobs(self.dbname)
            except Exception:  # noqa: BLE001 - a bad job must not kill the poller
                _logger.warning("cron: tick failed", exc_info=True)
            finally:
                thread.start_time = None
                # _process_jobs deletes thread.dbname on the way out
                # (ir_cron.py:211-213).  Leaving it deleted would drop this
                # thread back to *real* time, silently (DESIGN.md 5.8).
                thread.dbname = self.dbname


class GameRun(Command):
    """Run the odoo-sim game loop: drive game time and fire crons."""

    name = 'game_run'

    description = """
    Run the game world given by -d: advance its clock, fire its crons, and
    serve it over HTTP.

    This process takes control of both.  Game time only moves while it is
    running, and no other worker may fire a cron behind its back -- so any
    other odoo-bin pointed at the same world must be started with
    --max-cron-threads=0.
    """

    def run(self, args):
        parser = odoo.tools.config.parser
        parser.prog = self.prog
        group = optparse.OptionGroup(parser, "Game loop", "Drive the world given by `-d`.")
        group.add_option("--clock-tick", dest="game_clock_tick", type="float", default=1.0,
                         help="real seconds between two advances of game time "
                              "(default: %default)")
        group.add_option("--cron-tick", dest="game_cron_tick", type="float", default=1.0,
                         help="real seconds between two cron polls; sets cron "
                              "resolution to TICK * rate game seconds, and is a "
                              "lower bound, not a guarantee (default: %default)")
        group.add_option("--no-pulse", action="store_true", dest="game_no_pulse",
                         help="do not publish the world pulse on the bus; clients "
                              "will not be able to tell a dead world from a live one")
        parser.add_option_group(group)
        opt = odoo.tools.config.parse_config(args, setup_logging=True)

        dbnames = odoo.tools.config['db_name']
        if len(dbnames) != 1:
            sys.exit('game_run needs exactly one database. Use "-d" argument')
        dbname = dbnames[0]

        if opt.game_clock_tick <= 0 or opt.game_cron_tick <= 0:
            sys.exit("--clock-tick and --cron-tick must be strictly positive")

        clock = game_clock.clock_for(dbname)
        if clock is None:
            sys.exit(
                f"{dbname} is not a game world. "
                f"Run `odoo-bin sim_init -d {dbname}` first."
            )

        max_gap = clock.max_gap.total_seconds()
        if opt.game_clock_tick * 2 > max_gap:
            sys.exit(
                f"--clock-tick={opt.game_clock_tick}s leaves no headroom under this "
                f"world's max_gap of {max_gap}s: a single late tick would freeze the "
                f"clock. Lower the tick, or raise max_gap with sim_init --force."
            )

        # Nothing else in this process may poll crons: cron_spawn iterates
        # range(0) and our own thread becomes the only one (server.py:620-633).
        odoo.tools.config['max_cron_threads'] = 0

        # Bring the ORM up in *this* thread before the workers exist, so that
        # neither of them races the module-import machinery on first use.
        odoo.service.server.load_server_wide_modules()
        registry = Registry(dbname)

        pulse_enabled = not opt.game_no_pulse and 'bus.bus' in registry
        if not pulse_enabled and not opt.game_no_pulse:
            _logger.warning(
                "bus is not installed on %s: no world pulse will be published, "
                "so clients cannot tell a running world from a dead one", dbname,
            )
        if pulse_enabled:
            self._check_bus_retention(dbname, registry, clock)

        _logger.info(
            "game world %s: at %s, rate %sx, %s",
            dbname, clock.game_now, clock.rate,
            "PAUSED" if clock.paused else f"freezing after {max_gap}s without a tick",
        )
        if clock.paused:
            _logger.warning(
                "the world is paused; time will not move until "
                "`odoo-bin sim_pause -d %s --resume`", dbname,
            )

        GameLoop(dbname, opt.game_clock_tick, opt.game_cron_tick, pulse_enabled).start()

        # The registry is already loaded, so there is nothing left to preload.
        odoo.service.server.start(preload=[], stop=False)

    @staticmethod
    def _check_bus_retention(dbname, registry, clock):
        """Warn if the bus keeps its backlog for an absurdly short *real* time.

        `bus.bus._gc_messages` subtracts `bus.gc_retention_seconds` from
        `fields.Datetime.now()` and compares against `create_date`
        (`addons/bus/models/bus.py:97-108`). Both are game time, so the window
        is denominated in **game** seconds: the 24-hour default is sixty real
        seconds at K=1440. That window is how far back a reconnecting client
        can replay, so on a fast world it is worth setting deliberately.

        Correct code, surprising outcome -- see DESIGN.md 5.12.
        """
        from odoo.addons.bus.models.bus import DEFAULT_GC_RETENTION_SECONDS  # noqa: PLC0415

        with odoo.sql_db.db_connect(dbname).cursor() as cr:
            env = api.Environment(cr, api.SUPERUSER_ID, {})
            retention = int(env['ir.config_parameter'].sudo().get_param(
                'bus.gc_retention_seconds', DEFAULT_GC_RETENTION_SECONDS,
            ))
        real_window = timedelta(seconds=retention / clock.rate)
        if real_window < timedelta(hours=1):
            _logger.warning(
                "bus.gc_retention_seconds is %s game seconds, which at rate %sx is "
                "only %s of real time: a client offline longer than that cannot "
                "replay what it missed. Set it in game seconds for the real window "
                "you want (one real hour is %d).",
                retention, clock.rate, real_window, int(3600 * clock.rate),
            )
