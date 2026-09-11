{
    'name': "Odoo Sim",
    'version': '1.1',
    'category': 'Hidden',
    'summary': "Game world runtime: the loop that drives an accelerated Odoo, and the world it drives",
    'description': """
Hosts the odoo-sim game loop.

The command it ships, ``odoo-bin game_run``, is discovered straight from the
addons path (``odoo/cli/command.py`` ``load_addons_commands``) and therefore
works whether or not this module is installed on the database.

The **UI is the exception**: routes and templates come from the registry, so
``/game`` and ``/game/api/clock`` exist only once this module is installed.

It also holds the game world's own state -- what physically exists, as opposed
to what Odoo records -- in ``game.*`` models that only the game writes.

See ``odoo_sim/DESIGN.md``, ``odoo_sim/UI_DESIGN.md`` and
``odoo_sim/GAME_STATE.md`` at the root of the repository.
    """,
    'depends': ['base', 'bus', 'mrp', 'purchase_stock'],
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
