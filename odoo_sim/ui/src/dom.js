/**
 * The two DOM helpers every view builds with.  Views keep the elements they
 * make and update them in place, rather than rebuilding them, because they are
 * re-run on every pulse and a rebuilt input loses what the player was typing.
 */

export function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
        node.className = className;
    }
    if (text !== undefined) {
        node.textContent = text;
    }
    return node;
}

/**
 * Make `container`'s children match `items`, keyed by `item.key`: existing
 * nodes are updated in place and only moved when the list itself changed, so
 * a focused input or a half-pressed button survives a render.
 */
export function keyed(container, items, create, update) {
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
