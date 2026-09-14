/**
 * The player's mail: a bottom drawer that can be dragged (or clicked) taller
 * or shorter, showing one of three things at a time -- the inbox list, one
 * open email with a way back, or a form to write one -- the way a real mail
 * app on a small screen works.  See MAIL.md 7.
 *
 * The inbox is deliberately dumb: it only ever shows raw addresses, never a
 * sender's name, even though the game world knows perfectly well who Carla
 * Customer is.  Matching an address to a customer is the player's job, not
 * something the inbox should give away.
 *
 * As with the world, `describeMail` is the whole of the decision and touches
 * no DOM, and `createMailView` updates the elements it keeps rather than
 * rebuilding them -- the form above all, which holds what the player is
 * typing while the page re-renders on every pulse.
 */
import { element, keyed } from './dom.js';
import { pageFormats } from './formats.js';
import { parseInstant } from './reading.js';

const FOLDERS = [
    { key: 'inbox', label: 'Inbox', empty: 'No mail.' },
    { key: 'sent', label: 'Sent', empty: 'Nothing sent yet.' },
];

/** The address inside "Name <addr>", or the text itself if it is bare already. */
function bareAddress(text) {
    const match = /<([^<>]*)>/.exec(text || '');
    return (match ? match[1] : text || '').trim();
}

/** As `bareAddress`, over a comma-separated header of one or more addresses. */
function bareAddresses(value) {
    return (value || '').split(',').map(bareAddress).filter(Boolean).join(', ');
}

