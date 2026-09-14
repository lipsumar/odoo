/**
 * The game page's entry point: the world clock, kept current by the pulse,
 * and the world under it, kept current by the notices that it changed.
 * See UI_DESIGN.md 5.4 and GAME_STATE.md 10.
 *
 * Its name is spelled in `controllers/main.py` (`ENTRY`) too, which finds the
 * built file through Vite's manifest by this path.  Move it and update both.
 */
import './style.css';
import { createMailView } from './mailView.js';
import { readClock } from './reading.js';
import { readJson, syncWorld } from './sync.js';
import { createTimeBar } from './timeBar.js';
import { createWorldView } from './worldView.js';
import { watchWorld } from './world.js';

// Rendered into the page by `GET /game` (views/index.xml), or by index.html
// under `npm run dev`.
const bootstrap = window.odooSim;

// The page shows time in the browser's zone, so the morning a forward lands on,
// and an employee's nine to five, are as this page shows time.
const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;

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
clockRoot.className = 'timebar-root';
const worldRoot = document.createElement('main');
const mailRoot = document.createElement('div');
const veilRoot = document.createElement('div');
game.replaceChildren(clockRoot, worldRoot, mailRoot, veilRoot);

const renderClock = createTimeBar(clockRoot, veilRoot, {
    pause: (paused) => moveTime('clock:pause', '/game/api/clock/pause', { paused }),
    forward: () => moveTime('clock:forward', '/game/api/clock/forward', { tz: timeZone }),
});
const renderWorld = createWorldView(worldRoot, {
    start: (stationId, { qty, productionId }) => perform(
        `station:${stationId}`,
        () => send('POST', `/game/api/workstations/${stationId}/start`, { qty, production_id: productionId }),
    ),
    accept: (shipmentId) => perform(
        `shipment:${shipmentId}`,
        () => send('POST', `/game/api/shipments/${shipmentId}/accept`, {}),
    ),
    newPackage: () => perform('package:new', () => send('POST', '/game/api/packages', {})),
    pack: (packageId, { productId, qty }) => perform(
        `package:${packageId}`,
        () => send('POST', `/game/api/packages/${packageId}/pack`, { product_id: productId, qty }),
    ),
    unpack: (packageId) => perform(
        `package:${packageId}`,
        () => send('POST', `/game/api/packages/${packageId}/unpack`, {}),
    ),
    post: (packageId, address) => perform(
        `package:${packageId}`,
        () => send('POST', `/game/api/packages/${packageId}/send`, { address }),
    ),
    hire: (jobId) => perform(
        `job:${jobId}`,
        () => send('POST', `/game/api/jobs/${jobId}/hire`, { tz: timeZone }),
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
        compose({ to: '', subject: '', parentId: null });
    },
    reply() {
        const { open } = state.mailUi;
        compose({ to: open.reply.to, subject: open.reply.subject, parentId: open.id });
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
    // Nothing on the page can be used while the world forwards: the server
    // refuses it anyway, and a half-typed form would be answered by a
    // different day.  `inert` takes the focus away too, not just the mouse.
    const forwarding = Boolean(state.reading?.forwardTo);
    worldRoot.inert = forwarding;
    mailRoot.inert = forwarding;
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

const showError = (message) => { state.error = message; };
const showMailError = (message) => { state.mailUi.error = message; };

/** Do `work` as the action `key`: its button waits, and a refusal goes to `fail`, to be said out loud. */
async function track(key, work, fail) {
    state.pending.add(key);
    fail(null);
    render();
    try {
        await work();
    } catch (error) {
        fail(error.message);
    } finally {
        state.pending.delete(key);
        render();
    }
}

/** Take an action in the world, and show the world it answers with. */
function perform(key, request) {
    return track(key, () => sync.act(request), showError);
}

/**
 * Pause, resume or forward.  The answer is a clock reading, in the pulse's own
 * shape, so the bar changes at once; the pulse the server sends with it tells
 * every other page.
 */
function moveTime(key, url, body) {
    return track(key, async () => {
        state.reading = readClock(await readJson(await send('POST', url, body)));
    }, showError);
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
        state.mailUi.open = await readJson(await send('GET', `/game/api/mail/${emailId}`));
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
function sendMail(fields) {
    const parentId = state.mailUi.compose?.parentId ?? null;
    return track('mail:send', async () => {
        await sync.act(() => send('POST', '/game/api/mail/send', { ...fields, parent_id: parentId }));
        state.mailUi.compose = null;
    }, showMailError);
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
