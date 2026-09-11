import assert from 'node:assert/strict';
import { afterEach, beforeEach, mock, test } from 'node:test';

import { describe } from '../src/view.js';
import { watchWorld } from '../src/world.js';

const CHANNEL = 'odoo_sim.world';
const TYPE = 'odoo_sim.pulse';
const CHANGED = 'odoo_sim.world_changed';

/** A clock payload, as `pulse.payload` builds it. */
function payload(gameNow, extra = {}) {
    return {
        game_now: gameNow,
        last_tick_real: '2026-09-10T13:26:04.000000',
        rate: 1440.0,
        paused: false,
        max_gap: 5.0,
        running: true,
        server_real_now: '2026-09-10T13:26:04.412000',
        ...extra,
    };
}

/** A bus frame: a JSON array of notifications. */
function frame(...messages) {
    return messages.map((message, index) => ({ id: index + 1, message }));
}

const pulse = (gameNow, extra) => ({ type: TYPE, payload: payload(gameNow, extra) });

/** A WebSocket the test plays the server's side of. */
class FakeSocket extends EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;

    constructor(url) {
        super();
        this.url = url;
        this.sent = [];
        this.readyState = FakeSocket.CONNECTING;
        this.closedByClient = false;
        sockets.push(this);
    }

    send(data) {
        this.sent.push(JSON.parse(data));
    }

    close() {
        this.closedByClient = true;
        this.serverClose(1005);
    }

    serverOpen() {
        this.readyState = FakeSocket.OPEN;
        this.dispatchEvent(new Event('open'));
    }

    serverSend(notifications) {
        const event = new Event('message');
        event.data = JSON.stringify(notifications);
        this.dispatchEvent(event);
    }

    serverClose(code = 1006, reason = '') {
        this.readyState = FakeSocket.CLOSED;
        const event = new Event('close');
        Object.assign(event, { code, reason });
        this.dispatchEvent(event);
    }
}

/** A `fetch` answer, reduced to what `watchWorld` reads. */
const answer = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });

