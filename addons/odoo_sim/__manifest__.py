{
    'name': "Odoo Sim",
    'version': '1.0',
    'category': 'Hidden',
    'summary': "Game world runtime: the loop that drives an accelerated Odoo",
    'description': """
Hosts the odoo-sim game loop.

The command it ships, ``odoo-bin game_run``, is discovered straight from the
addons path (``odoo/cli/command.py`` ``load_addons_commands``) and therefore
works whether or not this module is installed on the database.  Installing it
is only needed once the game grows models of its own.

See ``odoo_sim/DESIGN.md`` at the root of the repository.
    """,
    'depends': ['base', 'bus'],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
}
