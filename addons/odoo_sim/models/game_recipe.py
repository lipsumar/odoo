# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""How things are really made: the world's physics, not the player's BoM.

A bill of materials is the player's *model* of a recipe, and can be wrong.
The recipe below is what a workstation actually consumes, whatever the BoM
says.  When the two disagree, marking a manufacturing order done consumes the
wrong components in Odoo, and Odoo drifts from reality -- which is the player's
problem to notice.  See ``odoo_sim/GAME_STATE.md`` section 4.
"""
from odoo import fields, models


class GameRecipe(models.Model):
    _name = 'game.recipe'
    _description = "Game world: recipe"
    _rec_name = 'product_id'

    product_id = fields.Many2one(
        'product.product', "Makes", required=True, index=True, ondelete='restrict',
        help="One unit of this, in its own unit of measure, per unit of the run.")
    duration = fields.Float(
        "Duration per unit (game minutes)", required=True, default=1.0,
        help="Game time a workstation takes to make one unit.")
    line_ids = fields.One2many('game.recipe.line', 'recipe_id', "Consumes")

    _product_uniq = models.Constraint(
        'UNIQUE(product_id)',
        "A product is made one way in this world.",
    )
    _duration_positive = models.Constraint(
        'CHECK(duration >= 0)',
        "Making something cannot take negative time.",
    )


class GameRecipeLine(models.Model):
    _name = 'game.recipe.line'
    _description = "Game world: recipe component"
    _rec_name = 'product_id'

    recipe_id = fields.Many2one('game.recipe', required=True, index=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', required=True, ondelete='restrict')
    qty = fields.Float(
        "Quantity per unit", digits='Product Unit', required=True,
        help="In the component's own unit of measure.")
    uom_id = fields.Many2one(related='product_id.uom_id')

    _qty_positive = models.Constraint(
        'CHECK(qty > 0)',
        "A component is consumed in a positive quantity.",
    )