/** Let pending promise callbacks run. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

const UTC = {
    time: new Intl.DateTimeFormat('en-GB', { timeStyle: 'medium', timeZone: 'UTC' }),
    date: new Intl.DateTimeFormat('en-GB', { dateStyle: 'full', timeZone: 'UTC' }),
};

let sockets;
let readings;
let changes;
let links;
let fetches;
let fetchClock;
let watcher;

beforeEach(() => {
    mock.timers.enable({ apis: ['setTimeout'] });
    // console.warn is expected where a fetch fails; keep the output clean
    mock.method(console, 'warn', () => {});
    sockets = [];
    readings = [];
    changes = 0;
    links = [];
    fetches = 0;
    fetchClock = async () => answer(200, payload('2030-03-01T09:00:00.000000'));
});

afterEach(() => {
    watcher?.stop();
    watcher = null;
    mock.timers.reset();
    mock.restoreAll();
});

function watch() {
    watcher = watchWorld({
        socketUrl: 'ws://odoo.test/websocket?version=19.0-2',
        channel: CHANNEL,
        type: TYPE,
        changedType: CHANGED,
        fetchClock: () => {
            fetches += 1;
            return fetchClock();
        },
        onReading: (reading) => readings.push(reading),
        onChanged: () => { changes += 1; },
        onLink: (link) => links.push(link),
        WebSocket: FakeSocket,
    });
    return sockets.at(-1);
}

const lastTime = () => readings.at(-1).gameNow.toISOString();

test('subscribes to the channel it was given, with last 0', () => {
    const socket = watch();
    socket.serverOpen();

    assert.equal(socket.url, 'ws://odoo.test/websocket?version=19.0-2');
    assert.deepEqual(socket.sent, [
        { event_name: 'subscribe', data: { channels: [CHANNEL], last: 0 } },
    ]);
    assert.deepEqual(links, ['live']);
});

test('fetches a reading on start, before the socket is open', async () => {
    watch();
    await settle();

    assert.equal(fetches, 1);
    assert.equal(lastTime(), '2030-03-01T09:00:00.000Z');
});

test('the page displays the game_now of a pulse', async () => {
    const socket = watch();
    await settle();
    socket.serverOpen();
    socket.serverSend(frame(pulse('2030-03-01T09:30:07.250000')));

    const shown = describe({ reading: readings.at(-1), link: 'live' }, UTC);
    assert.equal(shown.time, '09:30:07');
    // whether a comma follows the weekday depends on the ICU data in use
    assert.match(shown.date, /^Friday,? 1 March 2030$/);
    assert.equal(shown.world, 'Running at 1440×');
    assert.equal(shown.link, null, 'nothing to say about a live link');
});

test('every pulse in a frame is applied, in order, and nothing else is', async () => {
    const socket = watch();
    await settle();
    readings.length = 0;
    socket.serverOpen();
    socket.serverSend(frame(
        pulse('2030-03-01T09:30:00.000000'),
        { type: 'mail.record/insert', payload: { game_now: 'not a clock' } },
        pulse('2030-03-01T09:54:00.000000'),
    ));

    assert.deepEqual(readings.map((r) => r.gameNow.toISOString()), [
        '2030-03-01T09:30:00.000Z',
        '2030-03-01T09:54:00.000Z',
    ]);
});

test('a world-changed notice is passed on, and is not a clock reading', async () => {
    const socket = watch();
    await settle();
    socket.serverOpen();
    const before = changes;
    readings.length = 0;
    socket.serverSend(frame({ type: CHANGED, payload: {} }, { type: CHANGED, payload: {} }));

    assert.equal(changes - before, 2);
    assert.deepEqual(readings, []);
});

test('every connection resyncs the world, since notices sent while down are gone', async () => {
    const first = watch();
    await settle();
    first.serverOpen();
    assert.equal(changes, 1);

    first.serverClose(1006);
    await settle();
    mock.timers.tick(1000);
    sockets.at(-1).serverOpen();
    assert.equal(changes, 2);
});

test('a fetch that a pulse overtook is thrown away', async () => {
    let release;
    fetchClock = () => new Promise((resolve) => {
        release = () => resolve(answer(200, payload('2030-03-01T09:00:00.000000')));
    });
    const socket = watch();
    socket.serverOpen();
    socket.serverSend(frame(pulse('2030-03-01T09:30:00.000000')));
    release();
    await settle();

    assert.equal(readings.length, 1);
    assert.equal(lastTime(), '2030-03-01T09:30:00.000Z', 'not stepped back to the fetch');
});

test('when the socket closes, it falls back to the fetch and reconnects', async () => {
    const first = watch();
    await settle();
    first.serverOpen();
    first.serverSend(frame(pulse('2030-03-01T09:30:00.000000')));

    fetchClock = async () => answer(200, payload('2030-03-01T09:54:00.000000'));
    first.serverClose(1006);
    await settle();

    assert.equal(fetches, 2, 'one on start, one on close');
    assert.equal(lastTime(), '2030-03-01T09:54:00.000Z', 'resynced from the fetch');
    assert.deepEqual(links, ['live', 'reconnecting']);

    assert.equal(sockets.length, 1, 'not straight away');
    mock.timers.tick(1000);
    assert.equal(sockets.length, 2);
    const second = sockets[1];
    second.serverOpen();
    assert.deepEqual(second.sent[0].data, { channels: [CHANNEL], last: 0 }, 'a resync, not a replay');
    assert.equal(links.at(-1), 'live');
});

test('a closed socket and a failed fetch say the server is unreachable', async () => {
    const socket = watch();
    await settle();
    socket.serverOpen();

    fetchClock = async () => { throw new TypeError('Failed to fetch'); };
    socket.serverClose(1006);
    await settle();

    assert.deepEqual(links, ['live', 'reconnecting', 'unreachable']);
    assert.equal(describe({ reading: readings.at(-1), link: 'unreachable' }, UTC).link.tone, 'fault');
});

test('a fetch that fails while the socket is up changes nothing', async () => {
    let release;
    fetchClock = () => new Promise((resolve) => { release = () => resolve(answer(502)); });
    const socket = watch();
    socket.serverOpen();
    release();
    await settle();

    assert.deepEqual(links, ['live']);
});

test('reconnects back off, and a pulse resets them', async () => {
    const delays = [];
    watch();
    await settle();
    for (let i = 0; i < 6; i += 1) {
        const socket = sockets.at(-1);
        socket.serverClose(1006);
        await settle();
        // step forward until the next socket appears, counting the wait
        let waited = 0;
        while (sockets.at(-1) === socket) {
            mock.timers.tick(250);
            waited += 250;
        }
        delays.push(waited);
    }
    assert.deepEqual(delays, [1000, 2000, 4000, 8000, 15000, 15000]);

    const socket = sockets.at(-1);
    socket.serverOpen();
    socket.serverSend(frame(pulse('2030-03-01T09:30:00.000000')));
    socket.serverClose(1006);
    await settle();
    mock.timers.tick(1000);
    assert.notEqual(sockets.at(-1), socket, 'back to the first delay');
});

test('an expired session stops everything', async () => {
    const socket = watch();
    await settle();
    socket.serverOpen();
    socket.serverClose(4001);
    await settle();
    mock.timers.tick(60000);

    assert.equal(links.at(-1), 'signed-out');
    assert.equal(sockets.length, 1, 'no reconnect');
    assert.equal(fetches, 1, 'no fetch either');
});

test('a 403 from the fetch is an expired session too', async () => {
    fetchClock = async () => answer(403, { message: 'Session expired' });
    const socket = watch();
    await settle();

    assert.deepEqual(links, ['signed-out']);
    assert.ok(socket.closedByClient);
    assert.equal(describe({ reading: null, link: 'signed-out' }, UTC).link.action.href, '/web/login?redirect=/game');
});

test('a server whose bus is newer than the page asks for a reload', async () => {
    const socket = watch();
    await settle();
    socket.serverClose(1000, 'OUTDATED_VERSION');
    mock.timers.tick(60000);

    assert.deepEqual(links, ['outdated']);
    assert.equal(sockets.length, 1, 'retrying would only be refused again');
});

test('paused and stopped are said differently', () => {
    const reading = (extra) => ({ gameNow: new Date('2030-03-01T09:30:00Z'), rate: 1440, ...extra });

    const paused = describe({ reading: reading({ paused: true, running: false }), link: 'live' }, UTC);
    assert.equal(paused.world, 'Paused');
    assert.equal(paused.state, 'paused');

    const stopped = describe({ reading: reading({ paused: false, running: false }), link: 'live' }, UTC);
    assert.match(stopped.world, /^Stopped/);
    assert.equal(stopped.state, 'stopped');
});

test('before any reading there is still something to show', () => {
    const shown = describe({ reading: null, link: 'connecting' }, UTC);
    assert.equal(shown.time, '--:--:--');
    assert.equal(shown.link.text, 'Connecting…');
});
