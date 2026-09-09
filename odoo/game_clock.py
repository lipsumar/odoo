"""Accelerated game clock for odoo-sim.

A *game world* is an Odoo database whose business-level "now" runs at a fixed
multiplier over real time::

    game_now = anchor_game + (real_now - anchor_real) * rate

The three constants are fixed for the lifetime of the world and live in a
one-row ``game_clock`` table (see :func:`install`).  A database is a game world
if and only if that table exists and is populated; there is no environment
variable or configuration flag to keep in sync.

The same arithmetic exists twice: here, for everything that reads the clock
from Python (:meth:`odoo.sql_db.BaseCursor.now`, the ``fields.Date`` /
``fields.Datetime`` helpers), and in a generated ``public.now()`` SQL function
that shadows ``pg_catalog.now()`` for raw SQL in business queries.

See ``odoo_sim/DESIGN.md`` for the full rationale.
"""
from __future__ import annotations

import logging
import threading
import typing
from datetime import datetime, timezone

if typing.TYPE_CHECKING:
    from odoo.sql_db import BaseCursor

_logger = logging.getLogger(__name__)

#: Cache of the clock of each database seen by this process.  ``None`` means
#: "not a game world".  The clock row is immutable, so this never needs
#: invalidating outside of tests.
_clocks: dict[str, GameClock | None] = {}

#: Re-entrancy guard: loading a clock opens a cursor, and building a cursor
#: asks whether the database is a game world.
_loading = threading.local()


class GameClock:
    """The mapping from real time to game time of one world."""

    __slots__ = ('anchor_game', 'anchor_real', 'rate')

    def __init__(self, anchor_real: datetime, anchor_game: datetime, rate: float):
        """
        :param anchor_real: real UTC instant at which the world was created (naive)
        :param anchor_game: game UTC instant corresponding to ``anchor_real`` (naive)
        :param rate: game seconds per real second, strictly positive
        """
        if rate <= 0:
            raise ValueError(f"game clock rate must be strictly positive, got {rate!r}")
        self.anchor_real = anchor_real
        self.anchor_game = anchor_game
        self.rate = float(rate)

    def game_at(self, real: datetime) -> datetime:
        """ Return the game time corresponding to the naive UTC instant ``real``. """
        return self.anchor_game + (real - self.anchor_real) * self.rate

    def now(self) -> datetime:
        """ Return the current game time, as a naive UTC datetime. """
        # datetime.now() (and not time.monotonic()) on purpose: the process runs
        # with TZ=UTC (odoo/_monkeypatches/__init__.py), and reading the wall
        # clock is what lets freezegun drive this clock in tests.
        return self.game_at(datetime.now())

    def sql_function(self) -> str:
        """ Return the DDL of the ``public.now()`` that mirrors this clock.

        ``pg_catalog.now()`` (transaction start time) and not
        ``clock_timestamp()``, so that the result keeps Odoo's documented
        "transaction's timestamp" semantics and the function is honestly STABLE.
        """
        return f"""
            CREATE OR REPLACE FUNCTION public.now() RETURNS timestamptz AS $$
                SELECT TIMESTAMPTZ '{_sql_literal(self.anchor_game)}'
                     + (pg_catalog.now() - TIMESTAMPTZ '{_sql_literal(self.anchor_real)}')
                       * {self.rate!r};
            $$ LANGUAGE sql STABLE;
        """

    def __repr__(self):
        return (f"GameClock(anchor_real={self.anchor_real!r}, "
                f"anchor_game={self.anchor_game!r}, rate={self.rate!r})")


def _sql_literal(value: datetime) -> str:
    """ Render a naive UTC datetime as an unambiguous timestamptz literal. """
    return value.replace(tzinfo=timezone.utc).isoformat(sep=' ')


def clock_for(dbname: str | None) -> GameClock | None:
    """ Return the clock of ``dbname``, or ``None`` if it is not a game world. """
    if not dbname:
        return None
    try:
        return _clocks[dbname]
    except KeyError:
        pass

    if getattr(_loading, 'busy', False):
        # We are inside the query below; the cursor it opens must not recurse.
        return None

    _loading.busy = True
    try:
        from odoo.sql_db import db_connect  # noqa: PLC0415 (circular at module level)
        with db_connect(dbname, readonly=False).cursor() as cr:
            cr.execute("SELECT to_regclass('public.game_clock')")
            if cr.fetchone()[0] is None:
                clock = None
            else:
                cr.execute("""
                    SELECT anchor_real AT TIME ZONE 'UTC',
                           anchor_game AT TIME ZONE 'UTC',
                           rate
                      FROM public.game_clock
                """)
                row = cr.fetchone()
                clock = GameClock(*row) if row else None
    except Exception:  # noqa: BLE001 - never let the clock break a connection
        # Do not cache a transient failure: the database may not be reachable
        # yet, or may not be an Odoo database at all.
        _logger.warning("Could not read the game clock of %s", dbname, exc_info=True)
        return None
    finally:
        _loading.busy = False

    if clock is not None:
        _logger.info("Database %s is a game world: %r", dbname, clock)
    _clocks[dbname] = clock
    return clock


def is_sim_database(dbname: str | None) -> bool:
    """ Return whether ``dbname`` is a game world. """
    return clock_for(dbname) is not None


def current_clock() -> GameClock | None:
    """ Return the clock of the database this thread is working on.

    ``fields.Datetime.now()`` and friends are static methods with no cursor to
    ask, so the database is resolved from the current thread -- which the HTTP
    dispatcher, the RPC layer, the cron runner and ``odoo-bin shell`` all set --
    falling back to the single configured database.  There is one world per
    database (DESIGN.md section 9), so that fallback is unambiguous.
    """
    dbname = getattr(threading.current_thread(), 'dbname', None)
    if not dbname:
        from odoo.tools import config  # noqa: PLC0415 (circular at module level)
        db_names = config['db_name']
        if len(db_names) != 1:
            return None
        dbname = db_names[0]
    return clock_for(dbname)


def now() -> datetime:
    """ Return game time if this thread is inside a game world, else real time. """
    clock = current_clock()
    return clock.now() if clock is not None else datetime.now()


def install(cr: BaseCursor, anchor_real: datetime, anchor_game: datetime, rate: float) -> GameClock:
    """ Turn the database behind ``cr`` into a game world.

    Creates the one-row ``game_clock`` table, stores the constants, and
    generates the ``public.now()`` that inlines them.  The caller commits.
    """
    clock = GameClock(anchor_real, anchor_game, rate)
    cr.execute("""
        CREATE TABLE IF NOT EXISTS public.game_clock (
            id          boolean PRIMARY KEY DEFAULT true CHECK (id),
            anchor_real timestamptz NOT NULL,
            anchor_game timestamptz NOT NULL,
            rate        double precision NOT NULL
        )
    """)
    cr.execute("""
        INSERT INTO public.game_clock (id, anchor_real, anchor_game, rate)
             VALUES (true, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
                SET anchor_real = EXCLUDED.anchor_real,
                    anchor_game = EXCLUDED.anchor_game,
                    rate = EXCLUDED.rate
    """, [_sql_literal(anchor_real), _sql_literal(anchor_game), clock.rate])
    cr.execute(clock.sql_function())
    invalidate(cr.dbname)
    return clock


def invalidate(dbname: str | None = None) -> None:
    """ Drop the cached clock of ``dbname``, or of every database. """
    if dbname is None:
        _clocks.clear()
    else:
        _clocks.pop(dbname, None)


def override(dbname: str, clock: GameClock | None) -> None:
    """ Force the clock of ``dbname`` without touching the database (tests). """
    _clocks[dbname] = clock
