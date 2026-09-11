import assert from 'node:assert/strict';
import { test } from 'node:test';

import { describeWorld, moneyFormat } from '../src/worldView.js';

const UTC = {
    number: new Intl.NumberFormat('en-GB', { maximumFractionDigits: 2 }),
    time: new Intl.DateTimeFormat('en-GB', { timeStyle: 'short', timeZone: 'UTC' }),
    datetime: new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }),
    money: moneyFormat('en-GB'),
};

const wire = { id: 1, name: 'Wire', uom: 'm' };
const clip = { id: 2, name: 'Paperclip', uom: 'Units' };

/** A snapshot, as `game.world._snapshot` builds one. */
function world({ run = null, shipments = [], bank = null, orders = [] } = {}) {
    return {
        stock: [{ ...wire, qty: 49.9 }, { ...clip, qty: 0 }],
        workstations: [{
            id: 7,
            name: 'Paperclip bench',
            product: clip,
            duration: 2,
            recipe: [{ ...wire, qty: 0.1 }],
            run,
            orders: [{ id: 30, name: 'WH/MO/00030', qty: 10 }],
        }],
        shipments,
        bank,
        customer_orders: orders,
    };
}

const running = (gameNow) => ({ gameNow: new Date(gameNow), rate: 1440, paused: false, running: true });

const run = {
    id: 3, qty: 10, order: { id: 30, name: 'WH/MO/00030' },
    date_start: '2030-03-01T09:00:00', date_end: '2030-03-01T09:20:00.000000',
};

test('nothing to show before the world has arrived', () => {
    assert.equal(describeWorld({ world: null, reading: null }, UTC), null);
});

test('what exists, in its own unit, and counted things without one', () => {
    const shown = describeWorld({ world: world(), reading: running('2030-03-01T09:00:00Z') }, UTC);

    assert.deepEqual(shown.stock.map((line) => [line.name, line.qty]), [['Wire', '49.9 m'], ['Paperclip', '0']]);
});

test('a free station says what it takes and offers the open orders', () => {
    const [station] = describeWorld({ world: world(), reading: running('2030-03-01T09:00:00Z') }, UTC).stations;

    assert.equal(station.recipe, '0.1 m Wire → 1 Paperclip, 2 game min each');
    assert.deepEqual(station.orders, [{ id: 30, label: 'WH/MO/00030 (10)' }]);
    assert.equal(station.canStart, true);
    assert.equal(station.run, null);
});

test('a running station shows how far along it is', () => {
    const [station] = describeWorld({ world: world({ run }), reading: running('2030-03-01T09:05:00Z') }, UTC).stations;

    assert.equal(station.canStart, false, 'one run at a time');
    assert.equal(station.run.text, 'Making 10 Paperclip for WH/MO/00030');
    assert.equal(station.run.progress, 0.25);
    assert.equal(station.run.status, 'Done at 09:20');
});

test('a run past its end is finishing, until the world settles it', () => {
    const [station] = describeWorld({ world: world({ run }), reading: running('2030-03-01T09:30:00Z') }, UTC).stations;

    assert.equal(station.run.progress, 1);
    assert.equal(station.run.status, 'Finishing…');
});

test('nothing can be started in a world that is not running', () => {
    const paused = { gameNow: new Date('2030-03-01T09:00:00Z'), rate: 1440, paused: true, running: false };
    for (const reading of [paused, null]) {
        const [station] = describeWorld({ world: world(), reading }, UTC).stations;
        assert.equal(station.canStart, false);
    }
});

test('a pressed button waits for its answer', () => {
    const pending = new Set(['station:7']);
    const [station] = describeWorld({ world: world(), reading: running('2030-03-01T09:00:00Z'), pending }, UTC).stations;

    assert.equal(station.canStart, false);
});

const shipment = {
    id: 5, vendor: 'Tensile Wire Supply', order: 'P00001', state: 'in_transit',
    date_shipped: '2030-03-01T09:00:00', date_arrival: '2030-03-02T09:00:00',
    lines: [{ ...wire, qty: 100 }],
};

test('a delivery on its way says when it arrives, and cannot be accepted', () => {
    const [shown] = describeWorld({ world: world({ shipments: [shipment] }), reading: running('2030-03-01T12:00:00Z') }, UTC).shipments;

    assert.equal(shown.title, 'P00001 from Tensile Wire Supply');
    assert.equal(shown.contents, '100 m Wire');
    assert.equal(shown.status, 'Arrives 2 Mar 2030, 09:00');
    assert.equal(shown.canAccept, false);
});

