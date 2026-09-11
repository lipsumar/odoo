/**
 * What the page shows, and the DOM that shows it.
 *
 * `describe` is the whole of the decision and touches no DOM, so the tests
 * can check what a reading looks like without a browser; `createView` only
 * writes its answer into elements.
 */

/**
 * The browser's own locale and time zone, which is what the Odoo web client
 * shows datetimes in too -- so a record stamped at a game instant and this
 * clock showing that instant read the same.
 */
export function localFormats() {
    return {
        time: new Intl.DateTimeFormat(undefined, { timeStyle: 'medium' }),
        date: new Intl.DateTimeFormat(undefined, { dateStyle: 'full' }),
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
 * Describe `{ reading, link }` as text: the clock, the world's state, and --
 * when the connection is anything but live -- a line saying what is wrong.
 *
 * `reading` may be null, before the first one has arrived.
 */
export function describe({ reading, link }, formats) {
    const shown = {
        time: '--:--:--',
        date: '',
        datetime: '',
        world: '',
        state: 'unknown',
        link: LINKS[link],
    };
    if (!reading) {
        return shown;
    }
    shown.time = formats.time.format(reading.gameNow);
    shown.date = formats.date.format(reading.gameNow);
    shown.datetime = reading.gameNow.toISOString();
    if (reading.paused) {
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
    return shown;
}

/**
 * Build the clock inside `root`, and return `render(state)` to update it.
 */
export function createView(root, formats = localFormats()) {
    const time = document.createElement('time');
    time.className = 'clock-time';
    const date = document.createElement('p');
    date.className = 'clock-date';
    const world = document.createElement('p');
    world.className = 'clock-world';
    const link = document.createElement('p');
    link.className = 'clock-link';
    link.setAttribute('role', 'status');

    const clock = document.createElement('section');
    clock.className = 'clock';
    clock.append(time, date, world, link);
    root.replaceChildren(clock);

    return function render(state) {
        const shown = describe(state, formats);
        time.textContent = shown.time;
        time.dateTime = shown.datetime;
        date.textContent = shown.date;
        world.textContent = shown.world;
        clock.dataset.state = shown.state;

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
