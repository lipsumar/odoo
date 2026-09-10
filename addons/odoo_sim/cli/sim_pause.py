"""``odoo-bin sim_pause`` -- stop or restart game time on a world."""
import logging
import optparse
import sys

import odoo.game_clock
import odoo.sql_db
import odoo.tools.config

from odoo.cli import Command

_logger = logging.getLogger(__name__)


class SimPause(Command):
    """Pause or resume the game clock of a world."""

    name = 'sim_pause'

    description = """
    Flip the `paused` flag of the world given by -d, settling game time up to
    this instant first so that no time is lost or gained by the transition.

    A paused world is frozen for every reader at once, whether or not a game
    loop is running.  This is the explicit pause; the loop also freezes a world
    implicitly when it stops ticking for longer than the world's max_gap.
    """

    def run(self, args):
        parser = odoo.tools.config.parser
        parser.prog = self.prog
        group = optparse.OptionGroup(parser, "Sim", "Pause the world given by `-d`.")
        group.add_option("--resume", action="store_true", dest="sim_resume",
                         help="resume instead of pausing")
        parser.add_option_group(group)
        opt = odoo.tools.config.parse_config(args, setup_logging=True)

        dbnames = odoo.tools.config['db_name']
        if len(dbnames) != 1:
            sys.exit('sim_pause needs exactly one database. Use "-d" argument')
        dbname = dbnames[0]

        paused = not opt.sim_resume
        with odoo.sql_db.db_connect(dbname).cursor() as cr:
            if odoo.game_clock.clock_for(dbname, cr=cr) is None:
                sys.exit(f"{dbname} is not a game world.")
            clock = odoo.game_clock.set_paused(cr, paused)
            cr.commit()

        _logger.info(
            "%s is now %s at game time %s",
            dbname, "paused" if clock.paused else "running", clock.game_now,
        )
