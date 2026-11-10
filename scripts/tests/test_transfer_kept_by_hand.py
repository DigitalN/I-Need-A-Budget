# SPDX-License-Identifier: GPL-3.0-only
"""A transfer you mark with an account kept by hand (the Wallet, or any account no file has
brought transactions to): with no other half there, the app adds it, paired and yours, and
takes it away again when the transfer ends (issue #75). Accounts that get files still wait for
their file. Made-up data only."""

from helpers import bank_csv, call, categories, txns, write

ATM = ("10/05/2026", "ABM Withdrawal - SAMPLE BANK", "ABM Withdrawal - SAMPLE BANK", "-100.00")
PAY = ("09/15/2026", "PAYROLL DEPOSIT SAMPLE CO", "PAYROLL DEPOSIT SAMPLE CO", "2000.00")


def ofx(rows, balance, as_of, acct_id="000999888777", acct="SAVINGS"):
    """A made-up OFX download from a made-up bank. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>SAMPLE BANK"
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>{acct}</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


class Budget:
    """Chequing from a file, with a $100.00 cash withdrawal waiting in the inbox; a Wallet; savings
    at another bank that gets files; the budget starting in September."""

    def __init__(self, app):
        self.app = app
        call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([PAY, ATM])))])
        call(app, "import_files", [str(write(app.test_downloads, "savings.ofx", ofx(
            [("20260920", "25.00", "SAMPLE DEPOSIT", "S1")], "525.00", "20261001")))])
        self.chequing = next(a["id"] for a in call(app, "accounts") if a["kind"] == "chequing")
        self.savings = next(a["id"] for a in call(app, "accounts") if a["kind"] == "savings")
        call(app, "set_balance", self.chequing, 300000, "2026-10-11")
        self.wallet = call(app, "add_account", "Wallet", "cash", 4000, "2026-09-30")
        call(app, "finish_onboarding", "2026-09")
        self.atm = next(t["id"] for t in txns(app) if t["amount"] == -10000)
        assert self.row(self.atm)["status"] == "review"

    def row(self, txn_id):
        return self.app.db.conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()

    def rows(self, account_id):
        return self.app.db.conn.execute("SELECT * FROM transactions WHERE account_id = ? ORDER BY id",
                                        (account_id,)).fetchall()

    def mark(self, txn_id, account_id):
        return call(self.app, "update_transaction", txn_id, {"transfer_account_id": account_id})

    def off(self):
        return call(self.app, "month", "2026-10")["summary"]["off"]


def test_a_transfer_to_the_wallet_gets_its_other_half_there(app):
    b = Budget(app)
    assert b.off() == 0  # waiting in the inbox, the withdrawal is money out
    out = b.mark(b.atm, b.wallet)
    [half] = b.rows(b.wallet)
    atm = b.row(b.atm)
    assert (half["amount"], half["date"], half["transfer_id"], half["transfer_account_id"]) == (10000, "2026-10-05",
                                                                                                  b.atm, b.chequing)
    assert (atm["transfer_id"], atm["transfer_account_id"]) == (half["id"], b.wallet)
    assert (half["status"], half["by_you"], atm["status"], atm["by_you"]) == ("done", 1, "done", 1)
    assert half["payee_id"] == atm["payee_id"] and half["fingerprint"] is None and half["import_id"] is None
    assert txns(app, account_id=b.wallet)[0]["manual"]  # added by hand, as the Transactions page shows it
    assert b.off() == 0  # the check line: was $100.00 off for good
    assert out["added_to"] == "Wallet"
    assert b.atm not in {i["id"] for i in call(app, "inbox")["items"]}


def test_an_account_kept_by_hand_gets_its_other_half_too(app):
    b = Budget(app)
    other = call(app, "add_account", "Sample Savings", "savings", 0, "2026-09-30", "Sample Credit Union")
    b.mark(b.atm, other)
    [half] = b.rows(other)
    assert (half["amount"], half["transfer_id"]) == (10000, b.atm) and b.off() == 0


def test_an_account_that_gets_files_still_waits_for_its_file(app):
    b = Budget(app)
    before = len(b.rows(b.savings))
    out = b.mark(b.atm, b.savings)
    atm = b.row(b.atm)
    assert len(b.rows(b.savings)) == before and out["added_to"] is None
    assert (atm["transfer_account_id"], atm["transfer_id"], atm["status"]) == (b.savings, None, "done")


def test_one_undo_removes_both_from_command_z_or_the_toast(app):
    b = Budget(app)
    for toast in (False, True):
        b.mark(b.atm, b.wallet)
        step = call(app, "undo_step")
        assert step["label"] == "Edit transaction" and not step["big"]  # no question first
        call(app, "undo", step["step"]) if toast else call(app, "undo")
        assert b.rows(b.wallet) == []
        atm = b.row(b.atm)
        assert (atm["transfer_account_id"], atm["transfer_id"], atm["status"]) == (None, None, "review")


def test_not_a_transfer_removes_the_half_the_app_added_from_either_side(app):
    b = Budget(app)
    for side in ("atm", "half"):
        b.mark(b.atm, b.wallet)
        [half] = b.rows(b.wallet)
        out = b.mark(b.atm if side == "atm" else half["id"], None)
        assert b.rows(b.wallet) == [] and out["removed_from"] == "Wallet"
        atm = b.row(b.atm)
        assert (atm["status"], atm["by_you"], atm["transfer_account_id"]) == ("review", 1, None)
        assert atm["reason"] == "You said this isn't a transfer"
    call(app, "undo")  # undoing Not a transfer brings the half back, paired
    [half] = b.rows(b.wallet)
    assert half["transfer_id"] == b.atm and b.row(b.atm)["transfer_id"] == half["id"]


def test_an_other_half_already_there_is_paired_and_nothing_is_added(app):
    b = Budget(app)
    mine = call(app, "add_transaction", b.wallet, "2026-10-06", "Cash from the bank", 10000)["id"]
    b.mark(b.atm, b.wallet)
    assert [r["id"] for r in b.rows(b.wallet)] == [mine] and b.row(mine)["transfer_id"] == b.atm


def test_a_balance_adjustment_is_never_the_other_half(app):
    """Check balance… on the Wallet adds a $100.00 balance adjustment (it stands for what the app
    doesn't have): marking the withdrawal pairs it with a half of its own, never with that."""
    b = Budget(app)
    call(app, "reconcile", b.wallet, 14000)
    [adjustment] = b.rows(b.wallet)
    b.mark(b.atm, b.wallet)
    adjustment, half = b.rows(b.wallet)
    assert (adjustment["hint"], adjustment["transfer_id"], adjustment["status"]) == ("adjustment", None, "review")
    assert half["transfer_id"] == b.atm and half["hint"] != "adjustment"


def test_a_split_withdrawal_marked_as_a_transfer(app):
    b = Budget(app)
    ids = categories(app, {"Everyday": ["Food", "Fun Money"]})
    call(app, "split_transaction", b.atm, [{"category_id": ids["Food"], "amount": -6000},
                                           {"category_id": ids["Fun Money"], "amount": -4000}])
    b.mark(b.atm, b.wallet)
    [half] = b.rows(b.wallet)
    atm = b.row(b.atm)
    assert (atm["is_split"], atm["transfer_id"], half["transfer_id"]) == (0, half["id"], b.atm)
    assert app.db.conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0


def test_the_added_half_goes_whenever_the_transfer_ends(app):
    """It only existed because of the transfer: categorizing, splitting or deleting the withdrawal,
    or marking it as a transfer with another account, takes the half the app added with it."""
    b = Budget(app)
    ids = categories(app, {"Everyday": ["Food", "Fun Money"]})
    endings = {
        "categorized": lambda: call(app, "update_transaction", b.atm, {"category_id": ids["Food"]}),
        "split": lambda: call(app, "split_transaction", b.atm, [{"category_id": ids["Food"], "amount": -6000},
                                                                {"category_id": ids["Fun Money"], "amount": -4000}]),
        "another account": lambda: b.mark(b.atm, b.savings),
        "deleted": lambda: call(app, "delete_transaction", b.atm),
    }
    for what, end in endings.items():
        b.mark(b.atm, b.wallet)
        assert len(b.rows(b.wallet)) == 1, what
        end()
        assert b.rows(b.wallet) == [], what
        assert b.app.db.conn.execute("SELECT COUNT(*) FROM transactions WHERE status = 'done' AND category_id IS NULL "
                                     "AND is_split = 0 AND transfer_account_id IS NULL").fetchone()[0] == 0, what
        if what != "deleted":
            call(app, "undo")
            call(app, "undo")
            assert b.row(b.atm)["status"] == "review" and b.rows(b.wallet) == [], what


def test_removing_the_import_takes_the_added_half_and_undoing_it_brings_both_back(app):
    b = Budget(app)
    b.mark(b.atm, b.wallet)
    chequing_import = next(i["id"] for i in call(app, "imports") if i["account_id"] == b.chequing)
    call(app, "undo_import", chequing_import)
    assert b.row(b.atm) is None and b.rows(b.wallet) == []
    call(app, "undo")
    [half] = b.rows(b.wallet)
    assert half["transfer_id"] == b.atm and b.row(b.atm)["transfer_id"] == half["id"]


def test_a_merge_that_puts_both_halves_in_one_account_takes_the_added_half(app):
    """The Wallet merged into chequing: the transfer is now within one account, so it isn't one, and
    the half the app added for it goes; the withdrawal is the app's to decide about again."""
    b = Budget(app)
    b.mark(b.atm, b.wallet)
    call(app, "merge_accounts", b.wallet, b.chequing)
    assert [r["id"] for r in b.rows(b.chequing) if r["amount"] == 10000] == []
    atm = b.row(b.atm)
    assert (atm["transfer_account_id"], atm["transfer_id"], atm["status"], atm["by_you"]) == (None, None, "review", 0)


def test_marking_from_the_wallets_side(app):
    """Cash you took to the bank: from the Wallet to chequing, which gets files, waits for the bank's
    row; to savings kept by hand, its other half is added there."""
    b = Budget(app)
    other = call(app, "add_account", "Sample Savings", "savings", 0, "2026-09-30", "Sample Credit Union")
    cash = call(app, "add_transaction", b.wallet, "2026-10-08", "Deposit", -2500)["id"]
    b.mark(cash, b.chequing)
    assert b.row(cash)["transfer_id"] is None and len(b.rows(b.chequing)) == 2
    b.mark(cash, other)
    [half] = b.rows(other)
    assert (half["amount"], half["transfer_id"], b.row(cash)["transfer_id"]) == (2500, cash, half["id"])


def test_a_card_kept_by_hand_gets_its_payment(app):
    b = Budget(app)
    card = call(app, "add_account", "Store Card", "credit_card", 30000, "2026-09-30")
    owed = lambda: next(a["balance"] for a in call(app, "accounts") if a["id"] == card)
    assert owed() == -30000
    b.mark(b.atm, card)
    assert owed() == -20000 and b.off() == 0


def test_a_closed_account_gets_no_half(app):
    b = Budget(app)
    call(app, "update_account", b.wallet, {"closed": True})
    b.mark(b.atm, b.wallet)
    assert b.rows(b.wallet) == [] and b.row(b.atm)["transfer_account_id"] == b.wallet


def test_categorizing_the_added_half_makes_it_yours(app):
    """You decide about the half itself: it stays, in its category, and the withdrawal waits for you."""
    b = Budget(app)
    ids = categories(app, {"Everyday": ["Food"]})
    b.mark(b.atm, b.wallet)
    [half] = b.rows(b.wallet)
    call(app, "update_transaction", half["id"], {"category_id": ids["Food"]})
    [half] = b.rows(b.wallet)
    assert (half["category_id"], half["transfer_id"], half["by_you"]) == (ids["Food"], None, 1)
    assert b.row(b.atm)["status"] == "review"
    b.mark(half["id"], b.chequing)  # a transfer again, now one you marked: it pairs with the withdrawal
    assert b.row(half["id"])["transfer_id"] == b.atm
    out = b.mark(half["id"], None)
    assert len(b.rows(b.wallet)) == 1 and out["removed_from"] is None  # Not a transfer sends it back


def test_the_banks_row_takes_the_added_halfs_place(app):
    """Savings kept by hand until its first file: the file's row of the same amount a day later takes
    the half the app added (keeping the transfer), so it isn't there twice, and it's the bank's now."""
    b = Budget(app)
    other = call(app, "add_account", "Sample Savings", "savings", 0, "2026-09-30", "Sample Credit Union")
    b.mark(b.atm, other)
    path = write(app.test_downloads, "Savings.CSV", bank_csv([
        ("10/06/2026", "INTERNET TRANSFER FROM CHEQUING", "INTERNET TRANSFER FROM CHEQUING", "100.00")]))
    call(app, "import_files", [str(path)])
    call(app, "set_file_accounts", str(path), [{"index": 0, "account_id": other}])
    [half] = b.rows(other)
    assert (half["date"], half["transfer_id"], half["fingerprint"] is not None) == ("2026-10-06", b.atm, True)
    b.mark(b.atm, None)
    assert len(b.rows(other)) == 1  # a real transaction now: Not a transfer sends it back instead
