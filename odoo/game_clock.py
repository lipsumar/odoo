"""Accelerated game clock for odoo-sim.

A *game world* is an Odoo database whose business-level "now" runs at a fixed
multiplier over real time -- and, crucially, only advances while the game loop
is ticking it::

    game_now = <game time at the last tick> + clamp(real_now - last_tick) * rate

The clock is therefore an accumulator owned by the loop, not a function of the
wall clock: a world that nobody is playing does not age.  See ``DESIGN.md``
section 3.3 for why that reverses the original design, and section 3.1 for what
the original bought.

A world can also be *forwarding* (section 3.4): sent on to an instant ahead,
the next working morning, with everything due on the way happening at the
instant it was due.  Readers see time stand still while the loop moves it from
one due event to the next.

The state lives in a one-row ``game_clock`` table (see :func:`install`).  A
database is a game world if and only if that table exists and is populated;
there is no environment variable or configuration flag to keep in sync.

The same arithmetic exists twice: here, for everything that reads the clock
from Python (:meth:`odoo.sql_db.BaseCursor.now`, the ``fields.Date`` /
``fields.Datetime`` helpers), and in a generated ``public.now()`` SQL function
that shadows ``pg_catalog.now()`` for raw SQL in business queries.
"""
from __future__ import annotations

import logging
import threading
import time
import typing
from datetime import datetime, timedelta

if typing.TYPE_CHECKING:
    from odoo.sql_db import BaseCursor

_logger = logging.getLogger(__name__)

_ZERO = timedelta(0)

#: Default real-time gap after which a clock is assumed to be unattended and
#: stops advancing.  It bounds two things at once: how long a legitimately
#: delayed clock thread may take before the world freezes under it, and how
#: much game time a crashed or suspended process can accrue before the freeze
#: binds (``max_gap * rate``).  Overridable per world.
#:
#: It is also how quickly anything watching the world notices that it died --
#: the UI locks its controls after this long without a pulse -- so retuning it
#: retunes that too.
DEFAULT_MAX_GAP = timedelta(seconds=5)

#: How long a process may reuse a clock reading before re-reading the row.
#: Interpolation between ticks is exact (DESIGN.md 3.3), so a stale reading is
#: still the right answer while the loop ticks normally; this only bounds how
#: late a process notices a pause, a resume, or a loop that died.
CACHE_TTL = 1.0  # real seconds, measured on time.monotonic()

#: Whether each database seen by this process is a game world.  Cached for the
#: life of the process: ``Cursor.__init__`` asks on every connection, and the
#: answer only changes when ``sim_init`` runs.
_worlds: dict[str, bool] = {}

#: Last clock reading of each database, with the ``time.monotonic()`` at which
#: it was taken.  ``None`` as the timestamp means "pinned, never expires" and
#: is used by :func:`override` in tests.
_readings: dict[str, tuple[GameClock, float | None]] = {}

#: Re-entrancy guard: reading the clock opens a cursor, and building a cursor
#: asks whether the database is a game world.
_loading = threading.local()


