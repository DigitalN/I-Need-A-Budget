# SPDX-License-Identifier: GPL-3.0-only
"""What each of two computers changed since the save both started from (issue #184, docs/adr/0008).

Made-up budgets, built through BudgetService in throwaway folders: a start, then two copies of it
standing in for two computers, each changed on its own and compared with the start."""

import shutil
import sqlite3

import pytest

import backups
import changes
from db import Database
from helpers import call, write
from service import BudgetService
from settings import Settings


def ofx(rows, balance, as_of, acct_id="000111222333", kind="CHECKING"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>SAMPLE CU"
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>{kind}</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


FIRST = [("20260901", "2000.00", "PAYROLL SAMPLE CO", "A1"), ("20260903", "-4.50", "SAMPLE CAFE CALGARY AB", "A2"),
         ("20260905", "-60.00", "SAMPLE GROCER CALGARY AB", "A3")]
SECOND = [("20261002", "-12.00", "SAMPLE BAKERY CALGARY AB", "B1"), ("20261004", "-30.00", "SAMPLE BOOKS CALGARY AB", "B2"),
          ("20261006", "-8.25", "SAMPLE DELI CALGARY AB", "B3")]


def open_service(folder):
    downloads = folder / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    store = Settings(folder / "settings.json")
    store.update({"downloads_dir": str(downloads)})
    service = BudgetService(Database(folder / "budget.db"), store, folder / "data")
    service.test_downloads = downloads
    return service


def save(service, path):
    """The whole budget in one file, as a save or a backup is made (VACUUM INTO)."""
    with service.db.lock:
        service.db.conn.execute("VACUUM INTO ?", (str(path),))
    return path


class Budgets:
    """The save both computers started from, and the two computers."""

    def __init__(self, tmp_path, start):
        self.tmp = tmp_path
        self.start = save(start, tmp_path / "start.inab")
        self.this, self.other = self.computer("this"), self.computer("other")

    def computer(self, name):
        folder = self.tmp / name
        folder.mkdir()
        shutil.copyfile(self.start, folder / "budget.db")
        return open_service(folder)

    def compare(self):
        return changes.compare(self.start, self.this.db, self.other.db)

    def this_side(self):
        """This computer's changes, checking the other computer (which changed nothing) lists none."""
        both = self.compare()
        assert both["other"] == {"changed": False, "summary": "", "sections": []}
        return both["this"]


@pytest.fixture
def budgets(app, tmp_path):
    """A budget set up in September 2026 (today is Oct 12): Chequing from a file, three categories, two
    transactions waiting in the inbox (the payroll's Income too, as the app guesses nothing)."""
    first = write(app.test_downloads, "chequing-1.ofx", ofx(FIRST, "3000.00", "20260915"))
    call(app, "import_files", [str(first)])
    groups = {"Everyday": ["Food", "Fun"], "Bills": ["Rent"]}
    for group, names in groups.items():
        gid = call(app, "add_group", group)
        for n in names:
            call(app, "add_category", gid, n)
    call(app, "finish_onboarding", "2026-09")
    call(app, "set_assigned", "2026-10", cat(app, "Food"), 30000)
    return Budgets(tmp_path, app)


def cat(service, name):
    return {c["name"]: c["id"] for g in call(service, "categories") for c in g["categories"]}[name]


def group_id(service, name):
    return {g["name"]: g["id"] for g in call(service, "categories")}[name]


def txn(service, name, account_name="SAMPLE CU Chequing"):
    """The transaction in `account_name` whose payee or bank wording has `name` in it."""
    found = [t for t in call(service, "transactions", {})["items"] if t["account_name"] == account_name
             and name.lower() in f"{t['payee_name']} {t['raw_name']}".lower()]
    assert len(found) == 1, found
    return found[0]


def account(service, name):
    return next(a for a in call(service, "accounts") if a["name"] == name)


def texts(side, kind=None):
    return [item["text"] for s in side["sections"] if kind in (None, s["kind"]) for item in s["items"]]


def kinds(side):
    return [s["kind"] for s in side["sections"]]


# ── nothing to list ──

def test_nothing_changed_lists_nothing(budgets):
    assert budgets.compare() == {"this": {"changed": False, "summary": "", "sections": []},
                                 "other": {"changed": False, "summary": "", "sections": []}}


def test_bookkeeping_is_never_a_change(budgets, monkeypatch):
    """This session's undo, backups' own meta, what the budget folder keeps about saves, the inbox's
    reasons and a group folded are never listed."""
    this = budgets.this
    monkeypatch.setattr(changes, "BOOKKEEPING_META", changes.BOOKKEEPING_META | {"saved_by", "save_id"})
    call(this, "add_group", "Gifts")
    call(this, "undo")                              # an undo step made and undone: only undo's tables
    call(this, "set_group_collapsed", group_id(this, "Everyday"), True)
    backups.make(this.db)                            # backed_up, backed_up_to
    this.db.set_meta("saved_by", "Sample Computer")  # what a save keeps (issue #183 adds its keys)
    this.db.set_meta("save_id", "0001")
    with this.db.write(keep_undo=True) as c:
        c.execute("UPDATE transactions SET reason = 'Only text to show'")
        c.execute("INSERT INTO undo_groups (label) VALUES ('Something')")
    assert budgets.this_side() == {"changed": False, "summary": "", "sections": []}


def test_a_meta_key_nobody_described_is_seen(budgets):
    budgets.this.db.set_meta("a_later_versions_setting", "1")
    side = budgets.this_side()
    assert side["summary"] == "Something else in the budget changed"


# ── files and the inbox ──

def test_a_file_imported_and_categorized(budgets):
    """The issue's example: an import from Downloads, some of it categorized."""
    this = budgets.this
    second = write(this.test_downloads, "chequing-2.ofx", ofx(SECOND, "2949.75", "20261010"))
    call(this, "import_files", [str(second)])
    call(this, "categorize", [txn(this, "BAKERY")["id"]], cat(this, "Food"))
    call(this, "categorize", [txn(this, "CAFE")["id"]], cat(this, "Food"))
    call(this, "set_assigned", "2026-10", cat(this, "Food"), 35000)
    side = budgets.this_side()
    assert side["changed"]
    assert side["summary"] == ("1 file imported (3 transactions) · 2 categorized · "
                               "Food's Assigned for October 2026 $300.00 → $350.00")
    assert texts(side, "imported") == ["chequing-2.ofx into SAMPLE CU Chequing: 3 new transactions"]
    assert texts(side, "categorized") == ["Sample Cafe, Sep 3, −$4.50: categorized as Food",
                                          "Sample Bakery, Oct 2, −$12.00: categorized as Food"]
    assert all(not item["app"] for s in side["sections"] for item in s["items"])


def test_what_the_app_categorized_on_its_own_says_so(budgets):
    this = budgets.this
    call(this, "categorize", [txn(this, "CAFE")["id"]], cat(this, "Food"))
    start = save(this, budgets.tmp / "after-cafe.inab")
    again = write(this.test_downloads, "chequing-2.ofx",
                  ofx([("20261002", "-4.50", "SAMPLE CAFE CALGARY AB", "B9")], "2995.50", "20261010"))
    call(this, "import_files", [str(again)])
    side = changes.what_changed(start, this.db)
    item = next(s for s in side["sections"] if s["kind"] == "categorized")["items"][0]
    assert item == {"text": "Sample Cafe, Oct 2, −$4.50: categorized as Food by the app", "app": True}


def test_an_import_removed(budgets):
    this = budgets.this
    call(this, "undo_import", call(this, "imports")[0]["id"])
    side = budgets.this_side()
    assert side["sections"][0] == {
        "kind": "import_removed", "title": "1 import removed (3 transactions)",
        "items": [{"text": "chequing-1.ofx (SAMPLE CU Chequing): 3 transactions removed", "app": False}]}
    assert "deleted" not in kinds(side)


def test_a_removed_import_whose_transaction_another_file_has(budgets):
    this = budgets.this
    second = write(this.test_downloads, "chequing-2.ofx", ofx([FIRST[2], SECOND[0]], "2928.00", "20261010"))
    call(this, "import_files", [str(second)])
    first = next(i for i in call(this, "imports") if i["filename"] == "chequing-1.ofx")
    call(this, "undo_import", first["id"])
    side = budgets.this_side()
    assert side["summary"] == "1 file imported (1 transaction) · 1 import removed (2 transactions)"
    assert texts(side, "import_removed") == [
        "chequing-1.ofx (SAMPLE CU Chequing): 2 transactions removed; 1 stays: another file has it"]


def test_sent_back_split_and_recategorized(budgets):
    this = budgets.this
    cafe, grocer = txn(this, "CAFE")["id"], txn(this, "GROCER")["id"]
    call(budgets.this, "categorize", [cafe], cat(this, "Food"))
    start = save(this, budgets.tmp / "categorized.inab")
    call(this, "update_transaction", cafe, {"category_id": cat(this, "Fun")})
    call(this, "split_transaction", grocer, [{"category_id": cat(this, "Food"), "amount": -4000},
                                             {"category_id": cat(this, "Fun"), "amount": -2000}])
    side = changes.what_changed(start, this.db)
    assert texts(side, "categorized") == ["Sample Cafe, Sep 3, −$4.50: Food → Fun"]
    assert texts(side, "split") == ["Sample Grocer, Sep 5, −$60.00: split into Food −$40.00 + Fun −$20.00"]
    start = save(this, budgets.tmp / "split.inab")
    call(this, "unsplit_transaction", grocer)
    side = changes.what_changed(start, this.db)
    assert side["summary"] == "1 sent back"
    assert texts(side) == ["Sample Grocer, Sep 5, −$60.00: sent back to the Inbox"]


def test_added_by_hand_edited_and_deleted(budgets):
    this = budgets.this
    wallet = call(this, "add_account", "Wallet", "cash", 5000, "2026-10-01")
    added = call(this, "add_transaction", wallet, "2026-10-03", "Sample Market", -1500, cat(this, "Food"))["id"]
    start = save(this, budgets.tmp / "added.inab")
    call(this, "update_transaction", added, {"date": "2026-10-04", "amount": -1750, "note": "Made-up note"})
    call(this, "update_transaction", txn(this, "GROCER")["id"], {"note": "Another note"})
    call(this, "delete_transaction", txn(this, "CAFE")["id"])
    side = changes.what_changed(start, this.db)
    assert side["summary"] == "2 edited · 1 deleted"
    assert texts(side, "edited") == [
        "Sample Grocer, Sep 5, −$60.00: note “Another note”",
        "Sample Market, Oct 3, −$15.00: date Oct 3 → Oct 4; amount −$15.00 → −$17.50; note “Made-up note”"]
    assert texts(side, "deleted") == ["Sample Cafe, Sep 3, −$4.50 in SAMPLE CU Chequing: deleted"]
    side = budgets.this_side()  # from the start: the Wallet and its transaction are new
    assert texts(side, "added") == ["Sample Market, Oct 4, −$17.50 in Wallet: added by hand (Food)"]
    assert "Added account Wallet (Wallet)" in texts(side, "accounts")
    assert "Wallet's balance set: $50.00 on Oct 1, 2026" in texts(side, "accounts")


def test_a_transfer_is_one_change(budgets):
    this = budgets.this
    savings = call(this, "add_account", "Savings", "savings", 0, "2026-09-01")
    start = save(this, budgets.tmp / "savings.inab")
    call(this, "update_transaction", txn(this, "GROCER")["id"], {"transfer_account_id": savings})
    side = changes.what_changed(start, this.db)
    assert side["summary"] == "1 marked as a transfer"
    assert texts(side) == ["$60.00 from SAMPLE CU Chequing to Savings, Sep 5: marked as a transfer"]
    start = save(this, budgets.tmp / "transfer.inab")
    call(this, "update_transaction", txn(this, "GROCER")["id"], {"transfer_account_id": None})
    side = changes.what_changed(start, this.db)
    assert side["summary"] == "1 sent back"  # the half the app added to Savings went with the transfer


def test_a_balance_adjustment_from_check_balance(budgets):
    this = budgets.this
    chequing = account(this, "SAMPLE CU Chequing")
    call(this, "reconcile", chequing["id"], chequing["balance"] - 1000)
    side = budgets.this_side()
    assert texts(side, "balance_adjustment") == [
        "Balance adjustment in SAMPLE CU Chequing, Oct 12, −$10.00: added by Check balance…"]
    assert next(s for s in side["sections"] if s["kind"] == "balance_adjustment")["title"] == "1 balance adjustment added"


# ── the budget ──

def test_assigned_and_budgeted(budgets):
    this = budgets.this
    call(this, "move_money", "2026-10", cat(this, "Food"), cat(this, "Fun"), 5000)
    call(this, "set_monthly_budget", cat(this, "Rent"), 120000)
    call(this, "set_monthly_budget", cat(this, "Food"), 40000)
    side = budgets.this_side()
    assert side["summary"] == "2 Assigned amounts changed · 2 Budgeted amounts changed"
    assert texts(side, "assigned") == ["Food's Assigned for October 2026 $300.00 → $250.00",
                                       "Fun's Assigned for October 2026 $0.00 → $50.00"]
    assert texts(side, "budgeted") == ["Food's Budgeted none → $400.00", "Rent's Budgeted none → $1,200.00"]


def test_categories_and_groups(budgets):
    this = budgets.this
    everyday, bills = group_id(this, "Everyday"), group_id(this, "Bills")
    call(this, "add_category", everyday, "Gifts")
    call(this, "rename_category", cat(this, "Fun"), "Going Out")
    call(this, "set_category_hidden", cat(this, "Rent"), True)
    call(this, "set_category_note", cat(this, "Food"), "Groceries and takeout")
    call(this, "rename_group", bills, "Monthly Bills")
    call(this, "move_group", bills, everyday)
    call(this, "add_group", "Savings Goals")
    side = budgets.this_side()
    assert texts(side, "categories") == [
        "Added category group Savings Goals", "Renamed category group Bills → Monthly Bills",
        "Moved category group Monthly Bills", "Added category Gifts in Everyday", "Changed the notes for Food",
        "Renamed category Fun → Going Out", "Hid category Rent"]
    assert side["summary"] == "7 changes to categories"
    start = save(this, budgets.tmp / "categories.inab")
    call(this, "set_category_hidden", cat(this, "Rent"), False)
    call(this, "delete_group", group_id(this, "Savings Goals"))
    side = changes.what_changed(start, this.db)
    assert texts(side) == ["Deleted category group Savings Goals", "Showed category Rent again"]


def test_a_category_moved_once_is_one_move(budgets):
    this = budgets.this
    everyday, bills = group_id(this, "Everyday"), group_id(this, "Bills")
    call(this, "add_category", everyday, "Gifts")
    call(this, "add_category", everyday, "Household")
    start = save(this, budgets.tmp / "four.inab")
    call(this, "move_category", cat(this, "Household"), everyday, cat(this, "Food"))  # to the top
    call(this, "move_category", cat(this, "Fun"), bills)
    side = changes.what_changed(start, this.db)
    assert texts(side) == ["Moved category Fun from Everyday to Bills", "Moved category Household in Everyday"]
    assert side["summary"] == "2 categories moved"


def test_the_same_id_added_on_both_sides_is_two_things(budgets):
    call(budgets.this, "add_category", group_id(budgets.this, "Everyday"), "Gifts")
    call(budgets.other, "add_category", group_id(budgets.other, "Everyday"), "Pets")
    assert cat(budgets.this, "Gifts") == cat(budgets.other, "Pets")
    both = budgets.compare()
    assert texts(both["this"]) == ["Added category Gifts in Everyday"]
    assert texts(both["other"]) == ["Added category Pets in Everyday"]


# ── accounts, recurring charges, payees, settings ──

def test_accounts_edited_closed_and_merged(budgets):
    this = budgets.this
    card = call(this, "add_account", "Sample Card", "credit_card", 50000, "2026-10-01")
    copy = call(this, "add_account", "Sample Card Copy", "credit_card")
    call(this, "add_transaction", copy, "2026-10-02", "Sample Bookshop", -2500)
    start = save(this, budgets.tmp / "cards.inab")
    chequing = account(this, "SAMPLE CU Chequing")["id"]
    call(this, "update_account", chequing, {"name": "Everyday Chequing", "bank": "Sample Credit Union"})
    call(this, "update_account", card, {"apr": 19.99, "planned_payment": 10000})
    call(this, "set_balance", card, 45000, "2026-10-10")
    call(this, "merge_accounts", copy, card)
    savings = call(this, "add_account", "Savings", "savings")
    start_2 = save(this, budgets.tmp / "savings.inab")
    call(this, "update_account", savings, {"closed": True})
    side = changes.what_changed(start, this.db)
    assert texts(side, "accounts") == [
        "Added account Savings (Savings)", "Merged Sample Card Copy into Sample Card",
        "Renamed account SAMPLE CU Chequing → Everyday Chequing",
        "Everyday Chequing: bank SAMPLE CU → Sample Credit Union",
        "Sample Card: interest rate none → 19.99%; planned payment none → $100.00",
        "Sample Card's balance set: $450.00 owing on Oct 10, 2026"]
    assert "categories" not in kinds(side)  # the cards' payment categories come and go with them
    assert changes.what_changed(start_2, this.db)["summary"] == "1 account closed"
    start_3 = save(this, budgets.tmp / "closed.inab")
    call(this, "update_account", savings, {"closed": False})
    assert changes.what_changed(start_3, this.db)["summary"] == "1 account reopened"


def test_recurring_charges(budgets):
    this = budgets.this
    sub = call(this, "add_subscription", payee="Sample Stream", name="Sample Stream", amount=1199,
               category_id=cat(this, "Fun"))
    side = budgets.this_side()
    assert texts(side, "recurring_charges") == ["Started tracking Sample Stream: $11.99 a month"]
    start = save(this, budgets.tmp / "stream.inab")
    call(this, "update_subscription", sub, {"amount": 1299, "pricing": "varies", "category_id": cat(this, "Food")})
    call(this, "cancel_subscription", sub, "2026-10-12")
    side = changes.what_changed(start, this.db)
    assert texts(side) == ["Sample Stream: price $11.99 → $12.99; price type Same Price → Fluctuates Every Time; "
                           "category Fun → Food; marked cancelled"]
    assert side["summary"] == "1 recurring charge changed"
    start = save(this, budgets.tmp / "cancelled.inab")
    call(this, "delete_subscription", sub)
    assert changes.what_changed(start, this.db)["summary"] == "Stopped tracking 1 recurring charge"


def test_payees_renamed_merged_and_always_ask(budgets):
    this = budgets.this
    cafe, grocer = txn(this, "CAFE"), txn(this, "GROCER")
    call(this, "rename_payee", cafe["payee_id"], "Corner Cafe")
    call(this, "set_always_ask", grocer["payee_id"], True)
    side = budgets.this_side()
    assert texts(side, "payees") == ["Renamed payee Sample Cafe → Corner Cafe", "Always ask on for Sample Grocer"]
    start = save(this, budgets.tmp / "renamed.inab")
    call(this, "rename_payee", grocer["payee_id"], "Corner Cafe")  # the same name: one payee now
    side = changes.what_changed(start, this.db)
    assert texts(side) == ["Sample Grocer merged into Corner Cafe"]
    assert "edited" not in kinds(side)


def test_the_budgets_own_settings(budgets):
    this = budgets.this
    call(this, "save_settings", {"overspending": "carry_over", "average_months": 6, "theme": "dark"})
    side = budgets.this_side()
    assert texts(side) == ["Overspending Next month → Carry over", "Usual's months 3 → 6"]
    assert side["summary"] == "2 settings changed"


def test_the_budget_start(budgets):
    call(budgets.this, "set_budget_start", "2026-10")
    assert budgets.this_side()["summary"] == "Budget start September 2026 → October 2026"


# ── each side, on its own ──

def test_each_side_lists_only_its_own_changes(budgets):
    this, other = budgets.this, budgets.other
    call(this, "categorize", [txn(this, "CAFE")["id"]], cat(this, "Food"))
    call(other, "categorize", [txn(other, "CAFE")["id"]], cat(other, "Fun"))
    call(other, "set_assigned", "2026-10", cat(other, "Rent"), 100000)
    both = budgets.compare()
    assert both["this"]["summary"] == "1 categorized"
    assert texts(both["this"]) == ["Sample Cafe, Sep 3, −$4.50: categorized as Food"]
    assert both["other"]["summary"] == "1 categorized · Rent's Assigned for October 2026 $0.00 → $1,000.00"
    assert texts(both["other"], "categorized") == ["Sample Cafe, Sep 3, −$4.50: categorized as Fun"]


def test_a_transaction_under_a_reused_id_is_a_new_one(budgets):
    """SQLite gives a deleted newest row's id to the next one: made at another time, it's another transaction."""
    this = budgets.this
    wallet = call(this, "add_account", "Wallet", "cash", 5000, "2026-10-01")
    first = call(this, "add_transaction", wallet, "2026-10-03", "Sample Market", -1500)["id"]
    start = save(this, budgets.tmp / "market.inab")
    call(this, "delete_transaction", first)
    again = call(this, "add_transaction", wallet, "2026-10-05", "Sample Kiosk", -300)["id"]
    assert again == first
    with this.db.write(keep_undo=True) as c:  # added a moment later than the one deleted
        c.execute("UPDATE transactions SET created_at = '2026-10-12 23:59:59' WHERE id = ?", (again,))
    side = changes.what_changed(start, this.db)
    assert side["summary"] == "1 added by hand · 1 deleted"


# ── reading ──

def test_it_only_reads(budgets):
    """Files are read through a read-only connection: nothing written, nothing left beside them."""
    call(budgets.this, "add_group", "Gifts")
    this_file = save(budgets.this, budgets.tmp / "this.inab")
    other_file = save(budgets.other, budgets.tmp / "other.inab")
    before = {p.name: p.read_bytes() for p in budgets.tmp.iterdir() if p.is_file()}
    both = changes.compare(budgets.start, this_file, other_file)
    assert both["this"]["summary"] == "1 category group added" and not both["other"]["changed"]
    assert {p.name: p.read_bytes() for p in budgets.tmp.iterdir() if p.is_file()} == before


def test_any_way_of_passing_a_budget(budgets):
    call(budgets.this, "add_group", "Gifts")
    conn = sqlite3.connect(str(budgets.this.db.path))
    try:
        by_conn = changes.what_changed(budgets.start, conn)
    finally:
        conn.close()
    by_db = changes.what_changed(str(budgets.start), budgets.this.db)
    assert by_conn == by_db and by_db["summary"] == "1 category group added"


def test_a_budget_that_isnt_there(budgets):
    with pytest.raises(FileNotFoundError):
        changes.what_changed(budgets.tmp / "nowhere.inab", budgets.this.db)


def test_money_as_the_page_writes_it():
    assert [changes.money(c) for c in (0, 450, -450, 123456789, -100)] == \
        ["$0.00", "$4.50", "−$4.50", "$1,234,567.89", "−$1.00"]
