// A zone far from UTC, set before any Date is made: were the payload read as
// local time, every instant below would come out thirteen hours off.
process.env.TZ = 'Pacific/Auckland';

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseInstant, readClock } from '../src/reading.js';

test('a payload datetime is UTC, not local time', () => {
    assert.equal(
        parseInstant('2026-09-10T14:23:07.123456').toISOString(),
        '2026-09-10T14:23:07.123Z',
    );
});

test('a datetime whose microseconds are zero parses too', () => {
    // Python's isoformat() leaves the fraction out entirely when it is zero,
    // about one tick in a million.
    assert.equal(parseInstant('2026-09-09T12:00:00').toISOString(), '2026-09-09T12:00:00.000Z');
});

test('a short fraction is a fraction of a second, not of a millisecond', () => {
    assert.equal(parseInstant('2026-09-09T12:00:00.5').toISOString(), '2026-09-09T12:00:00.500Z');
});

test('anything but a naive ISO datetime is refused', () => {
    for (const text of ['2026-09-10T14:23:07Z', '2026-09-10T14:23:07+02:00', '2026-09-10 14:23:07', '', null]) {
        assert.throws(() => parseInstant(text), TypeError, JSON.stringify(text));
    }
});

test('a payload reads into what the page shows', () => {
    const reading = readClock({
        game_now: '2030-03-01T09:30:00.123456',
        last_tick_real: '2026-09-10T13:26:04.000000',
        rate: 1440.0,
        paused: false,
        max_gap: 5.0,
        running: true,
        server_real_now: '2026-09-10T13:26:04.412000',
    });
    assert.deepEqual(reading, {
        gameNow: new Date('2030-03-01T09:30:00.123Z'),
        rate: 1440,
        paused: false,
        running: true,
    });
});
