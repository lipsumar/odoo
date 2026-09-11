/**
 * The world on the page: what exists, what the workstations are doing, and
 * what is at the door.  See GAME_STATE.md 8.
 *
 * As with the clock, `describeWorld` is the whole of the decision and touches
 * no DOM; `createWorldView` writes its answer into elements it keeps, rather
 * than rebuilding them, because it is re-run on every pulse and a rebuilt
 * input loses what the player was typing into it.
 */
import { parseInstant } from './reading.js';

/** Odoo's counting unit, left out of quantities: "10 Paperclip", not "10 Units Paperclip". */
const COUNTING_UNIT = 'Units';

export function worldFormats() {
    return {
        number: new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }),
        time: new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }),
        datetime: new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }),
    };
}

function amount(qty, uom, formats) {
    const number = formats.number.format(qty);
    return uom === COUNTING_UNIT ? number : `${number} ${uom}`;
}

const goods = (line, formats) => `${amount(line.qty, line.uom, formats)} ${line.name}`;

function describeStation(station, now, acting, pending, formats) {
    const shown = {
        key: station.id,
        name: station.name,
        recipe: `${station.recipe.map((line) => goods(line, formats)).join(' + ')} → `
            + `${goods({ ...station.product, qty: 1 }, formats)}, `
            + `${formats.number.format(station.duration)} game min each`,
        orders: station.orders.map((order) => ({
            id: order.id,
            label: `${order.name} (${formats.number.format(order.qty)})`,
        })),
        canStart: acting && !station.run && !pending,
        run: null,
    };
    const { run } = station;
    if (run) {
        const start = parseInstant(run.date_start);
        const end = parseInstant(run.date_end);
        const over = now !== null && now >= end;
        shown.run = {
            text: `Making ${goods({ ...station.product, qty: run.qty }, formats)}`
                + (run.order ? ` for ${run.order.name}` : ''),
            progress: over ? 1 : now === null ? 0 : Math.max(0, (now - start) / (end - start)),
            // Past its end but not yet settled by the cron: the goods land
            // within a cron tick, or at once if anyone acts in the meantime.
            status: over ? 'Finishing…' : `Done at ${formats.time.format(end)}`,
        };
    }
    return shown;
}

function describeShipment(shipment, now, acting, pending, formats) {
    const arrival = parseInstant(shipment.date_arrival);
    // Arrived by the clock counts as arrived: accepting settles first.
    const here = shipment.state === 'arrived' || (now !== null && now >= arrival);
    return {
        key: shipment.id,
        title: shipment.order ? `${shipment.order} from ${shipment.vendor}` : `From ${shipment.vendor}`,
        contents: shipment.lines.map((line) => goods(line, formats)).join(', '),
        here,
        status: here ? 'At the door' : `Arrives ${formats.datetime.format(arrival)}`,
        canAccept: acting && here && !pending,
    };
}

/**
 * Describe `{ world, reading, pending, error }` as what to show.
 *
 * `reading` is the clock (`readClock`), used for progress and for whether the
 * world is running at all: nothing can be done in a paused or stopped world,
 * and the server refuses it anyway.  `pending` holds the keys of actions in
 * flight (`station:<id>`, `shipment:<id>`), whose buttons wait.
 */
export function describeWorld({ world, reading, pending = new Set(), error = null }, formats) {
    if (!world) {
        return null;
    }
    const now = reading ? reading.gameNow : null;
    const acting = Boolean(reading?.running);
    return {
        error,
        stock: world.stock.map((line) => ({
            key: line.id,
            name: line.name,
            qty: amount(line.qty, line.uom, formats),
        })),
        stations: world.workstations.map((station) => describeStation(
            station, now, acting, pending.has(`station:${station.id}`), formats,
        )),
        shipments: world.shipments.map((shipment) => describeShipment(
            shipment, now, acting, pending.has(`shipment:${shipment.id}`), formats,
        )),
    };
}

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
        node.className = className;
    }
    if (text !== undefined) {
        node.textContent = text;
    }
    return node;
}

function panel(title, ...children) {
    const section = element('section', 'panel');
    section.append(element('h2', null, title), ...children);
    return section;
}

/**
 * Make `container`'s children match `items`, keyed by `item.key`: existing
 * nodes are updated in place and only moved when the list itself changed, so
 * a focused input or a half-pressed button survives a render.
 */
function keyed(container, items, create, update) {
    const existing = new Map([...container.children].map((node) => [node.dataset.key, node]));
    const nodes = items.map((item) => {
        let node = existing.get(String(item.key));
        if (!node) {
            node = create(item);
            node.dataset.key = item.key;
        }
        update(node, item);
        return node;
    });
    const same = nodes.length === container.children.length
        && nodes.every((node, index) => container.children[index] === node);
    if (!same) {
        container.replaceChildren(...nodes);
    }
}

