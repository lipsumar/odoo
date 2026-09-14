/**
 * The time bar: what time it is in the world, and what the player can do with
 * time -- pause it, and forward the world to the next working morning
 * (DESIGN.md 3.4) -- plus the veil over the rest of the page while a forward
 * is under way.
 *
 * `describeTimeBar` is the whole of the decision and touches no DOM, so the
 * tests can check what a reading looks like without a browser; `createTimeBar`
 * only writes its answer into elements.
 */

/**
 * The browser's own locale and time zone, which is what the Odoo web client
 * shows datetimes in too -- so a record stamped at a game instant and this
 * clock showing that instant read the same.  A forward lands at nine in this
 * zone too: the page tells the server which one it is.
 */
export function localFormats() {
    return {
        time: new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' }),
        date: new Intl.DateTimeFormat(undefined, { dateStyle: 'full' }),
        // Where a forward is going: "Tuesday 09:00".
        until: new Intl.DateTimeFormat(undefined, { weekday: 'long', hour: '2-digit', minute: '2-digit' }),
    };
}

const LINKS = {
    live: null,
    connecting: { text: 'Connecting…', tone: 'wait' },
    reconnecting: { text: 'Reconnecting…', tone: 'wait' },
    unreachable: { text: 'Cannot reach the server. Retrying.', tone: 'fault' },
    'signed-out': {
        text: 'Your session has ended.',
        tone: 'fault',
        action: { label: 'Log in again', href: '/web/login?redirect=/game' },
    },
    outdated: {
        text: 'The server has been updated.',
        tone: 'fault',
        action: { label: 'Reload', href: '/game' },
    },
};

/**
 * Describe `{ reading, link, pending }` as what the bar shows: the clock, the
 * world's state, the pause and forward buttons, and -- when the connection is
 * anything but live -- a line saying what is wrong.
 *
 * `reading` may be null, before the first one has arrived.  `pending` holds
 * the keys of actions in flight; `clock:pause` and `clock:forward` are the
 * bar's own.  `forwarding` is the veil's text, or null for no veil.
 */
export function describeTimeBar({ reading, link, pending = new Set() }, formats) {
    const shown = {
        time: '--:--:--',
        date: '',
        datetime: '',
        world: '',
        state: 'unknown',
        link: LINKS[link],
        pause: { label: 'Pause', paused: true, disabled: true },
        forward: { label: 'Forward to next day', disabled: true },
        forwarding: null,
    };
    if (!reading) {
        return shown;
    }
    shown.time = formats.time.format(reading.gameNow);
    shown.date = formats.date.format(reading.gameNow);
    shown.datetime = reading.gameNow.toISOString();
    if (reading.forwardTo) {
        shown.world = `Forwarding to ${formats.until.format(reading.forwardTo)}…`;
        shown.state = 'forwarding';
        shown.forwarding = shown.world;
    } else if (reading.paused) {
        shown.world = 'Paused';
        shown.state = 'paused';
    } else if (!reading.running) {
        // Only a fetch can say this: pulses come from the loop, so the loop
        // being down is exactly when none arrive.
        shown.world = 'Stopped: nothing is ticking this world';
        shown.state = 'stopped';
    } else {
        shown.world = `Running at ${reading.rate}×`;
        shown.state = 'running';
    }

    // Time can be paused, resumed or sent on only while a loop is there to
    // do it and no forward is already under way.  A page that has lost its
    // session, or its server, could only be refused.
    const busy = pending.has('clock:pause') || pending.has('clock:forward');
    const able = !busy && (shown.state === 'running' || shown.state === 'paused')
        && shown.link?.tone !== 'fault';
    shown.pause = { label: reading.paused ? 'Resume' : 'Pause', paused: !reading.paused, disabled: !able };
    shown.forward = { label: 'Forward to next day', disabled: !able };
    return shown;
}

/**
 * Build the bar inside `root` and the veil inside `veil`, and return
 * `render(state)` to update both.
 *
 * `actions.pause(paused)` and `actions.forward()` are called when the player
 * presses a button.
 */
export function createTimeBar(root, veil, actions, formats = localFormats()) {
    const time = document.createElement('time');
    time.className = 'clock-time';
    const date = document.createElement('span');
    date.className = 'clock-date';
    const world = document.createElement('span');
    world.className = 'clock-world';
    const readout = document.createElement('div');
    readout.className = 'timebar-clock';
    readout.append(time, date, world);

    // What pressing the pause button asks for: the opposite of what is.
    let paused = true;
    const pause = document.createElement('button');
    pause.type = 'button';
    pause.className = 'secondary';
    pause.addEventListener('click', () => actions.pause(paused));
    const forward = document.createElement('button');
    forward.type = 'button';
    forward.addEventListener('click', () => actions.forward());
    const controls = document.createElement('div');
    controls.className = 'timebar-actions';
    controls.append(pause, forward);

    const link = document.createElement('p');
    link.className = 'clock-link';
    link.setAttribute('role', 'status');

    const bar = document.createElement('div');
    bar.className = 'timebar';
    bar.append(readout, controls, link);
    root.replaceChildren(bar);

    const veilText = document.createElement('p');
    veilText.setAttribute('role', 'status');
    veil.className = 'forwarding-veil';
    veil.replaceChildren(veilText);

    return function render(state) {
        const shown = describeTimeBar(state, formats);
        time.textContent = shown.time;
        time.dateTime = shown.datetime;
        date.textContent = shown.date;
        world.textContent = shown.world;
        bar.dataset.state = shown.state;

        pause.textContent = shown.pause.label;
        pause.disabled = shown.pause.disabled;
        paused = shown.pause.paused;
        forward.textContent = shown.forward.label;
        forward.disabled = shown.forward.disabled;

        veil.hidden = !shown.forwarding;
        veilText.textContent = shown.forwarding ?? '';

        link.hidden = !shown.link;
        if (shown.link) {
            link.dataset.tone = shown.link.tone;
            const parts = [shown.link.text];
            if (shown.link.action) {
                const anchor = document.createElement('a');
                anchor.href = shown.link.action.href;
                anchor.textContent = shown.link.action.label;
                parts.push(' ', anchor);
            }
            link.replaceChildren(...parts);
        }
    };
}
