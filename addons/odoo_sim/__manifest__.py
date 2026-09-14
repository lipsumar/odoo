{
    'name': "Odoo Sim",
    'version': '1.4',
    'category': 'Hidden',
    'summary': "Game world runtime: the loop that drives an accelerated Odoo, and the world it drives",
    'description': """
Hosts the odoo-sim game loop, and the world it drives.

The commands it ships -- ``odoo-bin sim_init``, ``game_run`` and ``sim_pause``
-- are discovered straight from the addons path (``odoo/cli/command.py``
``load_addons_commands``) and therefore work whether or not this module is
installed on the database.

The **UI is the exception**: routes and templates come from the registry, so
``/game`` and its API exist only once this module is installed.

It also holds the game world's own state -- what physically exists, the money
in the game bank, its mail and its employees, as opposed to what Odoo records
-- in ``game.*`` models that only the game writes.

See ``odoo_sim/README.md`` at the root of the repository for how to run a
world, and the design documents beside it.
    """,
    'depends': ['base', 'bus', 'mrp', 'purchase_stock', 'sale_stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/index.xml',
        'views/game_debug_views.xml',
    ],
    'post_init_hook': '_genesis',
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
}
