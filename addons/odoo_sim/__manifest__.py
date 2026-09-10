{
    'name': "Odoo Sim",
    'version': '1.0',
    'category': 'Hidden',
    'summary': "Game world runtime: the loop that drives an accelerated Odoo",
    'description': """
Hosts the odoo-sim game loop.

The command it ships, ``odoo-bin game_run``, is discovered straight from the
addons path (``odoo/cli/command.py`` ``load_addons_commands``) and therefore
works whether or not this module is installed on the database.

The **UI is the exception**: routes and templates come from the registry, so
``/game`` and ``/game/api/clock`` exist only once this module is installed.

See ``odoo_sim/DESIGN.md`` and ``odoo_sim/UI_DESIGN.md`` at the root of the
repository.
    """,
    'depends': ['base', 'bus'],
    'data': ['views/index.xml'],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
}
