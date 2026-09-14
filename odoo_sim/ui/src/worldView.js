/**
 * The world on the page: the company's money, what exists, what the
 * workstations are doing, who works there, what customers want, the packages
 * on the bench and sent, and what is at the door.  See GAME_STATE.md 7, 8 and
 * 10, and EMPLOYEES.md.
 *
 * As with the clock, `describeWorld` is the whole of the decision and touches
 * no DOM; `createWorldView` writes its answer into elements it keeps, rather
 * than rebuilding them, because it is re-run on every pulse and a rebuilt
 * input loses what the player was typing into it.
 */
import { element, keyed } from './dom.js';
import { pageFormats } from './formats.js';
import { parseInstant } from './reading.js';

/** Odoo's counting unit, left out of quantities: "10 Paperclip", not "10 Units Paperclip". */
const COUNTING_UNIT = 'Units';

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
        const making = `${goods({ ...station.product, qty: run.qty }, formats)}`
            + (run.order ? ` for ${run.order.name}` : '');
        shown.run = {
            text: run.worker ? `${run.worker} is making ${making}` : `Making ${making}`,
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
    let status = here ? 'At the door' : `Arrives ${formats.datetime.format(arrival)}`;
    if (shipment.worker) {
        status = `${shipment.worker} is unpacking it`;
    }
    return {
        key: shipment.id,
        title: shipment.order ? `${shipment.order} from ${shipment.vendor}` : `From ${shipment.vendor}`,
        contents: shipment.lines.map((line) => goods(line, formats)).join(', '),
        here,
        status,
        unpacking: Boolean(shipment.worker),
        canAccept: acting && here && !pending && !shipment.worker,
    };
}

/** The company's account: the score, and the last few movements on it. */
function describeBank(bank, formats) {
    if (!bank) {
        return null;
    }
    return {
        balance: formats.money(bank.balance, bank.currency),
        number: `Account ${bank.number}`,
        transactions: bank.transactions.map((transaction) => {
            const incoming = transaction.amount > 0;
            const sign = incoming ? '+' : '−';
            return {
                key: transaction.id,
                incoming,
                amount: `${sign}${formats.money(Math.abs(transaction.amount), bank.currency)}`,
                text: [transaction.counterparty ?? 'Deposit', transaction.reference]
                    .filter(Boolean).join(' · '),
                date: formats.datetime.format(parseInstant(transaction.date)),
            };
        }),
    };
}

function describeOrder(order, now, formats) {
    const money = (value) => formats.money(value, order.currency);
    const { invoice } = order;
    const due = invoice?.date_due ? parseInstant(invoice.date_due) : null;
    // Due by the clock, and not yet settled: the payment lands within a tick.
    const payingNow = order.state === 'invoiced' && due !== null && now !== null && now >= due;

    let status;
    let tone = 'wait';
    if (order.state === 'requested' && invoice?.state === 'refused') {
        status = `Refused ${invoice.name}: ${invoice.reason}`;
        tone = 'fault';
    } else if (order.state === 'requested') {
        status = 'Waiting for your invoice';
    } else if (payingNow) {
        status = `Paying ${invoice.name}…`;
        tone = 'ready';
    } else if (order.state === 'invoiced') {
        status = `Accepted ${invoice.name} for ${money(invoice.amount)}; pays ${formats.datetime.format(due)}`;
    } else {
        status = `Paid ${money(order.amount_paid)} for ${goods({ ...order.product, qty: order.qty_paid }, formats)}`;
        tone = 'ready';
    }
    return {
        key: order.id,
        title: `${order.customer} wants ${goods({ ...order.product, qty: order.qty }, formats)}`,
        terms: `Pays up to ${money(order.max_price)} each, taxes included`,
        status,
        tone,
    };
}

/**
 * The packing bench, and what the company has sent.  A sent package says what
 * went where and when, and nothing more: the post has no tracking, and a
 * customer still waiting says so by email.
 */