test('a delivery is at the door once its time has come, settled or not', () => {
    for (const [state, gameNow] of [['arrived', '2030-03-02T10:00:00Z'], ['in_transit', '2030-03-02T09:00:00Z']]) {
        const [shown] = describeWorld({
            world: world({ shipments: [{ ...shipment, state }] }), reading: running(gameNow),
        }, UTC).shipments;
        assert.equal(shown.status, 'At the door', state);
        assert.equal(shown.canAccept, true, state);
    }
});

const bank = {
    number: 'GB000001',
    balance: 8,
    currency: 'EUR',
    transactions: [
        { id: 2, date: '2030-03-01T13:00:00', amount: 8, counterparty: 'Binder & Co.', reference: 'INV/2030/00001' },
        { id: 1, date: '2030-03-01T12:00:00', amount: -2.5, counterparty: 'Tensile Wire Supply', reference: null },
    ],
};

test('the bank shows the balance, and which way each payment went', () => {
    const shown = describeWorld({ world: world({ bank }), reading: running('2030-03-01T14:00:00Z') }, UTC).bank;

    assert.equal(shown.balance, '€8.00');
    assert.equal(shown.number, 'Account GB000001');
    assert.deepEqual(shown.transactions.map(({ amount, text, date, incoming }) => [amount, text, date, incoming]), [
        ['+€8.00', 'Binder & Co. · INV/2030/00001', '1 Mar 2030, 13:00', true],
        ['−€2.50', 'Tensile Wire Supply', '1 Mar 2030, 12:00', false],
    ]);
});

test('a world with no company account shows no bank', () => {
    assert.equal(describeWorld({ world: world(), reading: null }, UTC).bank, null);
});

const order = {
    id: 9, customer: 'Binder & Co.', product: clip, qty: 200, max_price: 0.08, currency: 'EUR',
    state: 'requested', date_requested: '2030-03-01T09:00:00', qty_paid: 0, amount_paid: 0, invoice: null,
};
const accepted = {
    id: 4, name: 'INV/2030/00001', amount: 11.5, qty: 200, state: 'to_pay', reason: null,
    date_due: '2030-03-01T13:00:00',
};

function shownOrder(overrides, gameNow = '2030-03-01T12:00:00Z', { pending = new Set(), reading = running(gameNow) } = {}) {
    return describeWorld({ world: world({ orders: [{ ...order, ...overrides }] }), reading, pending }, UTC).orders[0];
}

test('an order says what the customer wants, and waits for an invoice', () => {
    const shown = shownOrder({});

    assert.equal(shown.title, 'Binder & Co. wants 200 Paperclip');
    assert.equal(shown.terms, 'Pays up to €0.08 each, taxes included');
    assert.equal(shown.status, 'Waiting for your invoice');
    assert.equal(shown.ship, null);
    assert.equal(shown.canShip, false);
});

test('a refused invoice says why', () => {
    const shown = shownOrder({ invoice: { ...accepted, state: 'refused', reason: 'Too dear.' } });

    assert.equal(shown.status, 'Refused INV/2030/00001: Too dear.');
    assert.equal(shown.tone, 'fault');
    assert.equal(shown.canShip, false);
});

test('an accepted invoice says when it will be paid, and nothing ships before', () => {
    const shown = shownOrder({ state: 'invoiced', invoice: accepted });

    assert.equal(shown.status, 'Accepted INV/2030/00001 for €11.50; pays 1 Mar 2030, 13:00');
    assert.equal(shown.canShip, false);
});

test('once due, a payment counts, settled or not', () => {
    const shown = shownOrder({ state: 'invoiced', invoice: accepted }, '2030-03-01T13:00:00Z');

    assert.equal(shown.status, 'Paying INV/2030/00001…');
    assert.equal(shown.ship, 'Ship 200 Paperclip');
    assert.equal(shown.canShip, true);
});

test('a paid order ships what was paid for', () => {
    const paid = { state: 'paid', qty_paid: 150, amount_paid: 9, invoice: { ...accepted, state: 'paid', qty: 150 } };
    const shown = shownOrder(paid);

    assert.equal(shown.status, 'Paid €9.00');
    assert.equal(shown.ship, 'Ship 150 Paperclip');
    assert.equal(shown.canShip, true);

    assert.equal(shownOrder(paid, undefined, { pending: new Set(['order:9']) }).canShip, false, 'pressed');
    const paused = { gameNow: new Date('2030-03-01T12:00:00Z'), rate: 1440, paused: true, running: false };
    assert.equal(shownOrder(paid, undefined, { reading: paused }).canShip, false, 'paused');
});
