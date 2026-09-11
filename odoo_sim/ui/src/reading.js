/**
 * Clock readings, as the server hands them out.
 *
 * One shape arrives by two routes: the payload of every `odoo_sim.pulse` on
 * the bus, and the body of `GET /game/api/clock`.  Both are built by
 * `pulse.payload` on the server (a test there pins it), so both come through
 * `readClock` here.  See UI_DESIGN.md 5.2.
 */

// A naive ISO date-time, as Python's `isoformat()` writes one: no offset, and
// a fraction of up to six digits that is left out entirely when the
// microseconds happen to be zero.
const NAIVE_ISO = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$/;

/**
 * Parse one of the payload's datetimes into a `Date`.
 *
 * They are **UTC with no offset** (`pulse.payload`), like every datetime Odoo
 * sends a browser.  `Date.parse` reads an offsetless date-time as *local*
 * time, which would show the right rate at the wrong time -- out by the
 * browser's UTC offset -- so the fields are handed to `Date.UTC` instead.
 * That also sidesteps six-digit fractions, which the language's date format
 * does not define (it has three) and engines are free to reject.
 *
 * Anything else throws.  A payload that grew an offset would otherwise be read
 * as some other instant, and a wrong clock that looks right is the one failure
 * this page must not have.
 */
export function parseInstant(text) {
    const match = typeof text === 'string' && NAIVE_ISO.exec(text);
    if (!match) {
        throw new TypeError(`not a naive ISO datetime: ${JSON.stringify(text)}`);
    }
    const [, year, month, day, hour, minute, second, fraction = ''] = match;
    const millis = Number(fraction.padEnd(3, '0').slice(0, 3));
    return new Date(Date.UTC(year, month - 1, day, hour, minute, second, millis));
}

/**
 * Read a payload into what the page shows.
 *
 * Only what milestone 1 displays.  The rest of the payload -- `last_tick_real`,
 * `max_gap`, `server_real_now` -- is what an interpolating clock will need
 * (UI_DESIGN.md 7), and is left alone until something uses it.
 */
export function readClock(payload) {
    return {
        gameNow: parseInstant(payload.game_now),
        rate: payload.rate,
        paused: payload.paused === true,
        // Evaluated by the server, so that what this page says about the world
        // and what a write path refuses cannot drift apart.
        running: payload.running === true,
    };
}
