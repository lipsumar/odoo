/**
 * The player's mail on the page: their inbox, what they sent, one email open,
 * and a form to write or answer one.  See MAIL.md.
 *
 * As with the world, `describeMail` is the whole of the decision and touches
 * no DOM, and `createMailView` updates the elements it keeps rather than
 * rebuilding them -- the form above all, which holds what the player is
 * typing while the page re-renders on every pulse.
 */
import { parseInstant } from './reading.js';
import { element, keyed, worldFormats } from './worldView.js';

const FOLDERS = [
    { key: 'inbox', label: 'Inbox', empty: 'No mail.' },
    { key: 'sent', label: 'Sent', empty: 'Nothing sent yet.' },
];

function describeOpen(email, formats, canWrite) {
    return {
        key: email.id,
        subject: email.subject || '(no subject)',
        headers: [
            ['From', email.from],
            ['To', email.to],
            ['Cc', email.cc],
            ['Date', formats.datetime.format(parseInstant(email.date))],
        ].filter(([, value]) => value),
        body: email.body,
        attachments: email.attachments.length ? `Attached: ${email.attachments.join(', ')}` : null,
        canReply: canWrite,
    };
}

/**
 * Describe the player's mail as what to show, or null before it has arrived
 * (it comes with the world, `world.mail`).
 *
 * `mailUi` is the page's own state: the `folder` shown, the email `open` (as
 * `GET /game/api/mail/<id>` answers it), `compose` (the form's starting
 * values, under a `key` that is new for each form), and an `error`.  Nothing
 * can be sent while the world is not running -- the server refuses it anyway,
 * because the post office is a cron -- or while a send is in flight.
 */
export function describeMail({ world, reading, mailUi, pending = new Set() }, formats) {
    const mail = world?.mail;
    if (!mail) {
        return null;
    }
    const acting = Boolean(reading?.running);
    const canWrite = Boolean(mail.address);
    const folder = FOLDERS.find(({ key }) => key === mailUi.folder) ?? FOLDERS[0];
    const listed = folder.key === 'sent' ? mail.sent : mail.inbox;
    const openId = mailUi.open?.id ?? null;
    const { compose } = mailUi;
    return {
        address: canWrite
            ? `You are ${mail.address}`
            : 'You have no email address, so no mail reaches you. Set one in your Odoo preferences.',
        folders: FOLDERS.map(({ key, label }) => ({
            key,
            label: key === 'inbox' && mail.unread ? `${label} (${mail.unread})` : label,
            current: key === folder.key,
        })),
        items: listed.map((email) => ({
            key: email.id,
            who: folder.key === 'sent' ? `To ${email.to}` : email.from,
            subject: email.subject || '(no subject)',
            preview: email.preview,
            date: formats.datetime.format(parseInstant(email.date)),
            unread: Boolean(email.unread),
            open: email.id === openId,
        })),
        empty: listed.length ? null : folder.empty,
        canWrite,
        open: mailUi.open ? describeOpen(mailUi.open, formats, canWrite) : null,
        compose: compose ? {
            key: compose.key,
            to: compose.to,
            cc: compose.cc,
            subject: compose.subject,
            title: compose.parentId ? 'Reply' : 'New email',
            canSend: acting && canWrite && !pending.has('mail:send'),
            waiting: acting ? null : 'Nothing can be sent until time moves again.',
        } : null,
        error: mailUi.error ?? null,
    };
}

function button(label, onClick, className) {
    const node = element('button', className, label);
    node.type = 'button';
    node.addEventListener('click', onClick);
    return node;
}

function field(label, control) {
    const wrapper = element('label', null, label);
    wrapper.append(control);
    return wrapper;
}

/**
 * The frame an email's body is shown in.  Sandboxed with no scripts and no
 * same-origin access: the body is sanitized on the server, and is still
 * someone else's HTML.  Links open in a new tab, which the sandbox allows.
 */
function bodyDocument(body) {
    return '<!doctype html><meta charset="utf-8"><base target="_blank">'
        + '<style>body { margin: 0.75rem; font: 15px/1.5 system-ui, sans-serif; color: #1c1c1a; background: #fff; }</style>'
        + body;
}

/**
 * Build the mail panel inside `root`, and return `render(state)` to update it.
 *
 * `actions`: `folder(key)`, `open(emailId)`, `close()`, `write()`, `reply()`,
 * `send({ to, cc, subject, body })`, `discard()`.
 */
