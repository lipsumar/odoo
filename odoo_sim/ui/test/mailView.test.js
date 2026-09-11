import assert from 'node:assert/strict';
import { test } from 'node:test';

import { describeMail } from '../src/mailView.js';

const UTC = {
    datetime: new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }),
};

const running = { gameNow: new Date('2031-05-04T09:00:00Z'), rate: 720, paused: false, running: true };
const paused = { ...running, paused: true, running: false };

const quote = {
    id: 2, from: 'Carla Customer <carla@outside.example.com>', to: 'player@example.com',
    subject: 'Quote', preview: 'Can you send me a quote?', date: '2031-05-04T08:30:00', unread: true,
};
const shipped = {
    id: 1, from: 'Tensile Wire Supply <orders@tensile-wire.example.com>', to: 'player@example.com',
    subject: '', preview: 'On its way', date: '2031-05-03T17:00:00.000000', unread: false,
};

/** The player's mail, as `game.email._mailbox` builds it. */
function mail(extra = {}) {
    return { address: 'Pat Player <player@example.com>', unread: 1, inbox: [quote, shipped], sent: [], ...extra };
}

function show({ mailUi = {}, reading = running, pending, world = { mail: mail() } } = {}) {
    return describeMail({
        world, reading, pending, mailUi: { folder: 'inbox', open: null, compose: null, error: null, ...mailUi },
    }, UTC);
}

/** An email opened, as `GET /game/api/mail/<id>` answers. */
const opened = {
    ...quote, cc: '', reply_to: '', body: '<p>Can you send me a quote?</p>',
    attachments: ['specification.pdf'], reply: { to: quote.from, subject: 'Re: Quote' },
};

test('nothing to show before the world, and so the mail, has arrived', () => {
    assert.equal(show({ world: null }), null);
    assert.equal(show({ world: {} }), null, 'a world fetched without a player has no mail');
});

test('the inbox lists what came, saying how much is unread', () => {
    const shown = show();

    assert.deepEqual(shown.folders.map(({ label, current }) => [label, current]), [['Inbox (1)', true], ['Sent', false]]);
    assert.deepEqual(shown.items.map(({ who, subject, date, unread }) => [who, subject, date, unread]), [
        ['Carla Customer <carla@outside.example.com>', 'Quote', '4 May 2031, 08:30', true],
        ['Tensile Wire Supply <orders@tensile-wire.example.com>', '(no subject)', '3 May 2031, 17:00', false],
    ]);
    assert.equal(shown.empty, null);
});

test('the sent folder says who each email went to', () => {
    const sent = [{ ...quote, id: 5, to: 'carla@outside.example.com' }];
    const shown = show({ world: { mail: mail({ sent }) }, mailUi: { folder: 'sent' } });

    assert.equal(shown.items[0].who, 'To carla@outside.example.com');
    assert.equal(show({ mailUi: { folder: 'sent' } }).empty, 'Nothing sent yet.');
});

test('an open email shows its headers, body and attachments', () => {
    const shown = show({ mailUi: { open: opened } });

    assert.equal(shown.items[0].open, true);
    assert.equal(shown.open.subject, 'Quote');
    assert.deepEqual(shown.open.headers.map(([name]) => name), ['From', 'To', 'Date'], 'an empty Cc is left out');
    assert.equal(shown.open.body, '<p>Can you send me a quote?</p>');
    assert.equal(shown.open.attachments, 'Attached: specification.pdf');
    assert.equal(shown.open.canReply, true);
});

test('a reply is titled as one, and starts from what the server proposed', () => {
    const compose = { key: 1, to: opened.reply.to, cc: '', subject: opened.reply.subject, parentId: 2 };
    const shown = show({ mailUi: { open: opened, compose } });

    assert.equal(shown.compose.title, 'Reply');
    assert.equal(shown.compose.subject, 'Re: Quote');
    assert.equal(shown.compose.canSend, true);
    assert.equal(show({ mailUi: { compose: { ...compose, parentId: null } } }).compose.title, 'New email');
});

test('nothing is sent while the world stands still, or while a send is in flight', () => {
    const compose = { key: 1, to: 'carla@outside.example.com', cc: '', subject: 'Hi', parentId: null };

    const stopped = show({ mailUi: { compose }, reading: paused }).compose;
    assert.equal(stopped.canSend, false);
    assert.equal(stopped.waiting, 'Nothing can be sent until time moves again.');
    assert.equal(show({ mailUi: { compose }, reading: null }).compose.canSend, false);
    assert.equal(show({ mailUi: { compose }, pending: new Set(['mail:send']) }).compose.canSend, false);
});

test('a player without an address is told why no mail comes, and cannot write', () => {
    const shown = show({ world: { mail: mail({ address: null }) }, mailUi: { open: opened, compose: { key: 1 } } });

    assert.match(shown.address, /no email address/);
    assert.equal(shown.canWrite, false);
    assert.equal(shown.open.canReply, false);
    assert.equal(shown.compose.canSend, false);
});
