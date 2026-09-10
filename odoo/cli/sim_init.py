import logging
import optparse
import sys
from datetime import datetime, timedelta

import odoo.game_clock
import odoo.sql_db
import odoo.tools.config

from . import Command

_logger = logging.getLogger(__name__)


def starting_instant(cr, game_start, existing):
    """ Return the game instant a world should start, or restart, at.

    :param game_start: the ``--game-start`` argument, or ``None``
    :param existing: the world's current clock when overwriting, else ``None``
    :raises ValueError: if ``game_start`` is not ISO 8601

    Carrying an existing world's instant across is what makes changing the rate
    between runs safe: without it, ``--force`` would rewind the world to the
    present and break the monotonicity everything else relies on.  It used to be
    the operator's job to remember ``--game-start`` (DESIGN.md 7-8).
    """
    if game_start:
        return datetime.fromisoformat(game_start).replace(tzinfo=None)
    if existing is not None:
        return existing.now()
    # Anchor on the server clock rather than this process's, so that a fresh
    # world starts where the database thinks "now" is.
    cr.execute("SELECT (pg_catalog.now() AT TIME ZONE 'UTC')")
    return cr.fetchone()[0]


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
    played.  Until a loop ticks it, a new world drifts one `--max-gap` worth of
    game time forward and then stands still there.
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

            try:
                game_now = starting_instant(cr, opt.sim_game_start, existing)
            except ValueError:
                sys.exit(f"--game-start is not a valid ISO 8601 datetime: {opt.sim_game_start!r}")
            if existing is not None and not opt.sim_game_start:
                _logger.info("Carrying the existing game instant across: %s", game_now)

            clock = odoo.game_clock.install(
                cr, game_now, opt.sim_rate, timedelta(seconds=opt.sim_max_gap),
            )
            cr.commit()

        # Not "still": readers interpolate from the last tick until the clamp
        # binds, so an untended world drifts one max_gap * rate forward and
        # settles there.  Bounded and harmless, but say so rather than promise
        # a stillness the first reader will disprove.
        settles = timedelta(seconds=opt.sim_max_gap * clock.rate)
        _logger.info(
            "%s is now a game world: at %s, running at %sx once ticked",
            dbname, clock.game_now, clock.rate,
        )
        _logger.info(
            "Nothing is ticking it, so it will drift %s to %s and stand still there. "
            "Run `odoo-bin game_run -d %s` to start time.",
            settles, clock.game_now + settles, dbname,
        )
