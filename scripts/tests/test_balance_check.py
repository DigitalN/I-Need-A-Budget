# SPDX-License-Identifier: GPL-3.0-only
"""A file's balance never quietly moves the budget's starting balance: a difference between two
files' balances waits in the Inbox as a balance adjustment, worked out by what arrived (issue #43).
Made-up data only."""

import db
from helpers import bank_csv, call, card_number, write
from test_card_signs import card_ofx


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE CU"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _import(app, name, text):
    return call(app, "import_files", [str(write(app.test_downloads, name, text))])


def _adjustments(app):
    return [i for i in call(app, "inbox")["items"] if i["payee_name"] == "Balance adjustment"]


def _start(app):
    return call(app, "month", "2026-09")["summary"]["carried_rta"]


SEPTEMBER = [("20260901", "2000.00", "PAYROLL SAMPLE CO", "A1"), ("20260910", "-50.00", "SAMPLE CAFE CALGARY AB", "A2")]
MISSING = [("20260920", "-100.00", "SAMPLE PHONE CO", "A5"), ("20260925", "-200.00", "SAMPLE INSURANCE CO", "A6")]
OCTOBER = [("20261003", "-100.00", "SAMPLE HARDWARE CALGARY AB", "A4")]


def _first_file(app):
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    call(app, "finish_onboarding", "2026-09")


def _second_file(app):
    """October's download, without the two September payments the bank's balance has (a download missing)."""
    return _import(app, "chequing-2.ofx", ofx(OCTOBER, "2600.00", "20261010"))


def test_a_gap_between_downloads_doesnt_move_the_starting_balance(app):
    """The issue's case: the balance taken from file 2 moved September's starting Ready to assign by $300."""
    _first_file(app)
    start = _start(app)
    assert start == 105000
    out = _second_file(app)
    assert _start(app) == start
    [adjustment] = _adjustments(app)
    assert (adjustment["amount"], adjustment["date"], adjustment["guess"], adjustment["group"]) == (-30000, "2026-10-10", None, "")
    assert "$300.00 more went out" in adjustment["reason"] and "between Sep 15 and Oct 3" in adjustment["reason"]
    assert "balance adjustment is waiting in the Inbox" in out["results"][0]["balance_note"]
    assert "balance adjustment" in call(app, "imports")[0]["balance_note"]  # Accounts → Imported files says so too
    assert call(app, "accounts")[0]["balance"] == 260000  # the bank's balance, as before


def test_a_balance_adjustment_is_never_guessed_or_accepted(app):
    _first_file(app)
    _second_file(app)
    [adjustment] = _adjustments(app)
    out = call(app, "accept_all", [adjustment["id"]])
    assert out["categorized"] == 0 and len(_adjustments(app)) == 1


def test_a_typed_balance_gives_way_to_a_files_without_a_word(app):
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 290000, "2026-09-30")  # typed: wrong by the two payments' $300.00
    out = _second_file(app)
    assert _adjustments(app) == [] and out["results"][0]["balance_note"] == ""
    assert call(app, "accounts")[0]["balance"] == 260000


def test_a_card_files_charges_dated_before_the_last_balance_make_no_adjustment(app):
    """Card files bring charges dated before they posted: by date, most card balances looked off."""
    sep = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260912", "-40.00", "SAMPLE CAFE", "S2")]
    _import(app, "card-1.ofx", card_ofx(sep, "-100.00", "20260930"))
    # A charge from Sep 29 that posted after the first download, then October's own.
    october = [("20260929", "-25.00", "SAMPLE TAXI", "S3"), ("20261005", "-30.00", "SAMPLE BAKERY", "O1")]
    out = _import(app, "card-2.ofx", card_ofx(october, "-155.00", "20261010"))
    assert _adjustments(app) == [] and out["results"][0]["balance_note"] == ""
    assert call(app, "accounts")[0]["balance"] == -15500


def test_a_waiting_adjustment_shrinks_and_goes_as_the_missing_transactions_arrive(app):
    _first_file(app)
    _second_file(app)
    # The missing downloads turn up: September's second half, in two older files.
    out = _import(app, "chequing-sep-a.ofx", ofx(MISSING[:1], "2850.00", "20260922"))
    assert [a["amount"] for a in _adjustments(app)] == [-20000]
    assert "now -$200.00" in out["results"][0]["balance_note"]
    out = _import(app, "chequing-sep-b.ofx", ofx(MISSING[1:], "2650.00", "20260930"))
    assert _adjustments(app) == []
    assert "the balance adjustment is gone" in out["results"][0]["balance_note"]
    assert call(app, "accounts")[0]["balance"] == 260000


def test_removing_the_file_that_filled_the_gap_puts_the_adjustment_back(app):
    _first_file(app)
    _second_file(app)
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2650.00", "20260930"))
    assert _adjustments(app) == []
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert [a["amount"] for a in _adjustments(app)] == [-30000]
    # ...and removing the file that made it takes it, and its balance, away.
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _adjustments(app) == []
    assert (call(app, "accounts")[0]["anchor_date"], _start(app)) == ("2026-09-15", 105000)


def test_one_you_categorized_asks_when_its_money_arrives_and_remove_it_takes_both(app):
    _first_file(app)
    _second_file(app)
    categories = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    [adjustment] = _adjustments(app)
    call(app, "categorize", txn_ids=[adjustment["id"]], category_id=categories["Income"])  # yours now
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2650.00", "20260930"))
    [question] = _adjustments(app)  # the app's new one, cancelling yours out
    assert question["amount"] == 30000 and question["guess"] is None
    assert question["reason"] == "The $300.00 your balance adjustment stood for has arrived: remove the adjustment?"
    assert question["adjustment_question"]["ids"] == [adjustment["id"]]
    call(app, "remove_adjustment", question["id"])
    assert _adjustments(app) == []
    assert not [t for t in call(app, "transactions", {})["items"] if t["payee_name"] == "Balance adjustment"]
    assert call(app, "accounts")[0]["balance"] == 260000 and _start(app) == 105000
    assert call(app, "undo")["undone"] == "Remove balance adjustment"  # one small step
    assert len(_adjustments(app)) == 1