function describeOpen(email, formats, canWrite) {
    return {
        key: email.id,
        subject: email.subject || '(no subject)',
        headers: [
            ['From', bareAddresses(email.from)],
            ['To', bareAddresses(email.to)],
            ['Cc', bareAddresses(email.cc)],
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
 * values, under a `key` that is new for each form), and an `error`.  `view`
 * says which of the three the drawer should show, and follows from `open`
 * and `compose` directly, so a caller need not compute it.  Nothing can be
 * sent while the world is not running -- the server refuses it anyway,
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
    const { open, compose } = mailUi;
    const openId = open?.id ?? null;
    const view = compose ? 'compose' : open ? 'open' : 'list';
    return {
        view,
        unread: mail.unread,
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
            who: folder.key === 'sent' ? `To ${bareAddresses(email.to)}` : bareAddresses(email.from),
            subject: email.subject || '(no subject)',
            preview: email.preview,
            date: formats.datetime.format(parseInstant(email.date)),
            unread: Boolean(email.unread),
            open: email.id === openId,
        })),
        empty: listed.length ? null : folder.empty,
        canWrite,
        open: open ? describeOpen(open, formats, canWrite) : null,
        compose: compose ? {
            key: compose.key,
            to: bareAddress(compose.to),
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
 * someone else's HTML.  Links open in a new tab.
 */
function bodyDocument(body) {
    return '<!doctype html><meta charset="utf-8"><base target="_blank">'
        + '<style>body { margin: 0.75rem; font: 15px/1.5 system-ui, sans-serif; color: #1c1c1a; background: #fff; }</style>'
        + body;
}

/** Drawer heights, in pixels: just the handle, and a comfortable default once opened. */
const PEEK_HEIGHT = 52;
const OPEN_HEIGHT = 420;
/** Pointer movement, in pixels, below which a press on the handle is a click, not a drag. */
const DRAG_THRESHOLD = 6;

/**
 * Build the mail drawer inside `root`, and return `render(state)` to update it.
 *
 * `actions`: `folder(key)`, `open(emailId)`, `close()`, `write()`, `reply()`,
 * `send({ to, subject, body })`, `discard()`.
 */
export function createMailView(root, actions, formats = pageFormats()) {
    const grip = element('span', 'mail-grip');
    const title = element('span', 'mail-title', 'Mail');
    const badge = element('span', 'mail-badge');
    const handle = element('button', 'mail-handle');
    handle.type = 'button';
    handle.append(grip, title, badge);

    const address = element('p', 'mail-address');
    const folders = element('nav', 'mail-folders');
    const write = button('New', () => actions.write(), 'secondary');
    const bar = element('div', 'mail-bar');
    bar.append(folders, write);

    const list = element('ul', 'mail-list');
    const empty = element('p', 'empty');
    const listing = element('div', 'mail-listing');
    listing.append(bar, list, empty);

    const openSubject = element('h3');
    const openHeader = element('div', 'mail-open-header');
    openHeader.append(button('← Back', () => actions.close(), 'secondary'), openSubject);
    const headers = element('dl', 'mail-headers');
    const frame = element('iframe', 'mail-content');
    frame.setAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox');
    frame.title = 'Email';
    const attachments = element('p', 'mail-attachments');
    const reply = button('Reply', () => actions.reply());
    const readerActions = element('div', 'mail-actions');
    readerActions.append(reply);
    const reader = element('article', 'mail-open');
    reader.append(openHeader, headers, frame, attachments, readerActions);

    const formHeader = element('div', 'mail-open-header');
    const formTitle = element('h3');
    formHeader.append(button('← Back', () => actions.discard(), 'secondary'), formTitle);
    const to = element('input');
    to.placeholder = 'name@example.com';
    const subjectInput = element('input');
    const body = element('textarea');
    body.rows = 8;
    const waiting = element('p', 'mail-waiting');
    const send = element('button', null, 'Send');
    send.type = 'submit';
    const formActions = element('div', 'mail-actions');
    formActions.append(send);
    const form = element('form', 'mail-compose');
    form.append(formHeader, field('To', to), field('Subject', subjectInput), field('Message', body), waiting, formActions);
    form.addEventListener('submit', (event) => {
        event.preventDefault();
        actions.send({ to: to.value, subject: subjectInput.value, body: body.value });
    });

    const error = element('p', 'mail-error');
    error.setAttribute('role', 'alert');

    const panelBody = element('div', 'mail-body');
    panelBody.append(address, error, listing, reader, form);

    const panel = element('div', 'mail');
    panel.append(handle, panelBody);
    root.replaceChildren(panel);

    // -- drag-or-click to resize the drawer -----------------------------------
    let height = PEEK_HEIGHT;
    let dragging = false;
    let dragStartY = 0;
    let dragStartHeight = 0;
    let moved = false;

    function setHeight(px, animated) {
        height = Math.max(PEEK_HEIGHT, Math.min(px, Math.round(window.innerHeight * 0.92)));
        panel.classList.toggle('dragging', !animated);
        panel.style.height = `${height}px`;
    }

    handle.addEventListener('pointerdown', (event) => {
        dragging = true;
        moved = false;
        dragStartY = event.clientY;
        dragStartHeight = height;
        handle.setPointerCapture(event.pointerId);
    });
    handle.addEventListener('pointermove', (event) => {
        if (!dragging) {
            return;
        }
        const delta = dragStartY - event.clientY;
        if (Math.abs(delta) > DRAG_THRESHOLD) {
            moved = true;
        }
        if (moved) {
            setHeight(dragStartHeight + delta, false);
        }
    });
    function endDrag() {
        if (!dragging) {
            return;
        }
        dragging = false;
        panel.classList.remove('dragging');
        if (!moved) {
            setHeight(height > PEEK_HEIGHT + 8 ? PEEK_HEIGHT : OPEN_HEIGHT, true);
        }
    }
    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);
    setHeight(PEEK_HEIGHT, true);

    // The email and the form currently shown, so that they are only filled in
    // when they change: an email never does, and a form belongs to the player.
    let openKey = null;
    let composeKey = null;
    let lastView = 'list';

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
        if (!open) {
            openKey = null;
            return;
        }
        if (open.key !== openKey) {
            openKey = open.key;
            openSubject.textContent = open.subject;
            headers.replaceChildren(...open.headers.flatMap(([name, value]) => [
                element('dt', null, name), element('dd', null, value),
            ]));
            frame.srcdoc = bodyDocument(open.body);
            attachments.hidden = !open.attachments;
            attachments.textContent = open.attachments ?? '';
            panelBody.scrollTop = 0;
        }
        reply.disabled = !open.canReply;
    }

    function showCompose(compose) {
        if (!compose) {
            composeKey = null;
            return;
        }
        if (compose.key !== composeKey) {
            composeKey = compose.key;
            formTitle.textContent = compose.title;
            to.value = compose.to ?? '';
            subjectInput.value = compose.subject ?? '';
            body.value = '';
            panelBody.scrollTop = 0;
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
        if (shown.view !== 'list' && lastView === 'list' && height <= PEEK_HEIGHT + 8) {
            setHeight(OPEN_HEIGHT, true);
        }
        lastView = shown.view;

        badge.textContent = shown.unread ? String(shown.unread) : '';
        address.textContent = shown.address;
        write.disabled = !shown.canWrite;
        keyed(folders, shown.folders, createFolder, updateFolder);
        keyed(list, shown.items, createItem, updateItem);
        empty.hidden = !shown.empty;
        empty.textContent = shown.empty ?? '';
        error.hidden = !shown.error;
        error.textContent = shown.error ?? '';

        listing.hidden = shown.view !== 'list';
        reader.hidden = shown.view !== 'open';
        form.hidden = shown.view !== 'compose';
        showOpen(shown.open);
        showCompose(shown.compose);
    };
}
