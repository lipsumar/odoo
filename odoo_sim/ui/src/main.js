/**
 * The game page's entry point: the world clock, kept current by the pulse,
 * and the world under it, kept current by the notices that it changed.
 * See UI_DESIGN.md 5.4 and GAME_STATE.md 8.
 *
 * Its name is spelled in `controllers/main.py` (`ENTRY`) too, which finds the
 * built file through Vite's manifest by this path.  Move it and update both.
 */
import './style.css';
import { readClock } from './reading.js';
import { syncWorld } from './sync.js';
import { createView } from './view.js';
import { createWorldView } from './worldView.js';
import { watchWorld } from './world.js';

// Rendered into the page by `GET /game` (views/index.xml), or by index.html
// under `npm run dev`.
const bootstrap = window.odooSim;

const state = {
    reading: bootstrap.clock ? readClock(bootstrap.clock) : null,
    link: 'connecting',
    world: bootstrap.world ?? null,
    pending: new Set(),
    error: null,
};

const game = document.getElementById('game');
const clockRoot = document.createElement('header');
const worldRoot = document.createElement('main');
game.replaceChildren(clockRoot, worldRoot);

const renderClock = createView(clockRoot);
const renderWorld = createWorldView(worldRoot, {
    start: (stationId, { qty, productionId }) => perform(
        `station:${stationId}`,
        () => send('POST', `/game/api/workstations/${stationId}/start`, { qty, production_id: productionId }),
    ),
    accept: (shipmentId) => perform(
        `shipment:${shipmentId}`,
        () => send('POST', `/game/api/shipments/${shipmentId}/accept`, {}),
    ),
});

function render() {
    renderClock(state);
    renderWorld(state);
}

function send(method, url, body) {
    return fetch(url, {
        method,
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
}

const sync = syncWorld({
    load: () => send('GET', '/game/api/world'),
    onWorld(world) {
        state.world = world;
        render();
    },
});

/** Take an action; its button waits, and a refusal is said out loud. */
async function perform(key, request) {
    state.pending.add(key);
    state.error = null;
    render();
    try {
        await sync.act(request);
    } catch (error) {
        state.error = error.message;
    } finally {
        state.pending.delete(key);
        render();
    }
}

render();

const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
watchWorld({
    // The bus closes a browser's socket straight away unless `version` names
    // the server's own bus client, which is why the server hands it over.
    socketUrl: `${scheme}//${location.host}/websocket?version=${encodeURIComponent(bootstrap.websocket_version)}`,
    channel: bootstrap.channel,
    type: bootstrap.type,
    changedType: bootstrap.changed_type,
    fetchClock: () => fetch('/game/api/clock', { headers: { Accept: 'application/json' } }),
    onReading(reading) {
        state.reading = reading;
        render();
    },
    onChanged: () => sync.refresh(),
    onLink(link) {
        state.link = link;
        render();
    },
    WebSocket,
});