class GameClock:
    """One reading of one world's clock.

    Immutable, and only valid for interpolation: ``game_now`` and
    ``last_tick_real`` move every time the loop ticks.  Interpolating from a
    stale reading gives the same answer as a fresh one for as long as the loop
    keeps ticking (DESIGN.md 3.3), which is what makes :data:`CACHE_TTL`
    liveable.
    """

    __slots__ = ('forward_to', 'game_now', 'last_tick_real', 'max_gap', 'paused', 'rate')

    def __init__(
        self,
        game_now: datetime,
        last_tick_real: datetime,
        rate: float,
        paused: bool = False,
        max_gap: timedelta = DEFAULT_MAX_GAP,
        forward_to: datetime | None = None,
    ):
        """
        :param game_now: game UTC instant as of the last tick (naive)
        :param last_tick_real: real UTC instant of that tick (naive)
        :param rate: game seconds per real second, strictly positive
        :param paused: whether the world is explicitly paused
        :param max_gap: how long an untended clock may advance for
        :param forward_to: game UTC instant (naive) the world is forwarding
            to, or ``None`` when it is not
        """
        if rate <= 0:
            raise ValueError(f"game clock rate must be strictly positive, got {rate!r}")
        if max_gap <= _ZERO:
            raise ValueError(f"game clock max_gap must be strictly positive, got {max_gap!r}")
        self.game_now = game_now
        self.last_tick_real = last_tick_real
        self.rate = float(rate)
        self.paused = paused
        self.max_gap = max_gap
        self.forward_to = forward_to

    @property
    def forwarding(self) -> bool:
        """ Whether the loop is moving this world on to ``forward_to``. """
        return self.forward_to is not None

    def game_at(self, real: datetime) -> datetime:
        """ Return the game time corresponding to the naive UTC instant ``real``.

        Mirrors the ``public.now()`` generated by :meth:`sql_function` exactly,
        clamp for clamp.
        """
        if self.paused or self.forwarding:
            # A forwarding world stands still for readers too: the loop moves
            # game_now itself, to each instant something is due (DESIGN.md
            # 3.4), and whatever runs there must see exactly that instant.
            return self.game_now
        elapsed = real - self.last_tick_real
        if elapsed < _ZERO:
            # ``last_tick_real`` is PostgreSQL's wall clock and ``real`` is this
            # process's; they can disagree.  Never let that move game time back.
            elapsed = _ZERO
        elif elapsed > self.max_gap:
            elapsed = self.max_gap
        return self.game_now + elapsed * self.rate

    def now(self) -> datetime:
        """ Return the current game time, as a naive UTC datetime. """
        # datetime.now() (and not time.monotonic()) on purpose: the process runs
        # with TZ=UTC (odoo/_monkeypatches/__init__.py), and reading the wall
        # clock is what lets freezegun drive this clock in tests.
        return self.game_at(datetime.now())

    @staticmethod
    def sql_function() -> str:
        """ Return the DDL of the ``public.now()`` that mirrors this clock.

        ``pg_catalog.now()`` (transaction start time) and not
        ``clock_timestamp()``, so that the result keeps Odoo's documented
        "transaction's timestamp" semantics and the function is honestly STABLE.

        Nothing is inlined any more -- the function reads the row -- so it never
        needs regenerating when the clock moves, pauses or resumes.
        """
        return f"""
            CREATE OR REPLACE FUNCTION public.now() RETURNS timestamptz AS $$
                SELECT {_SETTLED_GAME_NOW}
                  FROM public.game_clock;
            $$ LANGUAGE sql STABLE;
        """

    def __repr__(self):
        return (f"GameClock(game_now={self.game_now!r}, "
                f"last_tick_real={self.last_tick_real!r}, rate={self.rate!r}, "
                f"paused={self.paused!r}, max_gap={self.max_gap!r}, "
                f"forward_to={self.forward_to!r})")


#: Game time as of this transaction, from the row: the one expression
#: ``public.now()``, a tick and a pause all share.  Paused and forwarding
#: worlds stand still.
_SETTLED_GAME_NOW = """
    CASE
      WHEN paused OR forward_to IS NOT NULL THEN game_now
      ELSE game_now
         + LEAST(
               GREATEST(pg_catalog.now() - last_tick_real, INTERVAL '0'),
               max_gap
           ) * rate
    END
"""

#: What a reading is made of, in :class:`GameClock` argument order.
_COLUMNS = """
    game_now AT TIME ZONE 'UTC',
    last_tick_real AT TIME ZONE 'UTC',
    rate, paused, max_gap,
    forward_to AT TIME ZONE 'UTC'
"""

_SELECT_CLOCK = f"SELECT {_COLUMNS} FROM public.game_clock"

