/**
 * The game page's entry point: the world clock, kept current by the pulse,
 * and the world under it, kept current by the notices that it changed.
 * See UI_DESIGN.md 5.4 and GAME_STATE.md 8.
 *
 * Its name is spelled in `controllers/main.py` (`ENTRY`) too, which finds the
 * built file through Vite's manifest by this path.  Move it and update both.
 */
import './style.css';
import { createMailView } from './mailView.js';
import { readClock } from './reading.js';
import { readWorld, syncWorld } from './sync.js';
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
    // The mail panel's own state (mailView.js describeMail); the mail itself
    // arrives with the world.
    mailUi: { folder: 'inbox', open: null, compose: null, error: null },
};

const game = document.getElementById('game');
const clockRoot = document.createElement('header');
const worldRoot = document.createElement('main');
const mailRoot = document.createElement('div');
game.replaceChildren(clockRoot, worldRoot, mailRoot);

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

const renderMail = createMailView(mailRoot, {
    folder(key) {
        state.mailUi.folder = key;
        render();
    },
    open: (emailId) => openEmail(emailId),
    close() {
        state.mailUi.open = null;
        render();
    },
    write() {
        state.mailUi.open = null;
        compose({ to: '', cc: '', subject: '', parentId: null });
    },
    reply() {
        const { open } = state.mailUi;
        compose({ to: open.reply.to, cc: '', subject: open.reply.subject, parentId: open.id });
    },
    send: (fields) => sendMail(fields),
    discard() {
        state.mailUi.compose = null;
        state.mailUi.error = null;
        render();
    },
});

function render() {
    renderClock(state);
    renderWorld(state);
    renderMail(state);
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

// Each form gets a key of its own, so the view knows when to fill it in afresh.
let forms = 0;

function compose(values) {
    forms += 1;
    state.mailUi.compose = { key: forms, ...values };
    state.mailUi.error = null;
    render();
}

/** Open an email -- a read -- and then, if it was unread, say it has been read. */
async function openEmail(emailId) {
    state.mailUi.error = null;
    try {
        state.mailUi.open = await readWorld(await send('GET', `/game/api/mail/${emailId}`));
    } catch (error) {
        state.mailUi.error = error.message;
    }
    render();
    if (state.world?.mail?.inbox.some((email) => email.id === emailId && email.unread)) {
        sync.act(() => send('POST', `/game/api/mail/${emailId}/read`, {}))
            .catch((error) => console.warn('odoo_sim: could not mark the email read', error));
    }
}

/** Send what the form holds; the form stays, with the reason, if it is refused. */
async function sendMail(fields) {
    const parentId = state.mailUi.compose?.parentId ?? null;
    state.pending.add('mail:send');
    state.mailUi.error = null;
    render();
    try {
        await sync.act(() => send('POST', '/game/api/mail/send', { ...fields, parent_id: parentId }));
        state.mailUi.compose = null;
    } catch (error) {
        state.mailUi.error = error.message;
    } finally {
        state.pending.delete('mail:send');
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
