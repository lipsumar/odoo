import assert from 'node:assert/strict';
import { test } from 'node:test';

import { describeTimeBar } from '../src/timeBar.js';

const UTC = {
    time: new Intl.DateTimeFormat('en-GB', { timeStyle: 'medium', timeZone: 'UTC' }),
    date: new Intl.DateTimeFormat('en-GB', { dateStyle: 'full', timeZone: 'UTC' }),
    until: new Intl.DateTimeFormat('en-GB', {
        weekday: 'long', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    }),
};

test('paused and stopped are said differently', () => {
    const reading = (extra) => ({ gameNow: new Date('2030-03-01T09:30:00Z'), rate: 1440, ...extra });

    const paused = describeTimeBar({ reading: reading({ paused: true, running: false }), link: 'live' }, UTC);
    assert.equal(paused.world, 'Paused');
    assert.equal(paused.state, 'paused');

    const stopped = describeTimeBar({ reading: reading({ paused: false, running: false }), link: 'live' }, UTC);
    assert.match(stopped.world, /^Stopped/);
    assert.equal(stopped.state, 'stopped');
});

test('before any reading there is still something to show', () => {
    const shown = describeTimeBar({ reading: null, link: 'connecting' }, UTC);
    assert.equal(shown.time, '--:--:--');
    assert.equal(shown.link.text, 'Connecting…');
    assert.ok(shown.pause.disabled && shown.forward.disabled, 'nothing to act on yet');
});

// -- the time bar's buttons ---------------------------------------------------

const at = (extra) => ({
    gameNow: new Date('2030-03-01T17:30:00Z'), rate: 720, paused: false, running: true, forwardTo: null, ...extra,
});

test('a running world can be paused, or forwarded to the next day', () => {
    const shown = describeTimeBar({ reading: at(), link: 'live' }, UTC);
    assert.deepEqual(shown.pause, { label: 'Pause', paused: true, disabled: false });
    assert.equal(shown.forward.disabled, false);
    assert.equal(shown.forwarding, null, 'no veil');
});

test('a paused world can be resumed, and still forwarded', () => {
    const shown = describeTimeBar({ reading: at({ paused: true, running: false }), link: 'live' }, UTC);
    assert.deepEqual(shown.pause, { label: 'Resume', paused: false, disabled: false });
    assert.equal(shown.forward.disabled, false);
});

test('while forwarding, the page says where to and nothing can be pressed', () => {
    const forwardTo = new Date('2030-03-02T09:00:00Z');
    const shown = describeTimeBar({ reading: at({ running: false, forwardTo }), link: 'live' }, UTC);
    assert.equal(shown.state, 'forwarding');
    assert.equal(shown.world, 'Forwarding to Saturday 09:00…');
    assert.equal(shown.forwarding, shown.world, 'the veil is up');
    assert.ok(shown.pause.disabled && shown.forward.disabled);
});

test('a stopped world, or a lost session, offers neither', () => {
    const stopped = describeTimeBar({ reading: at({ running: false }), link: 'live' }, UTC);
    assert.ok(stopped.pause.disabled && stopped.forward.disabled);
    const signedOut = describeTimeBar({ reading: at(), link: 'signed-out' }, UTC);
    assert.ok(signedOut.pause.disabled && signedOut.forward.disabled);
});

test('a press in flight holds both buttons', () => {
    const shown = describeTimeBar({ reading: at(), link: 'live', pending: new Set(['clock:forward']) }, UTC);
    assert.ok(shown.pause.disabled && shown.forward.disabled);
});