#: Settle game time up to this instant, then optionally flip ``paused``.
#: ``last_tick_real`` moves even while paused, so a paused stretch is never
#: accrued and resuming does not jump.
_ADVANCE_CLOCK = f"""
    UPDATE public.game_clock
       SET game_now = {_SETTLED_GAME_NOW},
           last_tick_real = pg_catalog.now(),
           paused = COALESCE(%s, paused)
 RETURNING {_COLUMNS}
"""

#: A tick leaves a forwarding world's row alone: the loop is writing it at
#: every step, and a tick writing it too would fight each step for the row.
_TICK = f"""
    UPDATE public.game_clock
       SET game_now = {_SETTLED_GAME_NOW},
           last_tick_real = pg_catalog.now()
     WHERE forward_to IS NULL
 RETURNING {_COLUMNS}
"""

_START_FORWARD = f"""
    UPDATE public.game_clock
       SET game_now = {_SETTLED_GAME_NOW},
           last_tick_real = pg_catalog.now(),
           forward_to = %s AT TIME ZONE 'UTC'
     WHERE forward_to IS NULL
 RETURNING {_COLUMNS}
"""

#: Never past the target, and never back.
_STEP_FORWARD = f"""
    UPDATE public.game_clock
       SET game_now = GREATEST(game_now, LEAST(%s AT TIME ZONE 'UTC', forward_to)),
           last_tick_real = pg_catalog.now()
     WHERE forward_to IS NOT NULL
 RETURNING {_COLUMNS}
"""

#: ``GREATEST``: the target is computed from a reading taken a moment before
#: the forward started, so on a fast world game time may just have passed it.
_FINISH_FORWARD = f"""
    UPDATE public.game_clock
       SET game_now = GREATEST(game_now, forward_to),
           last_tick_real = pg_catalog.now(),
           forward_to = NULL
     WHERE forward_to IS NOT NULL
 RETURNING {_COLUMNS}
"""


def _remember(dbname: str, clock: GameClock | None) -> GameClock | None:
    """ Cache ``clock`` as ``dbname``'s reading, unless a newer one is known.

    Every write to the row sets ``last_tick_real`` to its transaction's
    instant, so a reading with an older one was taken through an older
    snapshot -- a request that began before the loop's last write, say.
    Caching it would hand every thread in this process a clock that is behind.
    While the loop runs normally that is harmless, since interpolation is
    exact; across a pause, or a step of a forward, it is not: the jobs a
    forward runs at 23:00 would stamp their records with the step before.

    Returns ``clock`` either way: it is the right answer for its own snapshot.
    """
    _worlds[dbname] = clock is not None
    if clock is None:
        _readings.pop(dbname, None)
        return None
    known = _readings.get(dbname)
    if known is None or known[0].last_tick_real <= clock.last_tick_real:
        _readings[dbname] = (clock, time.monotonic())
    return clock


def _read(cr: BaseCursor) -> GameClock | None:
    """ Read the clock of ``cr``'s database, or ``None`` if it is not a world. """
    cr.execute("SELECT to_regclass('public.game_clock')")
    if cr.fetchone()[0] is None:
        return None
    cr.execute(_SELECT_CLOCK)
    row = cr.fetchone()
    return GameClock(*row) if row else None


def read(cr: BaseCursor) -> GameClock | None:
    """ Read the clock of ``cr``'s database afresh, bypassing the cache.

    For decisions that must not act on a reading up to :data:`CACHE_TTL` old:
    whether a world is already forwarding, say.
    """
    return _remember(cr.dbname, _read(cr))


def tick(cr: BaseCursor) -> GameClock | None:
    """ Advance the clock by the real time elapsed since the last tick.

    This is the whole of the game loop's clock thread, and also the whole of
    crash recovery: the statement clamps its own elapsed term, so a restart
    after a kill or a laptop suspend is an ordinary tick that happens to have
    been a long time coming.

    While the world is forwarding, a tick writes nothing and returns the
    reading as it stands: the loop moves time then (:func:`step_forward`).
    """
    cr.execute(_TICK)
    row = cr.fetchone()
    if row is None:
        return read(cr)
    return _remember(cr.dbname, GameClock(*row))


