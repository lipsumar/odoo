import logging
import optparse
import sys
from datetime import datetime

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
    table holding the anchors and the rate, and a `public.now()` SQL function
    that inlines them so that raw SQL follows game time too.

    The clock is fixed for the lifetime of the world.  Editing the row by hand
    has no effect until the function is regenerated and the workers restarted;
    that awkwardness is deliberate, it is not a runtime control surface.
    """

    def run(self, args):
        parser = odoo.tools.config.parser
        parser.prog = self.prog
        group = optparse.OptionGroup(parser, "Sim", "Create the game world of the database given by `-d`.")
        group.add_option("--rate", dest="sim_rate", type="float", default=60.0,
                         help="game seconds per real second (default: %default)")
        group.add_option("--game-start", dest="sim_game_start", default=None,
                         help="ISO 8601 UTC instant the world starts at (default: now)")
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

        with odoo.sql_db.db_connect(dbname).cursor() as cr:
            cr.execute("SELECT to_regclass('public.game_clock')")
            if cr.fetchone()[0] is not None and not opt.sim_force:
                cr.execute("SELECT anchor_game, rate FROM public.game_clock")
                if row := cr.fetchone():
                    sys.exit(
                        f"{dbname} is already a game world (started {row[0]} at rate {row[1]}). "
                        f"Use --force to overwrite."
                    )

            # Anchor on the server clock rather than this process's, so that the
            # SQL function and the database agree even if they drift.
            cr.execute("SELECT (pg_catalog.now() AT TIME ZONE 'UTC')")
            anchor_real = cr.fetchone()[0]

            if opt.sim_game_start:
                try:
                    anchor_game = datetime.fromisoformat(opt.sim_game_start).replace(tzinfo=None)
                except ValueError:
                    sys.exit(f"--game-start is not a valid ISO 8601 datetime: {opt.sim_game_start!r}")
            else:
                anchor_game = anchor_real

            clock = odoo.game_clock.install(cr, anchor_real, anchor_game, opt.sim_rate)
            cr.commit()

        _logger.info(
            "%s is now a game world: starts at %s (real %s), running at %sx",
            dbname, clock.anchor_game, clock.anchor_real, clock.rate,
        )
