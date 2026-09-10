# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the odoo-sim world pulse (see odoo_sim/DESIGN.md section 3.3)."""

import json
from datetime import datetime, timedelta

from odoo import game_clock
from odoo.addons.odoo_sim import pulse
from odoo.game_clock import GameClock
from odoo.tests.common import TransactionCase

ANCHOR_REAL = datetime(2026, 9, 9, 12, 0, 0)
GAME_START = ANCHOR_REAL + timedelta(days=100)


class TestWorldPulse(TransactionCase):
    """What the loop tells clients, and when it stays quiet."""

    def _a_clock(self, *, paused=False, last_tick=ANCHOR_REAL, max_gap=timedelta(seconds=5)):
        return GameClock(GAME_START, last_tick, 1440, paused, max_gap)

    def _sent(self):
        """ Return the pulses queued on this transaction, oldest first.

        The bus writes its rows at precommit, so they are read out of the
        pending values rather than out of ``bus_bus``.
        """
        return [
            json.loads(vals['message'])['payload']
            for vals in self.env.cr.precommit.data.get('bus.bus.values', ())
        ]

    def test_pulse_is_sent(self):
        clock = self._a_clock()
        self.assertTrue(pulse.send(self.env.cr, clock))
        sent = self._sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['game_now'], GAME_START.isoformat())
        self.assertEqual(sent[0]['rate'], 1440)
        self.assertEqual(sent[0]['max_gap'], 5.0)

    def test_pulse_is_still_sent_while_paused(self):
        """ The property the UI's lock rests on: pause is not silence.

        A client cannot observe ticks, so silence is the only signal that means
        "the world died". If a paused world stopped pulsing, a deliberate pause
        would be indistinguishable from a crash and the UI would lock on it.

        This is exactly what a plausible "only send when something changed"
        optimisation would delete, which is why it is a test and not only a
        comment.
        """
        paused = self._a_clock(paused=True)
        self.assertTrue(pulse.send(self.env.cr, paused))
        self.assertTrue(pulse.send(self.env.cr, paused))

        sent = self._sent()
        self.assertEqual(len(sent), 2, "a paused world must keep pulsing")
        for payload in sent:
            self.assertTrue(payload['paused'])
            self.assertFalse(payload['running'], "paused is not running")
            self.assertEqual(payload['game_now'], GAME_START.isoformat())

    def test_running_is_decided_server_side(self):
        """ The client must not re-derive the predicate the write path uses. """
        live = pulse.payload(self._a_clock(), real=ANCHOR_REAL + timedelta(seconds=1))
        self.assertTrue(live['running'])

        # nothing has ticked for longer than max_gap: the world is down, even
        # though nobody paused it and the database is plainly up
        dead = pulse.payload(self._a_clock(), real=ANCHOR_REAL + timedelta(seconds=30))
        self.assertFalse(dead['running'])
        self.assertFalse(dead['paused'], "down is not the same state as paused")

    def test_payload_carries_a_basis_a_client_can_interpolate_from(self):
        """ Every field the browser needs to reproduce game_at() locally. """
        sent = pulse.payload(self._a_clock())
        self.assertEqual(
            set(sent),
            {'game_now', 'last_tick_real', 'rate', 'paused', 'max_gap',
             'running', 'server_real_now'},
        )
        # replaying the server's own arithmetic from the payload alone
        basis = GameClock(
            datetime.fromisoformat(sent['game_now']),
            datetime.fromisoformat(sent['last_tick_real']),
            sent['rate'], sent['paused'], timedelta(seconds=sent['max_gap']),
        )
        instant = ANCHOR_REAL + timedelta(seconds=2)
        self.assertEqual(basis.game_at(instant), GAME_START + timedelta(seconds=2 * 1440))

    def test_datetimes_are_naive_utc_iso(self):
        """ The wire format is pinned: ISO, microseconds, no offset.

        Both ways of "improving" this break a consumer, for opposite reasons --
        see pulse.payload(). The failure mode if it changes is quiet: a client
        that parses these as local time still ticks at the right rate and
        merely shows a time wrong by its UTC offset times K.
        """
        sent = pulse.payload(self._a_clock())
        for key in ('game_now', 'last_tick_real', 'server_real_now'):
            with self.subTest(field=key):
                value = sent[key]
                self.assertNotIn('+', value, "no UTC offset")
                self.assertFalse(value.endswith('Z'), "no Z suffix")
                self.assertIn('T', value, "ISO separator, not Odoo's SQL format")
                # round-trips back to the naive UTC value it came from
                self.assertIsNone(datetime.fromisoformat(value).tzinfo)

        # Sub-second precision survives, which Odoo's SQL datetime format would
        # truncate: one lost second of last_tick_real is 24 game minutes of
        # error in the basis at K=1440. (isoformat() omits the fractional part
        # when it is zero, so this needs a clock that actually has one.)
        precise = self._a_clock(last_tick=ANCHOR_REAL + timedelta(microseconds=123456))
        self.assertEqual(
            datetime.fromisoformat(pulse.payload(precise)['last_tick_real']).microsecond,
            123456,
        )

    def test_no_pulse_without_a_clock(self):
        """ Not a game world, nothing to say. """
        self.assertFalse(pulse.send(self.env.cr, None))
        self.assertEqual(self._sent(), [])
