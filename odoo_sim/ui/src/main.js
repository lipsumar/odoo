/**
 * The game page's entry point: the world clock, kept current by the pulse.
 * See UI_DESIGN.md 5.4.
 *
 * Its name is spelled in `controllers/main.py` (`ENTRY`) too, which finds the
 * built file through Vite's manifest by this path.  Move it and update both.
 */
import './style.css';
import { readClock } from './reading.js';
import { createView } from './view.js';
import { watchWorld } from './world.js';

// Rendered into the page by `GET /game` (views/index.xml), or by index.html
// under `npm run dev`.
const bootstrap = window.odooSim;

const state = {
    reading: bootstrap.clock ? readClock(bootstrap.clock) : null,
    link: 'connecting',
};
const render = createView(document.getElementById('game'));
render(state);

const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
watchWorld({
    // The bus closes a browser's socket straight away unless `version` names
    // the server's own bus client, which is why the server hands it over.
    socketUrl: `${scheme}//${location.host}/websocket?version=${encodeURIComponent(bootstrap.websocket_version)}`,
    channel: bootstrap.channel,
    type: bootstrap.type,
    fetchClock: () => fetch('/game/api/clock', { headers: { Accept: 'application/json' } }),
    onReading(reading) {
        state.reading = reading;
        render(state);
    },
    onLink(link) {
        state.link = link;
        render(state);
    },
    WebSocket,
});
