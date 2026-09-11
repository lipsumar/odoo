{
    'name': "Odoo Sim: Paperclips",
    'version': '1.0',
    'category': 'Hidden',
    'summary': "Game scenario: a company that makes paperclips from wire it buys",
    'description': """
The first odoo-sim scenario.

The company manufactures paperclips at a single workstation, from wire it buys
in 50 m spools from one vendor. A paperclip takes 10 cm of wire.

Installs both halves of the scenario: the Odoo records the player starts from
(products, bill of materials, work center, vendor pricelist), and the world's
own truth behind them (the real recipe, the workstation, the vendor agent).
They agree at the start; keeping them agreeing is the game.

See ``odoo_sim/GAME_STATE.md`` section 10.
    """,
    'depends': ['odoo_sim'],
    'data': ['data/scenario.xml'],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
}
