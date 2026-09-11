# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import models


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    def button_approve(self, force=False):
        """ Let the vendors know there are orders to read.

        ``button_approve`` rather than ``button_confirm``: under double
        validation a confirmed order goes to ``to approve`` and only becomes a
        purchase order here, which is also where ``purchase_stock`` creates the
        receipt.  The vendor agent re-reads the orders itself, so this only
        wakes it and says nothing about which.
        """
        result = super().button_approve(force=force)
        cron = self.env.ref('odoo_sim.ir_cron_vendor_agent', raise_if_not_found=False)
        if cron and self.filtered(lambda order: order.state == 'purchase'):
            cron._trigger()
        return result