/**
 * Build the world inside `root`, and return `render(state)` to update it.
 *
 * `actions.start(stationId, { qty, productionId })` and
 * `actions.accept(shipmentId)` are called when the player presses a button.
 */
export function createWorldView(root, actions, formats = worldFormats()) {
    const error = element('p', 'world-error');
    error.setAttribute('role', 'alert');

    const stockRows = element('tbody');
    const stockTable = element('table', 'stock');
    const head = element('thead');
    const headRow = element('tr');
    headRow.append(element('th', null, 'Product'), element('th', 'qty', 'On hand'));
    head.append(headRow);
    stockTable.append(head, stockRows);

    const stations = element('div', 'stations');
    const shipments = element('ul', 'shipments');
    const noShipments = element('p', 'empty', 'Nothing on the way.');

    const world = element('div', 'world');
    world.append(
        error,
        panel('On hand', stockTable),
        panel('Workstations', stations),
        panel('Deliveries', shipments, noShipments),
    );
    root.replaceChildren(world);

    function createStock() {
        const row = element('tr');
        row.append(element('td'), element('td', 'qty'));
        return row;
    }

    function updateStock(row, line) {
        row.children[0].textContent = line.name;
        row.children[1].textContent = line.qty;
    }

    function createStation(station) {
        const card = element('article', 'station');
        const refs = {
            name: element('h3'),
            recipe: element('p', 'recipe'),
            form: element('form', 'station-form'),
            qty: element('input'),
            order: element('select'),
            button: element('button', null, 'Manufacture'),
            run: element('div', 'run'),
            runText: element('p'),
            progress: element('progress'),
            runStatus: element('p', 'run-status'),
        };
        refs.qty.type = 'number';
        refs.qty.min = '1';
        refs.qty.step = '1';
        refs.qty.value = '1';
        refs.qty.required = true;
        refs.button.type = 'submit';
        refs.progress.max = 1;

        const qtyLabel = element('label', null, 'Quantity ');
        qtyLabel.append(refs.qty);
        const orderLabel = element('label', null, 'For ');
        orderLabel.append(refs.order);
        refs.form.append(qtyLabel, orderLabel, refs.button);
        refs.form.addEventListener('submit', (event) => {
            event.preventDefault();
            actions.start(station.key, {
                qty: Number(refs.qty.value),
                productionId: Number(refs.order.value) || null,
            });
        });
        refs.run.append(refs.runText, refs.progress, refs.runStatus);
        card.append(refs.name, refs.recipe, refs.form, refs.run);
        card.refs = refs;
        refs.optionsKey = null;
        return card;
    }

    function updateStation(card, station) {
        const { refs } = card;
        refs.name.textContent = station.name;
        refs.recipe.textContent = station.recipe;

        // Rebuilt only when the choice changed, keeping what was picked.
        const optionsKey = JSON.stringify(station.orders);
        if (optionsKey !== refs.optionsKey) {
            refs.optionsKey = optionsKey;
            const picked = refs.order.value;
            const none = element('option', null, 'No order');
            none.value = '';
            refs.order.replaceChildren(none, ...station.orders.map((order) => {
                const option = element('option', null, order.label);
                option.value = String(order.id);
                return option;
            }));
            refs.order.value = station.orders.some((order) => String(order.id) === picked) ? picked : '';
        }

        refs.button.disabled = !station.canStart;
        refs.form.hidden = Boolean(station.run);
        refs.run.hidden = !station.run;
        if (station.run) {
            refs.runText.textContent = station.run.text;
            refs.progress.value = station.run.progress;
            refs.runStatus.textContent = station.run.status;
        }
    }

    function createShipment(shipment) {
        const item = element('li', 'shipment');
        const refs = {
            title: element('h3'),
            contents: element('p'),
            status: element('p', 'shipment-status'),
            button: element('button', null, 'Accept delivery'),
        };
        refs.button.type = 'button';
        refs.button.addEventListener('click', () => actions.accept(shipment.key));
        item.append(refs.title, refs.contents, refs.status, refs.button);
        item.refs = refs;
        return item;
    }

    function updateShipment(item, shipment) {
        const { refs } = item;
        item.dataset.here = shipment.here;
        refs.title.textContent = shipment.title;
        refs.contents.textContent = shipment.contents;
        refs.status.textContent = shipment.status;
        refs.button.hidden = !shipment.here;
        refs.button.disabled = !shipment.canAccept;
    }

    return function render(state) {
        const shown = describeWorld(state, formats);
        world.hidden = !shown;
        if (!shown) {
            return;
        }
        error.hidden = !shown.error;
        error.textContent = shown.error ?? '';
        keyed(stockRows, shown.stock, createStock, updateStock);
        keyed(stations, shown.stations, createStation, updateStation);
        keyed(shipments, shown.shipments, createShipment, updateShipment);
        noShipments.hidden = shown.shipments.length > 0;
    };
}