def test_keep_both_makes_the_new_adjustment_yours(app):
    _first_file(app)
    _second_file(app)
    categories = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    [adjustment] = _adjustments(app)
    call(app, "categorize", txn_ids=[adjustment["id"]], category_id=categories["Income"])
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2650.00", "20260930"))
    [question] = _adjustments(app)
    call(app, "keep_both", question["id"])
    [kept] = _adjustments(app)
    assert kept["id"] == question["id"] and kept["yours"] and "adjustment_question" not in kept
    # A later download changes nothing: both are yours, and they cancel out.
    _import(app, "chequing-3.ofx", ofx([("20261012", "-20.00", "SAMPLE BAKERY", "A7")], "2580.00", "20261012"))
    assert [a["id"] for a in _adjustments(app)] == [kept["id"]]


def test_an_adjustment_is_never_a_transfers_other_half_nor_a_copy_of_a_bank_row(app):
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(OCTOBER, "2700.00", "20261010"))  # $200.00 missing
    [adjustment] = _adjustments(app)
    # The card's file brings a $200.00 payment the day before: it isn't paired with the adjustment...
    card = [("20261009", "200.00", "PAYMENT - THANK YOU", "P1"), ("20261002", "-15.00", "SAMPLE CAFE", "P2")]
    _import(app, "card.ofx", card_ofx(card, "-0.00", "20261010", acct_id=card_number("4444")))
    again = next(t for t in call(app, "transactions", {})["items"] if t["id"] == adjustment["id"])
    assert (again["transfer_id"], again["status"]) == (None, "review")
    # ...and chequing's CSV bringing the missing row, dated next to it, isn't taken for a copy of it.
    csv = bank_csv([("10/03/2026", "SAMPLE HARDWARE CALGARY AB", "SAMPLE HARDWARE CALGARY AB", "-100.00"),
                    ("10/09/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-200.00"),
                    ("10/11/2026", "SAMPLE CAFE CALGARY AB", "SAMPLE CAFE CALGARY AB", "-4.00")])
    path = write(app.test_downloads, "Chequing.CSV", csv)
    chequing = next(a["id"] for a in call(app, "accounts") if not a["is_debt"])
    [result] = call(app, "set_file_accounts", str(path), [{"index": 0, "account_id": chequing}])["results"]
    assert result["added"] == 2  # the payment and the café, not the hardware store again
    # The CSV goes past the balance's date, so what it brought counts once a newer balance has it.
    assert [a["amount"] for a in _adjustments(app)] == [-20000]
    _import(app, "chequing-3.ofx", ofx([("20261012", "-20.00", "SAMPLE BAKERY", "A7")], "2676.00", "20261012"))
    assert _adjustments(app) == []


def test_reconciling_adds_an_adjustment_that_waits_and_the_next_file_is_taken_as_it_is(app):
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 295000)["adjusted"] == -5000
    [adjustment] = _adjustments(app)
    assert adjustment["guess"] is None and adjustment["reason"] == "To match the balance the bank shows"
    _second_file(app)  # checks start again from this file's balance
    assert [a["amount"] for a in _adjustments(app)] == [-5000]


def test_merging_in_a_copy_with_the_missing_transactions_explains_the_gap(app):
    _first_file(app)
    _second_file(app)
    assert len(_adjustments(app)) == 1
    # The missing download went into an account of its own (a new account number, say).
    path = write(app.test_downloads, "copy.ofx", ofx(SEPTEMBER + MISSING, "2650.00", "20260930", acct_id="000999888777"))
    call(app, "set_file_accounts", str(path), [{"index": 0, "name": "Copy"}])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    out = call(app, "merge_accounts", accounts["Copy"], accounts["SAMPLE CU Chequing"])
    assert (out["moved"], out["dropped"]) == (2, 2)
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 260000 and _start(app) == 105000
    assert "Copy comes back as an account of its own" in call(app, "undo_step")["undoes"]


def test_an_existing_database_learns_where_its_balances_came_from(tmp_path):
    path = tmp_path / "budget.db"
    database = db.Database(path)
    c = database.conn
    c.execute("INSERT INTO accounts (id, name, kind, anchor_date, anchor_balance) VALUES "
              "(1, 'From a file', 'chequing', '2026-10-10', 500), (2, 'An older file', 'chequing', '2026-10-01', 700), "
              "(3, 'Typed', 'cash', '2026-10-05', 900), (4, 'No balance', 'savings', NULL, NULL)")
    c.execute("INSERT INTO imports (id, filename, sha256, account_id, format, anchor_after_date, anchor_after_balance) "
              "VALUES (1, 'a.ofx', 'x', 1, 'ofx', '2026-10-10', 500), (2, 'b.ofx', 'y', 2, 'ofx', NULL, NULL), "
              "(3, 'c.csv', 'z', 3, 'tangerine_bank', NULL, NULL)")
    c.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
    database.close()
    again = db.Database(path)
    rows = {r["id"]: (r["anchor_source"], r["check_from"]) for r in again.conn.execute("SELECT * FROM accounts")}
    assert rows == {1: ("file", 1), 2: ("file", None), 3: ("typed", None), 4: (None, None)}
    assert again.get_meta("schema_version") == "8"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v7-to-v8-*.db"))