def set_paused(cr: BaseCursor, paused: bool) -> GameClock | None:
    """ Pause or resume the world, settling game time up to this instant.

    A forward carries on regardless, and the world arrives paused or not as
    this last left it.
    """
    return _write(cr, _ADVANCE_CLOCK, paused)


def start_forward(cr: BaseCursor, target: datetime) -> GameClock | None:
    """ Send the world on to the game instant ``target`` (naive UTC).

    Settles game time up to this instant and freezes it there for every
    reader.  Nothing moves it until the game loop does, event by event
    (DESIGN.md 3.4), so a world nothing is ticking stays frozen: check
    :func:`is_ticking` first.

    Returns ``None``, and changes nothing, if the world is already forwarding.
    """
    return _write(cr, _START_FORWARD, target)


def step_forward(cr: BaseCursor, instant: datetime) -> GameClock | None:
    """ Move a forwarding world to the game instant ``instant`` (naive UTC).

    Clamped to the target, and never backwards.  ``None`` if not forwarding.
    """
    return _write(cr, _STEP_FORWARD, instant)


def finish_forward(cr: BaseCursor) -> GameClock | None:
    """ Put a forwarding world at its target, and let time run from there.

    ``None`` if it was not forwarding.
    """
    return _write(cr, _FINISH_FORWARD)


def _write(cr: BaseCursor, statement: str, *params) -> GameClock | None:
    cr.execute(statement, params)
    row = cr.fetchone()
    return _remember(cr.dbname, GameClock(*row)) if row else None


def clock_for(dbname: str | None, cr: BaseCursor | None = None) -> GameClock | None:
    """ Return the clock of ``dbname``, or ``None`` if it is not a game world.

    Pass ``cr`` when a cursor on that database is already open: the reading is
    one single-row SELECT, and taking it on an existing transaction avoids
    opening a connection just to ask the time.
    """
    if not dbname:
        return None

    if _worlds.get(dbname) is False:
        return None

    reading = _readings.get(dbname)
    if reading is not None:
        clock, fetched_at = reading
        if fetched_at is None or (time.monotonic() - fetched_at) < CACHE_TTL:
            return clock

    if getattr(_loading, 'busy', False):
        # We are inside the query below; the cursor it opens must not recurse.
        return reading[0] if reading is not None else None

    _loading.busy = True
    try:
        if cr is not None:
            clock = _read(cr)
        else:
            from odoo.sql_db import db_connect  # noqa: PLC0415 (circular at module level)
            with db_connect(dbname, readonly=False).cursor() as own_cr:
                clock = _read(own_cr)
    except Exception:  # noqa: BLE001 - never let the clock break a connection
        # Do not cache a transient failure: the database may not be reachable
        # yet, or may not be an Odoo database at all.  Keep serving the last
        # good reading if we have one -- interpolation degrades gracefully.
        _logger.warning("Could not read the game clock of %s", dbname, exc_info=True)
        return reading[0] if reading is not None else None
    finally:
        _loading.busy = False

    if clock is not None and dbname not in _worlds:
        _logger.info("Database %s is a game world: %r", dbname, clock)
    return _remember(dbname, clock)


def is_sim_database(dbname: str | None) -> bool:
    """ Return whether ``dbname`` is a game world.

    Asked on every ``Cursor`` construction, so it must stay cheap: the answer
    is cached for the life of the process and never re-read.
    """
    if not dbname:
        return False
    try:
        return _worlds[dbname]
    except KeyError:
        return clock_for(dbname) is not None


