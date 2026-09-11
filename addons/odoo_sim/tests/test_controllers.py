# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the game's HTTP surface (see odoo_sim/UI_DESIGN.md sections 5.4, 5.5).

These run against an overridden clock rather than a real world, so that they
pass on an ordinary test database: ``game_clock.override`` pins a reading that
never expires, and the cleanup below drops it again. Without the cleanup the
pin would leak into every later test in the process, which would be a very
confusing failure to chase.
"""
import io
import json
import re
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import game_clock
from odoo.addons.bus.websocket import WebsocketConnectionHandler
from odoo.addons.odoo_sim import pulse
from odoo.addons.odoo_sim.controllers import main
from odoo.game_clock import GameClock
from odoo.tests.common import HttpCase, TransactionCase, tagged

GAME_NOW = datetime(2030, 3, 1, 9, 30, 0, 123456)
RATE = 1440.0

#: Pulled back out of the page's bootstrap blob. The template writes
#: ``var odooSim = {...};`` and nothing else on that line.
BOOTSTRAP = re.compile(r'var odooSim = (\{.*\});')


class TestBuiltAssets(TransactionCase):
    """Reading Vite's manifest, including every way it can be absent."""

    def _manifest(self, payload):
        """ Patch the manifest read to return ``payload`` as its file body. """
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return patch.object(main, 'file_open', return_value=io.StringIO(body))

    def test_entry_becomes_script_and_style_urls(self):
        with self._manifest({
            'src/main.js': {
                'file': 'assets/main-DEADBEEF.js',
                'css': ['assets/main-CAFEBABE.css'],
                'isEntry': True,
            },
        }):
            scripts, styles = main._built_assets()
        self.assertEqual(scripts, ['/odoo_sim/static/dist/assets/main-DEADBEEF.js'])
        self.assertEqual(styles, ['/odoo_sim/static/dist/assets/main-CAFEBABE.css'])

    def test_an_entry_without_css_is_fine(self):
        """ Vite omits `css` entirely when the bundle imports no stylesheet. """
        with self._manifest({'src/main.js': {'file': 'assets/main-1.js'}}):
            scripts, styles = main._built_assets()
        self.assertEqual(scripts, ['/odoo_sim/static/dist/assets/main-1.js'])
        self.assertEqual(styles, [])

    def test_an_unbuilt_tree_is_not_an_error(self):
        """ The backend landed before the frontend; the page says so itself. """
        with patch.object(main, 'file_open', side_effect=FileNotFoundError):
            self.assertEqual(main._built_assets(), ([], []))

    def test_a_manifest_without_our_entry_is_survived(self):
        """ Renaming the entry in vite.config without telling Python.

        Warned about, unlike the unbuilt case: the page is about to claim the
        frontend was never built, and that would be a lie worth a log line.
        """
        with self._manifest({'src/somethingelse.js': {'file': 'assets/x.js'}}):
            with self.assertLogs(main._logger.name, 'WARNING'):
                self.assertEqual(main._built_assets(), ([], []))

    def test_a_corrupt_manifest_is_survived(self):
        """ A half-written manifest from a build that was interrupted. """
        with self._manifest('{"src/main.js": {"file"'):
            with self.assertLogs(main._logger.name, 'WARNING'):
                self.assertEqual(main._built_assets(), ([], []))


