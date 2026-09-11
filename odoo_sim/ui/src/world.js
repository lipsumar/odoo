/**
 * Keep the page's reading of the world current.
 *
 * While the socket is up, every `odoo_sim.pulse` on the bus is a fresh
 * reading.  While it is not -- before it first opens, and after it drops --
 * `GET /game/api/clock` is.  See UI_DESIGN.md 5.4.
 *
 * Everything the browser provides is passed in, so that the tests can drive a
 * socket and a fetch by hand.
 */
import { readClock } from './reading.js';

/** The bus's close code for a session that has gone (`bus/websocket.py`). */
const SESSION_EXPIRED = 4001;

/**
 * The reason the bus closes a socket with when the client's `version` is not
 * the server's.  It closes *cleanly* on purpose, so that a stale client stops
 * rather than retries; this one stops too, and asks for a reload.
 */
const OUTDATED_VERSION = 'OUTDATED_VERSION';

/** Reconnect delays: doubling from the first, up to the second. */
const RETRY_FIRST_MS = 1000;
const RETRY_MAX_MS = 15000;

/**
 * Start watching the world.  Returns `{ stop }`.
 *
 * `onReading(reading)` is called with each new `readClock` reading.
 *
 * `onLink(state)` is called as the connection changes, with one of:
 *
 * - `live`: the socket is open and subscribed;
 * - `reconnecting`: it dropped, and another is scheduled;
 * - `unreachable`: it is down *and* the fetch failed, so the reading on screen
 *   is as old as the last one that got through;
 * - `signed-out`: the session is gone; nothing more is attempted;
 * - `outdated`: the server's bus is newer than this page; nothing more is
 *   attempted.
 *
 * The last two are terminal.  Retrying either could only fail the same way
 * again, and would do it every fifteen seconds for as long as the tab is open.
 */
export function watchWorld({ socketUrl, channel, type, fetchClock, onReading, onLink, WebSocket }) {
    let socket = null;
    let retries = 0;
    let timer = null;
    let halted = false;
    // Counts pulses applied, so that a fetch can tell whether one overtook it.
    let pulses = 0;

    function link(state) {
        if (!halted) {
            onLink(state);
        }
    }

    function halt(state) {
        if (halted) {
            return;
        }
        halted = true;
        clearTimeout(timer);
        socket?.close();
        if (state) {
            onLink(state);
        }
    }

    function open() {
        timer = null;
        const ws = socket = new WebSocket(socketUrl);
        ws.addEventListener('open', () => {
            // `last` is read unconditionally by `ir.websocket._subscribe`, so
            // it is not optional.  It is always 0: this client resyncs from
            // the fetch rather than replaying the bus's backlog, which is
            // measured in game time and so is gone within a minute of real
            // time at the rates a world runs at (UI_DESIGN.md 7).
            ws.send(JSON.stringify({
                event_name: 'subscribe',
                data: { channels: [channel], last: 0 },
            }));
            link('live');
        });
        ws.addEventListener('message', (event) => receive(event.data));
        // Not `error`: a browser always follows `error` with `close`, so
        // handling both would reconnect twice.
        ws.addEventListener('close', (event) => closed(ws, event));
    }

    function receive(data) {
        // A frame is a JSON array of notifications, for every channel this
        // socket is subscribed to -- the bus adds a few of its own.
        for (const { message } of JSON.parse(data)) {
            if (message.type === type) {
                onReading(readClock(message.payload));
                pulses += 1;
                // Only a pulse proves the whole path works; an `open` alone
                // does not, since the server may close straight after it.
                retries = 0;
            }
        }
    }

    function closed(ws, { code, reason }) {
        if (halted || ws !== socket) {
            return;
        }
        socket = null;
        if (code === SESSION_EXPIRED) {
            return halt('signed-out');
        }
        if (reason === OUTDATED_VERSION) {
            return halt('outdated');
        }
        link('reconnecting');
        refetch();
        timer = setTimeout(open, Math.min(RETRY_FIRST_MS * 2 ** retries, RETRY_MAX_MS));
        retries += 1;
    }

    async function refetch() {
        const seen = pulses;
        let reading;
        try {
            const response = await fetchClock();
            if (response.status === 403) {
                // What an `auth='user'` route answers once the session has gone.
                return halt('signed-out');
            }
            if (!response.ok) {
                throw new Error(`GET /game/api/clock answered ${response.status}`);
            }
            reading = readClock(await response.json());
        } catch (error) {
            console.warn('odoo_sim: could not fetch the clock', error);
            // Only worth saying while the socket is down: when it is up, the
            // pulse is already keeping the page current.
            if (!socket || socket.readyState !== WebSocket.OPEN) {
                link('unreachable');
            }
            return;
        }
        // A pulse that landed while this was in flight is the newer reading;
        // applying the fetch now would step the clock backwards.
        if (!halted && pulses === seen) {
            onReading(reading);
        }
    }

    refetch();
    open();
    return { stop: () => halt(null) };
}
