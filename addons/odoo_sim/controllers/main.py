# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The game's HTTP surface: the page, and the clock basis behind it.

See ``odoo_sim/UI_DESIGN.md`` sections 5.4 (the API), 5.5 (why the clock
endpoint hands out a basis rather than a time) and 5.7 (the build).

Unlike the loop, none of this works on an uninstalled module: routes and
templates come from the registry, so ``/game`` needs ``-i odoo_sim`` even
though ``odoo-bin game_run`` does not.
"""
from __future__ import annotations

import json
import logging

import werkzeug.exceptions

from odoo import game_clock, http
from odoo.addons.bus.websocket import WebsocketConnectionHandler
from odoo.addons.odoo_sim import pulse, workday
from odoo.addons.odoo_sim.controllers.world import _world
from odoo.addons.odoo_sim.models.game_world import CHANGED
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import file_open

_logger = logging.getLogger(__name__)

#: Where ``vite build`` writes, relative to the addons path.  Under
#: ``static/dist/`` and never ``static/src/``: Odoo's JS transpiler rewrites
#: anything under ``src`` into an Odoo module, which would take a foreign
#: bundle apart (UI_DESIGN.md 2.1, 6.5).
MANIFEST = 'odoo_sim/static/dist/.vite/manifest.json'

#: Public URL of that same directory.  Odoo streams ``/<module>/static/<path>``
#: straight off disk with no asset bundle in the way (UI_DESIGN.md 2.1).
DIST_URL = '/odoo_sim/static/dist/'

#: The frontend's entry point, spelled as Vite spells it in the manifest: keys
#: there are source paths relative to the frontend project root, never the
#: hashed output filenames.
ENTRY = 'src/main.js'


def _built_assets() -> tuple[list[str], list[str]]:
    """ Return ``(scripts, styles)`` for the built frontend; empty if unbuilt.

    Read on every render rather than cached.  The bundle is rebuilt constantly
    while this page is fetched about once per session, so a cache here would go
    on serving the *previous* build's filenames until someone restarted the
    server -- and since those filenames are content-hashed, that presents as a
    page that mysteriously ignores every change you make.  It is one small file
    off local disk on a route nothing calls in a loop.

    Content hashing is not optional: static responses carry ``STATIC_CACHE`` of
    seven days, so a plain ``main.js`` would be stale in every browser that had
    ever loaded it.  Vite hashes by default; the point is not to turn it off.

    An unbuilt tree is a normal state rather than an error -- the backend
    landed first -- so this returns empty lists and the template says so.
    """
    try:
        with file_open(MANIFEST) as manifest_file:
            entry = json.load(manifest_file)[ENTRY]
        return (
            [DIST_URL + entry['file']],
            [DIST_URL + href for href in entry.get('css', ())],
        )
    except FileNotFoundError:
        return [], []
    except (ValueError, KeyError, TypeError):
        # Built, but not in a shape we recognise: worth a line in the log,
        # because the page below will claim it was never built at all.
        _logger.warning("%s is unreadable or has no %r entry", MANIFEST, ENTRY, exc_info=True)
        return [], []


def _world_clock(fresh=False) -> game_clock.GameClock:
    """ Return this database's clock, or refuse if it is not a game world.

    ``clock_for`` may hand back a reading up to ``CACHE_TTL`` old, and that is
    fine for a read on purpose: interpolating from a basis is exact whatever
    the basis's age (DESIGN.md 3.3), so a one-second-old reading yields the
    same game time as a fresh one.  What it can delay by up to a second is
    noticing a pause or a resume, and the pulse corrects that within a tick.

    ``fresh`` reads the row itself, for a decision that must see a forward
    another page started a moment ago.
    """
    cr = request.env.cr
    clock = game_clock.read(cr) if fresh else game_clock.clock_for(request.db, cr)
    if clock is None:
        raise werkzeug.exceptions.NotFound(
            f"{request.db} is not a game world. "
            f"Run `odoo-bin sim_init -d {request.db}` to make it one."
        )
    return clock


class GameUi(http.Controller):
    """The standalone game page, and the API behind it."""

    @http.route('/game', type='http', auth='user')
    def index(self):
        """ Serve the game page: a hand-written document, not the web client.

        None of Odoo's frontend comes with it -- no asset bundle, no OWL, no
        menu loading (UI_DESIGN.md 2.2).  The one thing the server hands the
        page is a bootstrap blob, and it earns its place twice:

        * the first clock basis arrives *with* the document, so the page can
          show a time before its first request rather than after it;
        * the bus channel and notification type come from ``pulse.py`` rather
          than being spelled a second time in JavaScript.  Two copies of a
          channel name drift, and the symptom when they do is not an error but
          a UI that quietly never updates.

        The bus's client version rides along for the same reason.  ``/websocket``
        closes a browser's socket on sight unless its ``version`` parameter
        names the bus's own worker (``WebsocketConnectionHandler._VERSION``),
        and that value changes whenever the bus's client does.

        So does the world: its first state, and the type of the notice that
        says it changed (GAME_STATE.md 10).
        """
        scripts, styles = _built_assets()
        return request.render('odoo_sim.index', {
            'bootstrap': {
                'clock': pulse.payload(_world_clock()),
                'channel': pulse.CHANNEL,
                'type': pulse.TYPE,
                'changed_type': CHANGED,
                'world': _world()._snapshot(request.env.user),
                'websocket_version': WebsocketConnectionHandler._VERSION,
            },
            'scripts': scripts,
            'styles': styles,
        })

    @http.route('/game/api/clock', type='json2', auth='user', methods=['GET'])
    def clock(self):
        """ Return the tick basis -- which is the pulse payload, verbatim.

        **The identical shape is the design, not an economy.** A client sets its
        basis from this fetch and then refreshes the same basis from every pulse
        (UI_DESIGN.md 5.5), so both arrive at one code path in the browser.  Were
        the shapes allowed to differ, that would become two parsers for one
        concept, and the day they disagreed they would disagree about what time
        it is in the world.  Building both from ``pulse.payload`` makes that
        impossible rather than merely unlikely, and a test pins it.

        **This endpoint does not enforce ``is_running``.** It is a read, and it
        is precisely the read that tells a client the world has stopped:
        refusing it while the loop is dead would withhold the one fact the
        client needs in order to lock itself.  The ``is_running`` guard belongs
        on write paths (UI_DESIGN.md 5.6).

        Not ``readonly=True``, deliberately.  A readonly route may be answered
        from ``db_replica_host`` when one is configured (``sql_db.py``
        ``connection_info_for``), and a replica's ``game_clock`` row lags by
        however far behind it is running.  Staleness is the one property a
        basis must not have.
        """
        return pulse.payload(_world_clock())

    @http.route('/game/api/clock/pause', type='json2', auth='user', methods=['POST'])
    def pause(self, paused=True):
        """ Pause the world (``paused``), or resume it.

        Allowed whether or not a loop is ticking: the flag takes effect for
        every reader at once, loop or no loop.  Answers with the new reading,
        and tells every other page with a pulse of its own rather than leaving
        them a tick behind.
        """
        env = _world().env
        if not isinstance(paused, bool):
            raise UserError(env._("Pausing is yes or no."))
        cr = request.env.cr
        if _world_clock(fresh=True).forwarding:
            raise UserError(env._("The world is moving on to the next day: wait until it gets there."))
        clock = game_clock.set_paused(cr, paused)
        pulse.send(cr, clock)
        return pulse.payload(clock)

    @http.route('/game/api/clock/forward', type='json2', auth='user', methods=['POST'])
    def forward(self, tz=None):
        """ Send the world on to the next working morning (DESIGN.md 3.4).

        The morning is :data:`workday.DAY_START` in ``tz``, the time zone the
        page shows time in, falling back to the player's own.  The loop then
        moves time there one due event at a time; until it arrives the world
        is not running, so every action is refused.  A paused world stays
        paused through it, and arrives paused.

        Refused while nothing is ticking the world, since nothing would ever
        carry it out and the world would stay frozen.
        """
        env = _world().env
        cr = request.env.cr
        clock = _world_clock(fresh=True)
        if clock.forwarding:
            raise UserError(env._("The world is already moving on to the next day."))
        if not game_clock.is_ticking(clock):
            raise UserError(env._("Nothing is ticking this world, so it cannot move on to the next day."))
        target = workday.next_day_start(
            clock.now(), workday.timezone(tz, request.env.user.tz),
        )
        clock = game_clock.start_forward(cr, target)
        if clock is None:  # another page got there first
            raise UserError(env._("The world is already moving on to the next day."))
        pulse.send(cr, clock)
        # Wake the loop's cron thread now rather than at its next poll.
        cr.postcommit.add(request.env['ir.cron']._notifydb)
        return pulse.payload(clock)