def current_clock() -> GameClock | None:
    """ Return the clock of the database this thread is working on.

    ``fields.Datetime.now()`` and friends are static methods with no cursor to
    ask, so the database is resolved from the current thread -- which the HTTP
    dispatcher, the RPC layer, the cron runner and ``odoo-bin shell`` all set --
    falling back to the single configured database.  There is one world per
    database (DESIGN.md section 9), so that fallback is unambiguous.

    Note that ``IrCron._process_jobs`` *deletes* ``thread.dbname`` when it
    returns (DESIGN.md 5.8), so a game loop must re-set it after every tick.
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


def is_ticking(clock: GameClock | None, real: datetime | None = None) -> bool:
    """ Return whether a game loop is ticking ``clock``'s world.

    Paused or not: a paused world's loop keeps ticking it.  This is what
    decides whether anything will act on a request to move time -- a forward
    asked of a world nothing ticks would leave it frozen for good.

    A database that is not a game world is always "ticking".
    """
    if clock is None:
        return True
    return (real if real is not None else datetime.now()) - clock.last_tick_real < clock.max_gap


def is_running(clock: GameClock | None, real: datetime | None = None) -> bool:
    """ Return whether ``clock``'s world is actually progressing.

    False when the world is paused or forwarding, and when nothing has ticked
    it for longer than its ``max_gap`` -- at which point game time has stopped
    for every reader, even though PostgreSQL, the web workers and the HTTP
    stack are all still up and happily accepting writes.

    That combination is the one hazard the clamp does not remove by itself: a
    player acting into a world that is not running gets records stamped with a
    frozen ``create_date``, and no cron will ever process the consequences.
    Guard write paths with this rather than re-deriving it per endpoint.

    A database that is not a game world is always "running": ordinary Odoo has
    no loop to stop.
    """
    if clock is None:
        return True
    if clock.paused or clock.forwarding:
        return False
    return is_ticking(clock, real)


def install(
    cr: BaseCursor,
    game_now: datetime,
    rate: float,
    max_gap: timedelta = DEFAULT_MAX_GAP,
) -> GameClock:
    """ Create (or reset) the game clock of ``cr``'s database.

    The world starts paused-in-effect at ``game_now``: ``last_tick_real`` is set
    to the database's clock, so no time accrues until a loop ticks it.
    """
    if rate <= 0:
        raise ValueError(f"game clock rate must be strictly positive, got {rate!r}")
    if max_gap <= _ZERO:
        raise ValueError(f"game clock max_gap must be strictly positive, got {max_gap!r}")

    cr.execute("""
        CREATE TABLE IF NOT EXISTS public.game_clock (
            id             boolean PRIMARY KEY DEFAULT true CHECK (id),
            game_now       timestamptz NOT NULL,
            last_tick_real timestamptz NOT NULL,
            rate           double precision NOT NULL CHECK (rate > 0),
            paused         boolean NOT NULL DEFAULT false,
            max_gap        interval NOT NULL CHECK (max_gap > INTERVAL '0'),
            forward_to     timestamptz
        )
    """)
    cr.execute(GameClock.sql_function())
    cr.execute("DELETE FROM public.game_clock")
    cr.execute("""
        INSERT INTO public.game_clock (game_now, last_tick_real, rate, paused, max_gap)
        VALUES (%s AT TIME ZONE 'UTC', pg_catalog.now(), %s, false, %s)
    """, [game_now, rate, max_gap])

    invalidate(cr.dbname)
    clock = _read(cr)
    assert clock is not None, "the clock we just installed must be readable"
    return _remember(cr.dbname, clock)


def invalidate(dbname: str | None = None) -> None:
    """ Drop the cached clock of ``dbname``, or of every database. """
    if dbname is None:
        _worlds.clear()
        _readings.clear()
    else:
        _worlds.pop(dbname, None)
        _readings.pop(dbname, None)


def override(dbname: str, clock: GameClock | None) -> None:
    """ Force the clock of ``dbname`` without touching the database (tests).

    The pinned reading never expires, so a test keeps the clock it asked for.
    """
    if clock is None:
        _worlds[dbname] = False
        _readings.pop(dbname, None)
    else:
        _worlds[dbname] = True
        _readings[dbname] = (clock, None)
