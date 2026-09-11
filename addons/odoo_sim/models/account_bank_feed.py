# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The bank feed: Odoo's side of the game bank.

A bank integration in the shape of Odoo's own online synchronisation.  The
bank publishes each account's transactions (``game.bank.account._statement``);
the feed imports into the connected journal every transaction that journal
has not seen yet, as a bank statement line carrying the bank's identifier for
it.  The identifier is what makes the feed idempotent, as the online
transaction identifier is for a real bank.

It only ever writes to Odoo, and reads nothing there but which lines it has
already imported.  A statement line the player deletes comes back at the next
import; one they type in by hand is theirs, and the bank knows nothing of it.
Nothing here moves money.  See ``odoo_sim/GAME_STATE.md`` section 8.3.
"""
from odoo import api, fields, models


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    def _get_bank_statements_available_sources(self):
        return super()._get_bank_statements_available_sources() + [('game_bank', self.env._("Game bank"))]

    @api.model_create_multi
    def create(self, vals_list):
        """ Connect a company's first bank journal to its account at the game bank.

        The account may have opened before the company had any bank journal:
        the chart of accounts can arrive after the world does.
        """
        journals = super().create(vals_list)
        for journal in journals.filtered(lambda j: j.type == 'bank'):
            company = journal.company_id
            account = self.env['game.bank.account'].sudo().search([
                ('partner_id', '=', company.partner_id.id), ('journal_id', '=', False),
            ], limit=1)
            if account and (not journal.currency_id or journal.currency_id == account.currency_id):
                account._connect(journal)
        return journals


class AccountBankStatementLine(models.Model):
    _inherit = 'account.bank.statement.line'

    game_bank_ref = fields.Char(
        "Game bank transaction", readonly=True, copy=False,
        help="The game bank's identifier for the transaction this line was imported from.")

    _game_bank_ref_uniq = models.UniqueIndex('(journal_id, game_bank_ref) WHERE game_bank_ref IS NOT NULL')

    @api.model
    def _game_bank_trigger(self):
        """ Have the feed run as soon as the loop next polls. """
        cron = self.env.ref('odoo_sim.ir_cron_bank_feed', raise_if_not_found=False)
        if cron:
            cron._trigger()

    @api.model
    def _cron_game_bank_feed(self):
        for account in self.env['game.bank.account'].search([('journal_id', '!=', False)]):
            self._game_bank_import(account)

    @api.model
    def _game_bank_import(self, account):
        """ Import ``account``'s transactions its journal has not seen yet. Returns the new lines.

        Two imports racing would both insert the same line; the unique index
        refuses the second, and the cron runs again next tick.
        """
        journal = account.journal_id
        if not journal:
            return self
        transactions = account._statement().sorted(lambda t: (t.date, t.id))
        refs = {transaction: transaction._bank_ref() for transaction in transactions}
        seen = set(self.search([
            ('journal_id', '=', journal.id),
            ('game_bank_ref', 'in', list(refs.values())),
        ]).mapped('game_bank_ref'))
        values = [
            self._game_bank_values(account, transaction, ref)
            for transaction, ref in refs.items() if ref not in seen
        ]
        return self.with_company(journal.company_id).create(values) if values else self

    @api.model
    def _game_bank_values(self, account, transaction, ref):
        incoming = transaction.payee_id == account
        other = transaction.payer_id if incoming else transaction.payee_id
        return {
            'journal_id': account.journal_id.id,
            'date': fields.Date.to_date(transaction.date),
            'amount': transaction.amount if incoming else -transaction.amount,
            'payment_ref': transaction.reference or transaction.display_name,
            'partner_id': other.partner_id.id if other else False,
            'partner_name': other.partner_id.display_name if other else False,
            'account_number': other.number if other else False,
            'game_bank_ref': ref,
        }
