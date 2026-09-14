# Part of Odoo. See LICENSE file for full copyright and licensing details.
"""Tests for the game bank and the feed that imports it into Odoo (GAME_STATE.md 8).

Balances are compared as movements, never as totals: the company's account
exists before any test runs, and on a world someone has been playing in it
already holds whatever the player earned (DESIGN.md 8).
"""
from odoo import Command
from odoo.exceptions import AccessError

from odoo.addons.odoo_sim.tests.common import BankCase


class TestBank(BankCase):

    def test_a_deposit_brings_money_into_the_world(self):
        transaction = self.accounts._deposit(self.buyer.id, 100)

        self.assertEqual(self.balance(self.buyer_account), 100)
        self.assertEqual((transaction.kind, transaction.payer_id, transaction.payee_id),
                         ('deposit', self.accounts, self.buyer_account))

    def test_a_payment_moves_money_and_records_it(self):
        self.accounts._deposit(self.buyer.id, 100)
        transaction = self.buyer_account._pay(self.company_account, 28.75, "INV/2030/00001")

        self.assertEqual(self.balance(self.buyer_account), 71.25)
        self.assertEqual(self.earned(), 28.75)
        self.assertEqual(
            (transaction.kind, transaction.payer_id, transaction.payee_id, transaction.amount, transaction.reference),
            ('payment', self.buyer_account, self.company_account, 28.75, "INV/2030/00001"),
        )

    def test_nobody_pays_with_money_they_do_not_have(self):
        self.accounts._deposit(self.buyer.id, 10)
        with self.refused(r"cannot pay .*10\.01.*only .*10\.00"):
            self.buyer_account._pay(self.company_account, 10.01, "too much")

        self.assertEqual(self.balance(self.buyer_account), 10)
        self.assertEqual(self.earned(), 0)
        self.assertEqual(len(self.transactions(self.buyer_account)), 1, "no record of what did not happen")

    def test_the_last_cent_can_be_spent(self):
        """ NUMERIC, not float8, exactly as for the stock ledger. """
        self.accounts._deposit(self.buyer.id, 0.3)
        for _ in range(3):
            self.buyer_account._pay(self.company_account, 0.1, "a dime")

        self.assertEqual(self.balance(self.buyer_account), 0)
        with self.refused("cannot pay"):
            self.buyer_account._pay(self.company_account, 0.1, "one dime too many")

    def test_a_payment_is_for_a_positive_amount(self):
        self.accounts._deposit(self.buyer.id, 10)
        for amount in (0, -5, 0.001):
            with self.subTest(amount=amount), self.refused("positive amount"):
                self.buyer_account._pay(self.company_account, amount, "nothing")

    def test_the_bank_does_not_change_money(self):
        other = self.env['res.currency'].with_context(active_test=False).search(
            [('id', '!=', self.company_account.currency_id.id)], limit=1)
        stranger = self.accounts.create({
            'partner_id': self.env['res.partner'].create({'name': "Abroad"}).id, 'currency_id': other.id,
        })
        self.accounts._deposit(stranger.partner_id.id, 10)
        with self.refused("does not change money"):
            stranger._pay(self.company_account, 5, "foreign")

    def test_every_actor_gets_an_account_number(self):
        self.assertRegex(self.buyer_account.number, r'^GB\d{6}$')
        self.assertEqual(self.accounts._of(self.buyer), self.buyer_account, "one account each")

    def test_the_company_account_is_opened_once_and_fed_into_a_bank_journal(self):
        self.assertEqual(self.accounts._company_account(), self.company_account)
        journal = self.company_account.journal_id
        self.assertEqual(journal.type, 'bank')
        self.assertEqual(journal.bank_statements_source, 'game_bank')

    def test_nobody_can_write_money_through_the_orm(self):
        """ Not even an administrator: money moves through the game only. """
        admin = self.env.ref('base.user_admin')
        for model in ('game.bank.account', 'game.bank.transaction'):
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(admin).check_access('write')
        with self.assertRaises(AccessError):
            self.buyer_account.with_user(admin).write({'balance': 1_000_000})

    def test_registering_a_payment_in_odoo_moves_no_money(self):
        """ Odoo can say the buyer paid. The bank has not seen a cent. """
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.buyer.id,
            'invoice_line_ids': [Command.create({'product_id': self.clip.id, 'quantity': 10, 'price_unit': 5})],
        })
        invoice.action_post()
        self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({})._create_payments()

        self.assertIn(invoice.payment_state, ('paid', 'in_payment'), "Odoo believes it")
        self.assertEqual(self.earned(), 0, "the bank does not")


