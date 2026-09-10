import logging
import optparse
import sys
from datetime import datetime, timedelta

import odoo.game_clock
import odoo.sql_db
import odoo.tools.config

from . import Command

_logger = logging.getLogger(__name__)


class SimInit(Command):
    """Turn a database into an odoo-sim game world with an accelerated clock."""

    name = 'sim_init'

    description = """
    Create the game clock of the database given by -d: a one-row `game_clock`
    table holding the current game instant, the instant of the last tick, the
    rate and the pause flag, plus a `public.now()` SQL function reading it so
    that raw SQL follows game time too.

    The clock does not advance on its own.  It is an accumulator ticked by the
    game loop (`odoo-bin game_run`), so a world only ages while it is being
    played.  A freshly created world sits still at --game-start until a loop
    ticks it for the first time.
    """

    def run(self, args):
        parser = odoo.tools.config.parser
        parser.prog = self.prog
        group = optparse.OptionGroup(parser, "Sim", "Create the game world of the database given by `-d`.")
        group.add_option("--rate", dest="sim_rate", type="float", default=60.0,
                         help="game seconds per real second (default: %default)")
        group.add_option("--game-start", dest="sim_game_start", default=None,
                         help="ISO 8601 UTC instant the world starts at "
                              "(default: real now, or the world's current game "
                              "instant when overwriting with --force)")
        group.add_option("--max-gap", dest="sim_max_gap", type="float",
                         default=odoo.game_clock.DEFAULT_MAX_GAP.total_seconds(),
                         help="real seconds without a tick after which the world "
                              "stops advancing (default: %default)")
        group.add_option("--force", action="store_true", dest="sim_force",
                         help="overwrite an existing game clock")
        parser.add_option_group(group)
        opt = odoo.tools.config.parse_config(args, setup_logging=True)

        dbnames = odoo.tools.config['db_name']
        if not dbnames:
            _logger.error('sim_init needs a database name. Use "-d" argument')
            sys.exit(1)
        if len(dbnames) > 1:
            sys.exit("-d/--database/db_name has multiple databases, please provide a single one")
        dbname = dbnames[0]

        if opt.sim_rate <= 0:
            sys.exit(f"--rate must be strictly positive, got {opt.sim_rate!r}")
        if opt.sim_max_gap <= 0:
            sys.exit(f"--max-gap must be strictly positive, got {opt.sim_max_gap!r}")

        with odoo.sql_db.db_connect(dbname).cursor() as cr:
            existing = None
            cr.execute("SELECT to_regclass('public.game_clock')")
            if cr.fetchone()[0] is not None:
                existing = odoo.game_clock.clock_for(dbname, cr=cr)
                if existing is not None and not opt.sim_force:
                    sys.exit(
                        f"{dbname} is already a game world (at {existing.game_now} "
                        f"running at rate {existing.rate}). Use --force to overwrite."
                    )

            if opt.sim_game_start:
                try:
                    game_now = datetime.fromisoformat(opt.sim_game_start).replace(tzinfo=None)
                except ValueError:
                    sys.exit(f"--game-start is not a valid ISO 8601 datetime: {opt.sim_game_start!r}")
            elif existing is not None:
                # Overwriting a live world: carry its game instant across rather
                # than rewinding it to the present.  Changing the rate between
                # runs is the supported escape hatch (DESIGN.md 3.2), and it
                # must not move game time backwards.
                game_now = existing.now()
                _logger.info("Carrying the existing game instant across: %s", game_now)
            else:
                # Anchor on the server clock rather than this process's, so that
                # a fresh world starts where the database thinks "now" is.
                cr.execute("SELECT (pg_catalog.now() AT TIME ZONE 'UTC')")
                game_now = cr.fetchone()[0]

            clock = odoo.game_clock.install(
                cr, game_now, opt.sim_rate, timedelta(seconds=opt.sim_max_gap),
            )
            cr.commit()

        _logger.info(
            "%s is now a game world: at %s, running at %sx once ticked "
            "(freezes after %ss without a tick)",
            dbname, clock.game_now, clock.rate, opt.sim_max_gap,
        )
        _logger.info("The clock is still: run `odoo-bin game_run -d %s` to start time.", dbname)