function describePost(world, acting, pending, formats) {
    const products = world.stock.filter((line) => line.qty > 0).map((line) => ({
        id: line.id,
        label: `${line.name} (${amount(line.qty, line.uom, formats)})`,
    }));
    const contents = (box) => box.lines.map((line) => goods(line, formats)).join(', ');
    return {
        canTake: acting && !pending.has('package:new'),
        bench: (world.packages ?? []).map((box) => {
            // A box an employee is packing is theirs until it goes to the post.
            const free = acting && !pending.has(`package:${box.id}`) && !box.worker;
            return {
                key: box.id,
                title: box.name,
                worker: box.worker ? `${box.worker} is packing it` : null,
                contents: contents(box) || 'Empty',
                returned: box.returned
                    ? `Returned ${formats.datetime.format(parseInstant(box.date_returned))}: ${box.returned}`
                    : null,
                address: box.address ?? '',
                products,
                canPack: free && products.length > 0,
                canSend: free && box.lines.length > 0,
                canUnpack: free,
            };
        }),
        sent: (world.sent_packages ?? []).map((box) => ({
            key: box.id,
            title: `${box.name}: ${contents(box)}`,
            to: (box.address ?? '').split('\n').join(', '),
            date: `Sent ${formats.datetime.format(parseInstant(box.date_posted))}`,
        })),
    };
}

/**
 * One employee: what they are doing, whether they are at work at all, and
 * whether they have been paid.  The snapshot says when their working day
 * starts and ends as of when it was taken, and the clock moves on from there.
 */
function describeEmployee(employee, now, formats) {
    const money = (value) => formats.money(value, employee.currency);
    const start = parseInstant(employee.shift_start);
    const end = parseInstant(employee.shift_end);
    const { task } = employee;
    const taskEnd = task ? parseInstant(task.date_end) : null;

    let status;
    let tone = 'ready';
    if (now !== null && now < start) {
        status = task ? `Carries on at ${formats.time.format(start)}` : `Off until ${formats.time.format(start)}`;
        tone = 'wait';
    } else if (now !== null && now >= end && !(taskEnd !== null && now >= taskEnd)) {
        status = 'Gone home';
        tone = 'wait';
    } else if (task) {
        status = now !== null && now >= taskEnd ? 'Finishing…' : `Done ${formats.datetime.format(taskEnd)}`;
    } else {
        status = 'Waiting for work';
        tone = 'wait';
    }

    const due = employee.salary_due ? parseInstant(employee.salary_due) : null;
    const unpaid = due !== null && now !== null && now >= due && employee.date_leave;
    return {
        key: employee.id,
        name: employee.name,
        job: `${employee.job}, ${money(employee.wage)} a month`,
        doing: task ? task.text : 'Nothing to do',
        status,
        tone,
        pay: unpaid
            ? `Not paid since ${formats.datetime.format(due)}: leaves ${formats.datetime.format(parseInstant(employee.date_leave))}`
            : null,
    };
}

/** Who works for the company, and the jobs it can hire for. */
function describeStaff(world, now, acting, pending, formats) {
    return {
        jobs: (world.jobs ?? []).map((job) => ({
            key: job.id,
            label: `Hire: ${job.name}, ${formats.money(job.wage, job.currency)} a month`,
            canHire: acting && !pending.has(`job:${job.id}`),
        })),
        employees: (world.employees ?? []).map((employee) => describeEmployee(employee, now, formats)),
    };
}

/**
 * Describe `{ world, reading, pending, error }` as what to show.
 *
 * `reading` is the clock (`readClock`), used for progress and for whether the
 * world is running at all: nothing can be done in a paused or stopped world,
 * and the server refuses it anyway.  `pending` holds the keys of actions in
 * flight (`station:<id>`, `shipment:<id>`, `package:<id>`, `package:new`, `job:<id>`),
 * whose buttons wait.
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
        bank: describeBank(world.bank ?? null, formats),
        orders: (world.customer_orders ?? []).map((order) => describeOrder(order, now, formats)),
        post: describePost(world, acting, pending, formats),
        staff: describeStaff(world, now, acting, pending, formats),
    };
}

function panel(title, ...children) {
    const section = element('section', 'panel');
    section.append(element('h2', null, title), ...children);
    return section;
}

/**
 * Build the world inside `root`, and return `render(state)` to update it.
 *
 * `actions.start(stationId, { qty, productionId })`, `actions.accept(shipmentId)`,
 * `actions.newPackage()`, `actions.pack(packageId, { productId, qty })`,
 * `actions.unpack(packageId)`, `actions.post(packageId, address)` and
 * `actions.hire(jobId)` are called when the player presses a button.
 */
