/**
 * Keep the page's copy of the world current.
 *
 * The world arrives three ways: in the bootstrap blob, from
 * `GET /game/api/world` whenever the bus says it changed, and as the answer to
 * every action the player takes.  They can land out of order -- a fetch sent
 * before a button press can answer after it -- so each request takes a ticket
 * when it is sent, and an answer is only applied if nothing sent later has
 * been applied already.  See GAME_STATE.md 8.
 *
 * The bus notice carries nothing on purpose (any socket may subscribe to the
 * channel), so a fetch is the only way the page learns *what* changed.
 */

/**
 * Read a response from one of the world's endpoints, or throw an `Error`
 * whose message is the server's reason -- a `UserError` arrives as a 422
 * whose body carries the sentence the player should see.
 */
export async function readWorld(response) {
    let body = null;
    try {
        body = await response.json();
    } catch {
        // not JSON: say what we can from the status alone
    }
    if (!response.ok) {
        const error = new Error(body?.message ?? `The server answered ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return body;
}

/**
 * Returns `{ refresh, act }`.
 *
 * - `refresh()` fetches the world again.  Calls made while one is in flight
 *   collapse into a single follow-up, so a burst of notices costs two fetches
 *   rather than one per notice.
 * - `act(request)` sends an action (a function returning a `fetch` promise)
 *   and applies the world it answers with.  It rejects with the server's
 *   reason, for the caller to show.
 */
export function syncWorld({ load, onWorld }) {
    let issued = 0;
    let applied = 0;
    let loading = false;
    let again = false;

    async function run(request) {
        const ticket = ++issued;
        const world = await readWorld(await request());
        if (ticket > applied) {
            applied = ticket;
            onWorld(world);
        }
    }

    function refresh() {
        if (loading) {
            again = true;
            return;
        }
        loading = true;
        run(load)
            .catch((error) => console.warn('odoo_sim: could not fetch the world', error))
            .finally(() => {
                loading = false;
                if (again) {
                    again = false;
                    refresh();
                }
            });
    }

    return { refresh, act: run };
}
