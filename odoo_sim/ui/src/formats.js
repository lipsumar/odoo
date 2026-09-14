/**
 * How the page writes numbers, times and money: in the browser's own locale
 * and time zone, which is what the Odoo web client shows them in too, so a
 * record stamped at a game instant and the page showing that instant agree.
 */

/** `money(value, currency)` formats an amount in an ISO currency, one formatter per currency. */
export function moneyFormat(locale) {
    const formats = new Map();
    return (value, currency) => {
        if (!formats.has(currency)) {
            formats.set(currency, new Intl.NumberFormat(locale, { style: 'currency', currency }));
        }
        return formats.get(currency).format(value);
    };
}

/** The formats the world and the mail are shown in. */
export function pageFormats() {
    return {
        number: new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }),
        time: new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }),
        datetime: new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }),
        money: moneyFormat(undefined),
    };
}