class TestBankFeed(BankCase):

    def setUp(self):
        super().setUp()
        self.journal = self.company_account.journal_id
        # Whatever a played world has not imported yet is not this test's.
        self.lines._game_bank_import(self.company_account)

    def imported(self):
        return self.lines.search([('journal_id', '=', self.journal.id), ('game_bank_ref', '!=', False)])

    def test_a_payment_is_imported_as_the_bank_describes_it(self):
        self.accounts._deposit(self.buyer.id, 100)
        transaction = self.buyer_account._pay(self.company_account, 28.75, "INV/2030/00001")
        line = self.lines._game_bank_import(self.company_account)

        self.assertEqual(len(line), 1, "the buyer's deposit is not the company's business")
        self.assertEqual(
            (line.journal_id, line.amount, line.payment_ref, line.date, line.game_bank_ref),
            (self.journal, 28.75, "INV/2030/00001", transaction.date.date(), transaction._bank_ref()),
        )
        self.assertEqual((line.partner_id, line.partner_name, line.account_number),
                         (self.buyer, "Test buyer", self.buyer_account.number))

    def test_money_going_out_is_imported_as_negative(self):
        self.accounts._deposit(self.buyer.id, 100)
        self.buyer_account._pay(self.company_account, 10, "in")
        self.company_account._pay(self.buyer_account, 4, "refund")
        lines = self.lines._game_bank_import(self.company_account)

        self.assertEqual(sorted(lines.mapped('amount')), [-4, 10])

    def test_a_transaction_is_imported_once(self):
        self.accounts._deposit(self.buyer.id, 100)
        self.buyer_account._pay(self.company_account, 10, "once")
        self.lines._game_bank_import(self.company_account)

        self.assertFalse(self.lines._game_bank_import(self.company_account))
        self.assertEqual(len(self.imported().filtered(lambda l: l.payment_ref == "once")), 1)

    def test_a_deleted_line_is_imported_again(self):
        """ The bank still has the transaction; the feed restores what Odoo lost. """
        self.accounts._deposit(self.buyer.id, 100)
        self.buyer_account._pay(self.company_account, 10, "again")
        self.lines._game_bank_import(self.company_account).unlink()

        self.assertEqual(self.lines._game_bank_import(self.company_account).payment_ref, "again")

    def test_an_unconnected_account_feeds_nothing(self):
        self.accounts._deposit(self.buyer.id, 100)
        self.assertFalse(self.lines._game_bank_import(self.buyer_account))

    def test_a_transaction_on_a_connected_account_wakes_the_feed(self):
        cron = self.env.ref('odoo_sim.ir_cron_bank_feed')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        self.accounts._deposit(self.buyer.id, 100)
        self.assertEqual(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before,
                         "nothing to import for the company")

        self.buyer_account._pay(self.company_account, 10, "wake up")
        self.assertGreater(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)

    def test_a_line_typed_into_odoo_moves_no_money(self):
        """ The journal can say a million came in. The bank knows better. """
        self.lines.create({'journal_id': self.journal.id, 'amount': 1_000_000, 'payment_ref': "Wishful thinking"})

        self.assertEqual(self.earned(), 0)
