# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""The game bank: how much money each actor in the world holds, and every payment.

Odoo's bank journals are what the player *says* is in the bank.  These two
tables are what *is*, and only the game writes them: money moves when one
actor in the world pays another, and at no other time.  Nothing recorded in
Odoo -- a registered payment, an invoice marked paid, a statement line typed in
by hand -- moves a cent.  The company's balance is the score.

Odoo learns of the bank's transactions the way it would from a real bank,
through a feed (``account_bank_feed.py``).  See ``odoo_sim/GAME_STATE.md``
section 8.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL, format_amount

from odoo.addons.odoo_sim import utils

#: Why money moved.  A deposit brings money into the world from outside it --
#: a scenario's opening balances -- and has no payer.  A payment moves money
#: from one account to another, so the sum of all balances is always the sum
#: of all deposits.
KINDS = [
    ('deposit', "Deposit"),
    ('payment', "Payment"),
]


class GameBankAccount(models.Model):
    """An account at the game bank, held by one actor in the world.

    ``balance`` is a cache of the transactions' sum, kept for the same reason
    as ``game.stock``'s: ``CHECK (balance >= 0)`` on a single row is what makes
    "you cannot pay with money you do not have" hold under concurrency.
    """
    _name = 'game.bank.account'
    _description = "Game world: bank account"
    _order = 'id'
    _rec_name = 'number'

    number = fields.Char(
        "Account number", readonly=True, copy=False, index=True,
        help="How the bank names this account, to its holder and on the other side of a payment.")
    partner_id = fields.Many2one(
        'res.partner', "Holder", required=True, readonly=True, index=True, ondelete='restrict')
    currency_id = fields.Many2one(
        'res.currency', required=True, readonly=True, default=lambda self: self.env.company.currency_id)
    # Monetary is NUMERIC, so a thousand payments of 0.10 add up to exactly 100.
    balance = fields.Monetary(required=True, readonly=True, default=0.0)
    journal_id = fields.Many2one(
        'account.journal', "Feeds journal", readonly=True, ondelete='set null',
        help="The Odoo bank journal this account's transactions are imported into. The connection "
             "between the bank and Odoo, made by the game: nothing the player does in Odoo moves money.")

    _partner_uniq = models.Constraint(
        'UNIQUE(partner_id)',
        "An actor holds one account at the game bank.",
    )
    _number_uniq = models.Constraint(
        'UNIQUE(number)',
        "Two accounts cannot share a number.",
    )
    _balance_positive = models.Constraint(
        'CHECK(balance >= 0)',
        "The game bank lends nobody anything.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        accounts = super().create(vals_list)
        for account in accounts.filtered(lambda a: not a.number):
            account.number = f"GB{account.id:06d}"
        return accounts

    # -- opening accounts ----------------------------------------------------

    @api.model
    def _of(self, partner):
        """ The account ``partner`` holds, opened now if it has none. """
        partner.ensure_one()
        return self.search([('partner_id', '=', partner.id)], limit=1) or self.create({'partner_id': partner.id})

    @api.model
    def _company_account(self, company=None):
        """ The company's own account: the one the player plays for.

        Opened the first time it is asked for, in the company's currency, and
        connected to the company's first bank journal in that currency, so the
        feed has somewhere to import into.  A company may have no bank journal
        yet when its account opens -- a chart of accounts installed after the
        world is -- so an unconnected account is connected whenever it is asked
        for, and when a bank journal is created (``account_bank_feed.py``).
        """
        company = company or self.env.company
        account = self.search([('partner_id', '=', company.partner_id.id)], limit=1) or self.create({
            'partner_id': company.partner_id.id, 'currency_id': company.currency_id.id,
        })
        if not account.journal_id:
            journal = self.env['account.journal'].search([
                ('type', '=', 'bank'),
                ('company_id', '=', company.id),
                ('currency_id', 'in', (False, company.currency_id.id)),
            ], order='sequence, id', limit=1)
            if journal:
                account._connect(journal)
        return account

    def _connect(self, journal):
        """ Import this account's transactions into ``journal`` from now on. """
        self.ensure_one()
        self.journal_id = journal
        journal.bank_statements_source = 'game_bank'
        utils.trigger(self.env, 'odoo_sim.ir_cron_bank_feed')

    @api.model
    def _deposit(self, partner_id, amount, reference="Opening balance"):
        """ Bring ``amount`` into the world, into ``partner_id``'s account.  Takes an id, as scenario data passes one. """
        account = self._of(self.env['res.partner'].browse(partner_id))
        return self._book(self.browse(), account, amount, reference, 'deposit')

    def _pay(self, payee, amount, reference, *, date=None):
        """ Pay ``amount`` from this account to ``payee``, with ``reference`` as the communication. """
        self.ensure_one()
        return self._book(self, payee, amount, reference, 'payment', date=date)

    # -- the one write path ----------------------------------------------------

    @api.model
    def _book(self, payer, payee, amount, reference, kind, *, date=None):
        """ Move ``amount`` from ``payer`` to ``payee``, and record it.

        **The only way money moves.**  Both balances and the transaction are
        written in the caller's transaction and commit or roll back together,
        so the balances cannot disagree with the transactions.

        ``payer`` is empty for a deposit.  ``date`` is the game instant the
        payment happened, which is not always now: a payment due at 14:00 and
        settled by a cron at 14:07 happened at 14:00.

        Raises ``UserError`` rather than let a payer go below zero.
        """
        payee.ensure_one()
        currency = payee.currency_id
        if payer and payer.currency_id != currency:
            raise UserError(self.env._(
                "%(payer)s holds %(payer_currency)s and %(payee)s holds %(payee_currency)s: "
                "the game bank does not change money.",
                payer=payer.number, payer_currency=payer.currency_id.name,
                payee=payee.number, payee_currency=currency.name,
            ))
        amount = currency.round(amount)
        if currency.compare_amounts(amount, 0) <= 0:
            raise UserError(self.env._("A payment is for a positive amount."))

        now = fields.Datetime.now()
        self.flush_model()
        # Lock both rows in id order first, so that two payments crossing
        # between the same two accounts wait for each other instead of
        # deadlocking.
        self.env.cr.execute(SQL(
            "SELECT id FROM game_bank_account WHERE id IN %s ORDER BY id FOR UPDATE",
            tuple((payer | payee).ids),
        ))
        values = {'amount': amount, 'uid': self.env.uid, 'now': now}
        if payer:
            # The WHERE clause is the rule: no row changes if the payer does
            # not have the money, and the CHECK constraint backs it up.
            self.env.cr.execute(SQL("""
                UPDATE game_bank_account
                   SET balance = balance - %(amount)s, write_uid = %(uid)s, write_date = %(now)s
                 WHERE id = %(account)s AND balance >= %(amount)s
            """, account=payer.id, **values))
            if not self.env.cr.rowcount:
                self.invalidate_model(['balance'])
                raise UserError(self.env._(
                    "%(account)s cannot pay %(amount)s: there is only %(balance)s in it.",
                    account=f"{payer.partner_id.display_name} ({payer.number})",
                    amount=format_amount(self.env, amount, currency),
                    balance=format_amount(self.env, payer.balance, currency),
                ))
        self.env.cr.execute(SQL("""
            UPDATE game_bank_account
               SET balance = balance + %(amount)s, write_uid = %(uid)s, write_date = %(now)s
             WHERE id = %(account)s
        """, account=payee.id, **values))
        self.invalidate_model(['balance', 'write_uid', 'write_date'])

        transaction = self.env['game.bank.transaction'].create({
            'date': date or now,
            'kind': kind,
            'payer_id': payer.id if payer else False,
            'payee_id': payee.id,
            'amount': amount,
            'currency_id': currency.id,
            'reference': reference,
        })
        if (payer | payee).journal_id:
            utils.trigger(self.env, 'odoo_sim.ir_cron_bank_feed')
        self.env['game.world']._changed()
        return transaction

    # -- what the bank tells its customers -------------------------------------

    def _statement(self, limit=None):
        """ This account's transactions, newest first, as the bank shows them. """
        self.ensure_one()
        return self.env['game.bank.transaction'].search(
            ['|', ('payer_id', '=', self.id), ('payee_id', '=', self.id)], limit=limit)


class GameBankTransaction(models.Model):
    """One movement of money.  Never edited, never deleted."""
    _name = 'game.bank.transaction'
    _description = "Game world: bank transaction"
    _order = 'date desc, id desc'

    date = fields.Datetime(required=True, readonly=True, index=True, default=fields.Datetime.now)
    kind = fields.Selection(KINDS, required=True, readonly=True)
    payer_id = fields.Many2one(
        'game.bank.account', "From", readonly=True, index='btree_not_null', ondelete='restrict')
    payee_id = fields.Many2one('game.bank.account', "To", required=True, readonly=True, index=True, ondelete='restrict')
    amount = fields.Monetary(required=True, readonly=True)
    currency_id = fields.Many2one('res.currency', required=True, readonly=True)
    reference = fields.Char(
        "Communication", readonly=True,
        help="What the payer wrote on the payment: for an invoice, its payment reference.")

    _amount_positive = models.Constraint('CHECK(amount > 0)', "A payment is for a positive amount.")
    _deposit_has_no_payer = models.Constraint(
        "CHECK((payer_id IS NULL) = (kind = 'deposit'))",
        "Money comes from another account, unless it is a deposit into the world.",
    )
    _not_to_itself = models.Constraint(
        'CHECK(payer_id IS NULL OR payer_id != payee_id)',
        "An account does not pay itself.",
    )

    def _compute_display_name(self):
        for transaction in self:
            transaction.display_name = transaction.reference or self.env._("Transaction %s", transaction.id)

    def _bank_ref(self):
        """ The bank's own identifier for this transaction, as a feed sees it. """
        self.ensure_one()
        return f"GBT{self.id:08d}"