export function createWorldView(root, actions, formats = pageFormats()) {
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

    const balance = element('p', 'balance');
    const accountNumber = element('p', 'account');
    const transactions = element('ul', 'transactions');
    const noTransactions = element('p', 'empty', 'No money has moved yet.');
    const bank = panel('Bank', balance, accountNumber, transactions, noTransactions);

    const orders = element('ul', 'orders');
    const noOrders = element('p', 'empty', 'Nobody is asking for anything.');

    const takeBox = element('button', null, 'New package');
    takeBox.type = 'button';
    takeBox.addEventListener('click', () => actions.newPackage());
    const bench = element('ul', 'packages');
    const emptyBench = element('p', 'empty', 'No package on the bench.');
    const sentTitle = element('h3', 'sent-title', 'Sent');
    const sent = element('ul', 'sent');

    const jobs = element('div', 'jobs');
    const employees = element('ul', 'employees');
    const noEmployees = element('p', 'empty', 'Nobody works here yet.');
    const staff = panel('Employees', jobs, employees, noEmployees);

    const world = element('div', 'world');
    world.append(
        error,
        bank,
        panel('On hand', stockTable),
        panel('Workstations', stations),
        staff,
        panel('Customer orders', orders, noOrders),
        panel('Post', takeBox, bench, emptyBench, sentTitle, sent),
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
        refs.button.hidden = !shipment.here || shipment.unpacking;
        refs.button.disabled = !shipment.canAccept;
    }

    function createTransaction() {
        const item = element('li', 'transaction');
        item.append(element('span', 'amount'), element('span', 'what'), element('span', 'when'));
        return item;
    }

    function updateTransaction(item, transaction) {
        item.dataset.incoming = transaction.incoming;
        item.children[0].textContent = transaction.amount;
        item.children[1].textContent = transaction.text;
        item.children[2].textContent = transaction.date;
    }

    function createOrder() {
        const item = element('li', 'order');
        item.append(element('h3'), element('p', 'order-terms'), element('p', 'order-status'));
        return item;
    }

    function updateOrder(item, order) {
        item.dataset.tone = order.tone;
        item.children[0].textContent = order.title;
        item.children[1].textContent = order.terms;
        item.children[2].textContent = order.status;
    }

    function createPackage(box) {
        const item = element('li', 'package');
        const refs = {
            title: element('h3'),
            worker: element('p', 'package-worker'),
            returned: element('p', 'package-returned'),
            contents: element('p'),
            form: element('form', 'package-form'),
            product: element('select'),
            qty: element('input'),
            put: element('button', null, 'Put in'),
            address: element('textarea'),
            send: element('button', null, 'Send'),
            unpack: element('button', 'secondary', 'Unpack'),
            optionsKey: null,
            canSend: false,
        };
        refs.qty.type = 'number';
        refs.qty.min = '0';
        refs.qty.step = 'any';
        refs.qty.value = '1';
        refs.qty.required = true;
        refs.put.type = 'submit';
        refs.send.type = 'button';
        refs.unpack.type = 'button';
        refs.address.rows = 4;
        refs.address.placeholder = 'Name\nStreet\nTown and postcode\nCountry';
        // Filled once, from what was last written on the box: from then on it
        // holds what the player types, which a render must not undo.
        refs.address.value = box.address;

        const productLabel = element('label', null, 'Product ');
        productLabel.append(refs.product);
        const qtyLabel = element('label', null, 'Quantity ');
        qtyLabel.append(refs.qty);
        refs.form.append(productLabel, qtyLabel, refs.put);
        refs.form.addEventListener('submit', (event) => {
            event.preventDefault();
            actions.pack(box.key, { productId: Number(refs.product.value), qty: Number(refs.qty.value) });
        });

        const addressLabel = element('label', 'package-address', 'Address');
        addressLabel.append(refs.address);
        refs.address.addEventListener('input', () => updateSend(refs));
        refs.send.addEventListener('click', () => actions.post(box.key, refs.address.value));
        refs.unpack.addEventListener('click', () => actions.unpack(box.key));
        const buttons = element('div', 'package-actions');
        buttons.append(refs.send, refs.unpack);

        item.append(refs.title, refs.worker, refs.returned, refs.contents, refs.form, addressLabel, buttons);
        item.refs = refs;
        return item;
    }

    // Sending needs something in the box, and an address, which only the
    // textarea knows.
    function updateSend(refs) {
        refs.send.disabled = !(refs.canSend && refs.address.value.trim());
    }

    function updatePackage(item, box) {
        const { refs } = item;
        refs.title.textContent = box.title;
        refs.worker.hidden = !box.worker;
        refs.worker.textContent = box.worker ?? '';
        refs.returned.hidden = !box.returned;
        refs.returned.textContent = box.returned ?? '';
        refs.contents.textContent = box.contents;

        // Rebuilt only when the choice changed, keeping what was picked.
        const optionsKey = JSON.stringify(box.products);
        if (optionsKey !== refs.optionsKey) {
            refs.optionsKey = optionsKey;
            const picked = refs.product.value;
            refs.product.replaceChildren(...box.products.map((product) => {
                const option = element('option', null, product.label);
                option.value = String(product.id);
                return option;
            }));
            if (box.products.some((product) => String(product.id) === picked)) {
                refs.product.value = picked;
            }
        }

        refs.put.disabled = !box.canPack;
        refs.unpack.disabled = !box.canUnpack;
        refs.canSend = box.canSend;
        updateSend(refs);
    }

    function createJob(job) {
        const button = element('button');
        button.type = 'button';
        button.addEventListener('click', () => actions.hire(job.key));
        return button;
    }

    function updateJob(button, job) {
        button.textContent = job.label;
        button.disabled = !job.canHire;
    }

    function createEmployee() {
        const item = element('li', 'employee');
        item.append(
            element('h3'),
            element('p', 'employee-job'),
            element('p', 'employee-doing'),
            element('p', 'employee-status'),
            element('p', 'employee-pay'),
        );
        return item;
    }

    function updateEmployee(item, employee) {
        item.dataset.tone = employee.tone;
        const [name, job, doing, status, pay] = item.children;
        name.textContent = employee.name;
        job.textContent = employee.job;
        doing.textContent = employee.doing;
        status.textContent = employee.status;
        pay.hidden = !employee.pay;
        pay.textContent = employee.pay ?? '';
    }

    function createSent() {
        const item = element('li', 'sent-package');
        item.append(element('p', 'sent-what'), element('p', 'sent-to'), element('p', 'sent-when'));
        return item;
    }

    function updateSent(item, box) {
        item.children[0].textContent = box.title;
        item.children[1].textContent = box.to;
        item.children[2].textContent = box.date;
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
        keyed(orders, shown.orders, createOrder, updateOrder);
        noOrders.hidden = shown.orders.length > 0;
        takeBox.disabled = !shown.post.canTake;
        keyed(bench, shown.post.bench, createPackage, updatePackage);
        emptyBench.hidden = shown.post.bench.length > 0;
        keyed(sent, shown.post.sent, createSent, updateSent);
        sentTitle.hidden = shown.post.sent.length === 0;
        staff.hidden = shown.staff.jobs.length === 0 && shown.staff.employees.length === 0;
        keyed(jobs, shown.staff.jobs, createJob, updateJob);
        keyed(employees, shown.staff.employees, createEmployee, updateEmployee);
        noEmployees.hidden = shown.staff.employees.length > 0;
        bank.hidden = !shown.bank;
        if (shown.bank) {
            balance.textContent = shown.bank.balance;
            accountNumber.textContent = shown.bank.number;
            keyed(transactions, shown.bank.transactions, createTransaction, updateTransaction);
            noTransactions.hidden = shown.bank.transactions.length > 0;
        }
    };
}