export function createMailView(root, actions, formats = worldFormats()) {
    const address = element('p', 'mail-address');
    const folders = element('nav', 'mail-folders');
    const write = button('Write', () => actions.write(), 'secondary');
    const bar = element('div', 'mail-bar');
    bar.append(folders, write);

    const list = element('ul', 'mail-list');
    const empty = element('p', 'empty');
    const listing = element('div', 'mail-listing');
    listing.append(list, empty);

    const subject = element('h3');
    const headers = element('dl', 'mail-headers');
    const frame = element('iframe', 'mail-content');
    frame.setAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox');
    frame.title = 'Email';
    const attachments = element('p', 'mail-attachments');
    const reply = button('Reply', () => actions.reply());
    const readerActions = element('div', 'mail-actions');
    readerActions.append(reply, button('Close', () => actions.close(), 'secondary'));
    const reader = element('article', 'mail-open');
    reader.append(subject, headers, frame, attachments, readerActions);

    const formTitle = element('h3');
    const to = element('input');
    const cc = element('input');
    const subjectInput = element('input');
    const body = element('textarea');
    body.rows = 8;
    const waiting = element('p', 'mail-waiting');
    const send = element('button', null, 'Send');
    send.type = 'submit';
    const formActions = element('div', 'mail-actions');
    formActions.append(send, button('Discard', () => actions.discard(), 'secondary'));
    const form = element('form', 'mail-compose');
    form.append(formTitle, field('To', to), field('Cc', cc), field('Subject', subjectInput), field('Message', body), waiting, formActions);
    form.addEventListener('submit', (event) => {
        event.preventDefault();
        actions.send({ to: to.value, cc: cc.value, subject: subjectInput.value, body: body.value });
    });

    const error = element('p', 'mail-error');
    error.setAttribute('role', 'alert');
    const pane = element('div', 'mail-pane');
    pane.append(error, reader, form);
    const columns = element('div', 'mail-columns');
    columns.append(listing, pane);

    const panel = element('section', 'panel mail');
    panel.append(element('h2', null, 'Mail'), address, bar, columns);
    root.replaceChildren(panel);

    // The email and the form currently shown, so that they are only filled in
    // when they change: an email never does, and a form belongs to the player.
    let openKey = null;
    let composeKey = null;

    function createFolder(folder) {
        return button('', () => actions.folder(folder.key), 'mail-folder');
    }

    function updateFolder(node, folder) {
        node.textContent = folder.label;
        node.setAttribute('aria-pressed', String(folder.current));
    }

    function createItem(item) {
        const node = element('li', 'mail-item');
        const refs = {
            who: element('span', 'mail-who'),
            date: element('span', 'mail-date'),
            subject: element('span', 'mail-subject'),
            preview: element('span', 'mail-preview'),
        };
        const open = button('', () => actions.open(item.key));
        open.append(refs.who, refs.date, refs.subject, refs.preview);
        node.append(open);
        node.refs = refs;
        return node;
    }

    function updateItem(node, item) {
        const { refs } = node;
        refs.who.textContent = item.who;
        refs.date.textContent = item.date;
        refs.subject.textContent = item.subject;
        refs.preview.textContent = item.preview;
        node.dataset.unread = item.unread;
        node.dataset.open = item.open;
    }

    function showOpen(open) {
        reader.hidden = !open;
        if (!open) {
            openKey = null;
            return;
        }
        if (open.key !== openKey) {
            openKey = open.key;
            subject.textContent = open.subject;
            headers.replaceChildren(...open.headers.flatMap(([name, value]) => [
                element('dt', null, name), element('dd', null, value),
            ]));
            frame.srcdoc = bodyDocument(open.body);
            attachments.hidden = !open.attachments;
            attachments.textContent = open.attachments ?? '';
        }
        reply.disabled = !open.canReply;
    }

    function showCompose(compose) {
        form.hidden = !compose;
        if (!compose) {
            composeKey = null;
            return;
        }
        if (compose.key !== composeKey) {
            composeKey = compose.key;
            formTitle.textContent = compose.title;
            to.value = compose.to ?? '';
            cc.value = compose.cc ?? '';
            subjectInput.value = compose.subject ?? '';
            body.value = '';
            (compose.to ? body : to).focus();
        }
        send.disabled = !compose.canSend;
        waiting.hidden = !compose.waiting;
        waiting.textContent = compose.waiting ?? '';
    }

    return function render(state) {
        const shown = describeMail(state, formats);
        panel.hidden = !shown;
        if (!shown) {
            return;
        }
        address.textContent = shown.address;
        write.disabled = !shown.canWrite;
        keyed(folders, shown.folders, createFolder, updateFolder);
        keyed(list, shown.items, createItem, updateItem);
        empty.hidden = !shown.empty;
        empty.textContent = shown.empty ?? '';
        error.hidden = !shown.error;
        error.textContent = shown.error ?? '';
        showOpen(shown.open);
        showCompose(shown.compose);
    };
}
