import { defineConfig } from 'vite';

// The server `npm run dev` forwards the API and the bus to.
const odoo = process.env.ODOO_URL ?? 'http://localhost:8069';

export default defineConfig(({ command }) => ({
    // Where Odoo serves the build from, straight off disk (UI_DESIGN.md 2.1).
    // The dev server serves from its own root.
    base: command === 'build' ? '/odoo_sim/static/dist/' : '/',
    build: {
        // Under static/dist/ and never static/src/: Odoo's JS transpiler
        // rewrites anything under src/ into an Odoo module (UI_DESIGN.md 6.3).
        outDir: '../../addons/odoo_sim/static/dist',
        // It is outside this project, so Vite will not clear it unasked.
        emptyOutDir: true,
        // controllers/main.py reads .vite/manifest.json to find the hashed
        // entry, keyed by the path below.  Filenames stay content-hashed
        // (Vite's default): Odoo caches static files for a week.
        manifest: true,
        rolldownOptions: {
            input: 'src/main.js',
        },
    },
    server: {
        port: 5173,
        strictPort: true,
        proxy: {
            '/game/api': odoo,
            '/websocket': { target: odoo, ws: true },
        },
    },
}));