@tagged('-at_install', 'post_install')
class TestGameUi(HttpCase):
    """The page and the clock endpoint, over real HTTP."""

    def setUp(self):
        super().setUp()
        self.dbname = self.env.cr.dbname
        self.addCleanup(game_clock.invalidate, self.dbname)
        self.authenticate('admin', 'admin')

    def _world(self, *, paused=False, silent_for=timedelta(0), max_gap=timedelta(seconds=5)):
        """ Install a clock and return it. ``silent_for`` ages the last tick. """
        clock = GameClock(GAME_NOW, datetime.now() - silent_for, RATE, paused, max_gap)
        game_clock.override(self.dbname, clock)
        return clock

    def _no_frontend(self):
        return patch.object(main, '_built_assets', return_value=([], []))

    # -- the clock endpoint ------------------------------------------------

    def test_the_endpoint_returns_exactly_the_pulse_payload(self):
        """ One shape, so the browser needs one parser.

        The client sets its basis from this fetch and then refreshes the very
        same basis from every pulse (UI_DESIGN.md 5.5). If the two payloads were
        allowed to diverge, the browser would end up with two parsers for one
        concept, and the day they disagreed they would disagree about what time
        it is in the world. Both are built by ``pulse.payload``; this pins it.
        """
        clock = self._world()
        fetched = self.url_open('/game/api/clock').json()

        self.assertEqual(set(fetched), set(pulse.payload(clock)))
        # every field but the two that move on their own must match exactly
        expected = pulse.payload(clock)
        for field in ('game_now', 'last_tick_real', 'rate', 'paused', 'max_gap'):
            with self.subTest(field=field):
                self.assertEqual(fetched[field], expected[field])

    def test_the_endpoint_agrees_with_the_clock_it_serves(self):
        """ Reproduce the server's arithmetic from the response alone.

        This is the same round trip ``test_pulse.py`` performs, and it is the
        executable half of the spec the JavaScript has to conform to: when the
        browser's ``gameNow()`` disagrees with this, the browser is wrong.
        """
        self._world()
        fetched = self.url_open('/game/api/clock').json()

        basis = GameClock(
            datetime.fromisoformat(fetched['game_now']),
            datetime.fromisoformat(fetched['last_tick_real']),
            fetched['rate'], fetched['paused'],
            timedelta(seconds=fetched['max_gap']),
        )
        self.assertEqual(basis.game_now, GAME_NOW)
        two_seconds_on = basis.last_tick_real + timedelta(seconds=2)
        self.assertEqual(
            basis.game_at(two_seconds_on), GAME_NOW + timedelta(seconds=2 * RATE),
        )

    def test_a_stopped_world_still_answers(self):
        """ The endpoint must not refuse when the loop is dead.

        It is a read, and it is the specific read that *tells* a client the
        world stopped. Guarding it with ``is_running`` -- which is right on a
        write path -- would withhold the one fact the client needs in order to
        lock itself, and the page would have nothing to show but a failed
        request.
        """
        self._world(silent_for=timedelta(seconds=30))
        response = self.url_open('/game/api/clock')

        self.assertEqual(response.status_code, 200)
        fetched = response.json()
        self.assertFalse(fetched['running'], "nothing has ticked it for six max_gaps")
        self.assertFalse(fetched['paused'], "down is not the same state as paused")

    def test_a_paused_world_answers_paused(self):
        """ The two not-running states stay distinguishable on the wire.

        Which is what lets the UI show a deliberate hold differently from a
        fault (UI_DESIGN.md 5.6). One flag would have collapsed them.
        """
        self._world(paused=True)
        fetched = self.url_open('/game/api/clock').json()

        self.assertFalse(fetched['running'])
        self.assertTrue(fetched['paused'])

    def test_a_database_that_is_not_a_world_is_refused(self):
        """ Ordinary Odoo has no clock to hand out, and says so with a status. """
        game_clock.override(self.dbname, None)
        response = self.url_open('/game/api/clock')

        self.assertEqual(response.status_code, 404)
        self.assertIn('sim_init', response.text, "say how to fix it")

    # -- the page ----------------------------------------------------------

    def test_the_page_carries_a_basis_and_the_channel_names(self):
        """ Both halves of the bootstrap blob, and why each is there.

        The basis means the page can show a time before its first request. The
        channel and type mean the JavaScript never spells them a second time --
        two copies of a channel name drift, and the symptom is not an error but
        a UI that silently never updates.
        """
        clock = self._world()
        with self._no_frontend():
            page = self.url_open('/game').text

        found = BOOTSTRAP.search(page)
        self.assertIsNotNone(found, "the page must carry a bootstrap blob")
        bootstrap = json.loads(found.group(1))

        self.assertEqual(bootstrap['channel'], pulse.CHANNEL)
        self.assertEqual(bootstrap['type'], pulse.TYPE)
        # the bus shuts a browser's socket unless it quotes this back
        self.assertEqual(bootstrap['websocket_version'], WebsocketConnectionHandler._VERSION)
        self.assertEqual(set(bootstrap['clock']), set(pulse.payload(clock)))
        self.assertEqual(bootstrap['clock']['game_now'], GAME_NOW.isoformat())

    def test_the_page_renders_before_the_frontend_exists(self):
        """ An unbuilt tree is a state to explain, not a 500 to debug. """
        self._world()
        with self._no_frontend():
            response = self.url_open('/game')

        self.assertEqual(response.status_code, 200)
        self.assertIn('not built', response.text)
        self.assertIn('npm run build', response.text, "say how to fix it")
        # nothing is *linked* -- the path appears in the prose above, which is
        # why this looks for the tags rather than for the directory name
        self.assertNotIn('<script type="module"', response.text)
        self.assertNotIn('src="/odoo_sim/static/dist/', response.text)

    def test_the_page_links_the_built_bundle(self):
        """ Hashed filenames come from the manifest, never from a constant. """
        self._world()
        built = ([main.DIST_URL + 'assets/main-DEADBEEF.js'],
                 [main.DIST_URL + 'assets/main-CAFEBABE.css'])
        with patch.object(main, '_built_assets', return_value=built):
            page = self.url_open('/game').text

        self.assertIn('src="/odoo_sim/static/dist/assets/main-DEADBEEF.js"', page)
        self.assertIn('href="/odoo_sim/static/dist/assets/main-CAFEBABE.css"', page)
        self.assertIn('type="module"', page, "vite emits ES modules")
        self.assertNotIn('not built', page)

    def test_the_page_is_refused_when_there_is_no_world(self):
        game_clock.override(self.dbname, None)
        with self._no_frontend():
            self.assertEqual(self.url_open('/game').status_code, 404)

    def test_the_page_does_not_pull_in_the_web_client(self):
        """ A standalone document, not a client action (UI_DESIGN.md 2.2, 3.A).

        The whole point of serving our own page is that the backend bundle does
        not come with it. A stray t-call-assets would undo that silently -- the
        page would still work, just with several megabytes of OWL in front of a
        canvas.
        """
        self._world()
        with self._no_frontend():
            page = self.url_open('/game').text

        self.assertNotIn('/web/assets/', page)
        self.assertNotIn('web.assets_backend', page)
        self.assertNotIn('odoo.define', page)
