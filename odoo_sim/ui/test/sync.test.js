import assert from 'node:assert/strict';
import { beforeEach, mock, test } from 'node:test';

import { readWorld, syncWorld } from '../src/sync.js';

const answer = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
const settle = () => new Promise((resolve) => setImmediate(resolve));

/** A request the test answers by hand, in whatever order it likes. */
function deferred() {
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    return { request: () => promise, answer: (world) => resolve(answer(200, world)) };
}

let applied;
beforeEach(() => {
    applied = [];
    mock.restoreAll();
});

test('a refusal reads as the sentence the server gave', async () => {
    const response = answer(422, { name: 'odoo.exceptions.UserError', message: 'There is not enough Wire in the world' });
    await assert.rejects(readWorld(response), { message: 'There is not enough Wire in the world', status: 422 });
});

test('a failure with no JSON body still says something', async () => {
    const response = { status: 502, ok: false, json: async () => { throw new SyntaxError('no'); } };
    await assert.rejects(readWorld(response), { message: 'The server answered 502' });
});

test('an older answer never replaces a newer one', async () => {
    const fetchWorld = deferred();
    const press = deferred();
    const sync = syncWorld({ load: fetchWorld.request, onWorld: (world) => applied.push(world) });

    sync.refresh();                          // sent first
    const acting = sync.act(press.request);  // sent second
    press.answer('after the press');
    await acting;
    fetchWorld.answer('before the press');   // answers last, but is older
    await settle();

    assert.deepEqual(applied, ['after the press']);
});

test('a burst of notices costs one follow-up fetch, not one each', async () => {
    const answers = [];
    const load = () => new Promise((resolve) => answers.push(resolve));
    const sync = syncWorld({ load, onWorld: (world) => applied.push(world) });

    sync.refresh();
    sync.refresh();
    sync.refresh();
    sync.refresh();
    assert.equal(answers.length, 1, 'one in flight');

    answers[0](answer(200, 'first'));
    await settle();
    assert.equal(answers.length, 2, 'and exactly one more for everything that came in meanwhile');
    answers[1](answer(200, 'second'));
    await settle();

    assert.deepEqual(applied, ['first', 'second']);
});

test('a failed fetch is survived, and the next one still happens', async () => {
    mock.method(console, 'warn', () => {});
    let fail = true;
    const load = async () => (fail ? answer(502, {}) : answer(200, 'ok'));
    const sync = syncWorld({ load, onWorld: (world) => applied.push(world) });

    sync.refresh();
    await settle();
    fail = false;
    sync.refresh();
    await settle();

    assert.deepEqual(applied, ['ok']);
});

test('an action that is refused rejects, and changes nothing', async () => {
    const sync = syncWorld({ load: async () => answer(200, 'unused'), onWorld: (world) => applied.push(world) });

    await assert.rejects(sync.act(async () => answer(422, { message: 'Test bench is already running.' })), {
        message: 'Test bench is already running.',
    });
    assert.deepEqual(applied, []);
});
