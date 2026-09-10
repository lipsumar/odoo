"""``odoo-bin game_run`` -- run a world: its clock, its crons, and its UI.

This process owns game progression.  It is the only thing allowed to fire crons
on its world (``--max-cron-threads=0`` is forced, so ``cron_spawn`` iterates
``range(0)``), and the only thing that advances the clock: game time is an
accumulator that moves when this loop ticks it and stands still otherwise, so a
world does not age while nobody is playing.

Argument parsing and bootstrap only.  The behaviour lives in
:mod:`odoo.addons.odoo_sim.loop`, where it can be tested without a thread.
"""
import logging
import optparse
import sys

import odoo
import odoo.service.server
import odoo.sql_db
import odoo.tools.config
from odoo import api, game_clock
from odoo.addons.odoo_sim import loop
from odoo.cli import Command
from odoo.orm.registry import Registry

_logger = logging.getLogger(__name__)


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

        if error := loop.tick_headroom_error(opt.game_clock_tick, clock.max_gap):
            sys.exit(error)

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
        if pulse_enabled and (warning := self._retention_warning(dbname, clock)):
            _logger.warning("%s", warning)

        _logger.info(
            "game world %s: at %s, rate %sx, %s",
            dbname, clock.game_now, clock.rate,
            "PAUSED" if clock.paused
            else f"freezing after {clock.max_gap.total_seconds()}s without a tick",
        )
        if clock.paused:
            _logger.warning(
                "the world is paused; time will not move until "
                "`odoo-bin sim_pause -d %s --resume`", dbname,
            )

        loop.GameLoop(dbname, opt.game_clock_tick, opt.game_cron_tick, pulse_enabled).start()

        # The registry is already loaded, so there is nothing left to preload.
        odoo.service.server.start(preload=[], stop=False)

    @staticmethod
    def _retention_warning(dbname, clock):
        """Read this world's bus retention and judge it (DESIGN.md 5.12)."""
        from odoo.addons.bus.models.bus import DEFAULT_GC_RETENTION_SECONDS  # noqa: PLC0415

        with odoo.sql_db.db_connect(dbname).cursor() as cr:
            env = api.Environment(cr, api.SUPERUSER_ID, {})
            retention = int(env['ir.config_parameter'].sudo().get_param(
                'bus.gc_retention_seconds', DEFAULT_GC_RETENTION_SECONDS,
            ))
        return loop.retention_warning(retention, clock.rate)
