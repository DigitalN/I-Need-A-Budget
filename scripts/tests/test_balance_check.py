# SPDX-License-Identifier: GPL-3.0-only
"""A file's balance never quietly moves the budget's starting balance: a difference between two
files' balances waits in the Inbox as a balance adjustment, worked out by what arrived (issue #43).
Made-up data only."""

import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

import db
import ledger
from helpers import bank_csv, call, card_number, categories, write
from test_card_signs import card_ofx, covers

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE CU", start=None):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid). `start`: the first day the file says it
    covers (DTSTART, YYYYMMDD), if it says."""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n"
            f"{covers(start, as_of)}{trn}"
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
    out = _import(app, "chequing-sep-a.ofx", ofx(MISSING[:1], "2900.00", "20260922"))
    assert [a["amount"] for a in _adjustments(app)] == [-20000]
    assert out["results"][0]["balance_note"] == "The balance adjustment waiting in the Inbox is now -$200.00"
    out = _import(app, "chequing-sep-b.ofx", ofx(MISSING[1:], "2700.00", "20260930"))
    assert _adjustments(app) == []
    assert out["results"][0]["balance_note"] == "The missing transactions arrived: the balance adjustment is gone"
    assert call(app, "accounts")[0]["balance"] == 260000


def test_removing_the_file_that_filled_the_gap_puts_the_adjustment_back(app):
    _first_file(app)
    _second_file(app)
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
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
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
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
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
    [question] = _adjustments(app)
    call(app, "keep_both", question["id"])
    [kept] = _adjustments(app)
    assert kept["id"] == question["id"] and kept["yours"] and "adjustment_question" not in kept
    # A later download changes nothing: both are yours, and they cancel out.
    _import(app, "chequing-3.ofx", ofx([("20261012", "-20.00", "SAMPLE BAKERY", "A7")], "2580.00", "20261012"))
    assert [a["id"] for a in _adjustments(app)] == [kept["id"]]


# ── what an import's balance note says (issue #138) ──
#
# It's worked out once the import is done: its rows in, and the balance adjustment worked out again. It's shown in the
# import's toast and in Accounts → Imported files.


def _income(app):
    return next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")


def test_an_older_files_balance_is_compared_once_the_adjustment_is_worked_out_again(app):
    _first_file(app)
    _second_file(app)
    out = _import(app, "chequing-sep-a.ofx", ofx(MISSING[:1], "2900.00", "20260922"))  # the bank's balance that day
    assert out["results"][0]["balance_note"] == "The balance adjustment waiting in the Inbox is now -$200.00"


def test_the_note_says_when_the_money_a_kept_adjustment_stood_for_arrives(app):
    _first_file(app)
    _second_file(app)
    income = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")
    call(app, "categorize", txn_ids=[_adjustments(app)[0]["id"]], category_id=income)
    out = _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
    assert out["results"][0]["balance_note"] == ("The $300.00 your balance adjustment stood for has arrived: "
                                                 "the Inbox asks whether to remove it")


def test_an_older_files_balance_that_still_disagrees_is_said(app):
    """Of the $300.00 October's balance is missing, $50.00 is a charge on Sep 17 that's in no file (so the Sep 25
    payment is $150.00). The file that fills the gap covers Sep 18–22: the bank had $2,850.00 on Sep 22, and once
    that file is in, the app has $2,900.00 that day. Both are said."""
    _first_file(app)
    _second_file(app)
    out = _import(app, "chequing-sep-a.ofx", ofx(MISSING[:1], "2850.00", "20260922", start="20260918"))
    assert out["results"][0]["balance_note"] == (
        "The file says the balance was $2,850.00 on Sep 22; the app has $2,900.00. "
        "The balance adjustment waiting in the Inbox is now -$200.00")
    assert call(app, "imports")[0]["balance_note"] == out["results"][0]["balance_note"]  # Accounts → Imported files


def test_the_note_says_so_when_a_kept_adjustments_money_arrives_in_two_files(app):
    _first_file(app)
    _second_file(app)
    call(app, "categorize", txn_ids=[_adjustments(app)[0]["id"]], category_id=_income(app))  # yours now
    # $100.00 of it: the app's +$100.00 waits on its own, and nothing asks yet.
    out = _import(app, "chequing-sep-a.ofx", ofx(MISSING[:1], "2900.00", "20260922"))
    assert out["results"][0]["balance_note"] == ("By the bank's balance, $100.00 more came in than the transactions "
                                                 "show: a balance adjustment is waiting in the Inbox")
    # The rest: the Inbox asks, and the note says so (it said "…is now $300.00").
    out = _import(app, "chequing-sep-b.ofx", ofx(MISSING[1:], "2700.00", "20260930"))
    [question] = [a for a in _adjustments(app) if not a["yours"]]
    assert question["reason"] == "The $300.00 your balance adjustment stood for has arrived: remove the adjustment?"
    assert out["results"][0]["balance_note"] == ("The $300.00 your balance adjustment stood for has arrived: "
                                                 "the Inbox asks whether to remove it")
    # A later download that changes nothing doesn't say it again.
    out = _import(app, "chequing-3.ofx", ofx([("20261012", "-20.00", "SAMPLE BAKERY", "A7")], "2580.00", "20261012"))
    assert out["results"][0]["balance_note"] == ""
    assert [a["id"] for a in _adjustments(app) if not a["yours"]] == [question["id"]]


def test_the_note_says_them_when_the_money_of_two_kept_adjustments_arrives(app, monkeypatch):
    _first_file(app)
    _second_file(app)  # −$300.00
    call(app, "categorize", txn_ids=[_adjustments(app)[0]["id"]], category_id=_income(app))
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    # Oct 20: an Oct 15 bakery charge comes in, and a $50.00 gym payment on Oct 17 doesn't.
    bakery = ("20261015", "-20.00", "SAMPLE BAKERY CALGARY AB", "A8")
    out = _import(app, "chequing-3.ofx", ofx([bakery], "2530.00", "20261020"))
    assert out["results"][0]["balance_note"] == ("By the bank's balance, $50.00 more went out than the transactions "
                                                 "show: a balance adjustment is waiting in the Inbox")
    [gym] = [a for a in _adjustments(app) if not a["yours"]]
    call(app, "categorize", txn_ids=[gym["id"]], category_id=_income(app))
    # One long download brings all of it.
    rows = MISSING + OCTOBER + [bakery, ("20261017", "-50.00", "SAMPLE GYM CALGARY AB", "A9")]
    out = _import(app, "chequing-4.ofx", ofx(rows, "2530.00", "20261020"))
    [question] = [a for a in _adjustments(app) if not a["yours"]]
    assert question["reason"] == "The $350.00 your balance adjustments stood for has arrived: remove them?"
    assert out["results"][0]["balance_note"] == ("The $350.00 your balance adjustments stood for has arrived: "
                                                 "the Inbox asks whether to remove them")
    call(app, "remove_adjustment", question["id"])
    assert _adjustments(app) == [] and _start(app) == 105000


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


def test_reconciling_adds_an_adjustment_that_waits_and_the_next_file_is_still_checked(app):
    """Check balance… adds a balance adjustment for the difference, waiting in the Inbox. It doesn't end the check
    (issue #137): October's file, whose balance is older than it, is still checked, and its gap waits on its own."""
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 295000)["adjusted"] == -5000  # Oct 12
    [adjustment] = _adjustments(app)
    assert adjustment["guess"] is None and adjustment["reason"] == "To match the balance the bank shows"
    _second_file(app)
    assert sorted(a["amount"] for a in _adjustments(app)) == [-30000, -5000]
    assert _start(app) == 105000


def test_merging_in_a_copy_with_the_missing_transactions_explains_the_gap(app):
    _first_file(app)
    _second_file(app)
    assert len(_adjustments(app)) == 1
    # The missing download went into an account of its own (a new account number, say).
    path = write(app.test_downloads, "copy.ofx", ofx(SEPTEMBER + MISSING, "2700.00", "20260930", acct_id="000999888777"))
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
    assert again.get_meta("schema_version") == "11"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v7-to-v11-*.db"))


def test_an_existing_database_keeps_where_each_file_says_it_starts(tmp_path):
    """Version 9 keeps the first day each file says it covers (issue #136). Imports from before don't know it: the
    check uses their first row's date, and what they recorded stays as it was."""
    path = tmp_path / "budget.db"
    database = db.Database(path)
    c = database.conn
    c.execute("INSERT INTO accounts (id, name, kind, anchor_date, anchor_balance, anchor_source) "
              "VALUES (1, 'Chequing', 'chequing', '2026-10-10', 500, 'file')")
    c.execute("INSERT INTO imports (id, filename, sha256, account_id, format, first_date, last_date, anchor_after_date, "
              "anchor_after_balance) VALUES (1, 'a.ofx', 'x', 1, 'ofx', '2026-09-01', '2026-10-09', '2026-10-10', 500)")
    c.execute("UPDATE accounts SET check_from = 1")
    for suffix in ("ins", "upd", "del"):  # the undo triggers name every column
        c.execute(f"DROP TRIGGER IF EXISTS undo_imports_{suffix}")
    c.execute("ALTER TABLE imports DROP COLUMN covers_from")  # a version 8 database
    c.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    c.commit()
    database.close()
    again = db.Database(path)
    old = again.conn.execute("SELECT * FROM imports").fetchone()
    assert (old["filename"], old["first_date"], old["anchor_after_date"], old["covers_from"]) == \
        ("a.ofx", "2026-09-01", "2026-10-10", None)
    assert again.conn.execute("SELECT anchor_source, check_from FROM accounts").fetchone()[:] == ("file", 1)
    assert again.get_meta("schema_version") == "11"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v8-to-v11-*.db"))


# ── onboarding's 3. Balances (issue #135) ────────────────────────────────────

GAP = [("20261014", "-100.00", "SAMPLE HARDWARE CALGARY AB", "A4")]  # with the bank's $2,600.00 on Oct 20: $300.00 missing


def test_onboardings_balances_step_keeps_the_files_balance_checked(app):
    """The issue's case: Continue saves every box that has an amount, so the file's own balance came back as a
    typed one, and the next download's gap moved September's starting Ready to assign without a word."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    account = call(app, "accounts")[0]
    call(app, "set_balance", account_id=account["id"], cents=account["balance"], in_favour=False)  # Continue
    assert call(app, "undo_step") is None  # nothing changed, so there's nothing to undo
    call(app, "finish_onboarding", "2026-09")
    _import(app, "chequing-2.ofx", ofx(GAP, "2600.00", "20261020"))
    assert [a["amount"] for a in _adjustments(app)] == [-30000] and _start(app) == 105000


def test_a_changed_balance_on_a_files_row_is_typed_for_that_day(app):
    """3. Balances shows a file's balance for its own day ("Balance on Sep 15, 2026, from your file"): the same
    amount there changes nothing, and another is a typed balance for that day, not the day you onboard."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    account = call(app, "accounts")[0]
    assert (account["balance"], account["balance_date"]) == (300000, "2026-09-15")
    call(app, "set_balance", account_id=account["id"], cents=300000, date="2026-09-15")
    assert call(app, "undo_step") is None
    call(app, "set_balance", account_id=account["id"], cents=295000, date="2026-09-15")
    account = call(app, "accounts")[0]
    assert (account["anchor_source"], account["anchor_date"], account["balance"]) == ("typed", "2026-09-15", 295000)


def test_a_balance_is_for_the_day_of_the_last_transaction_it_counts(app):
    """A file whose rows go past its balance's date: the balance shown counts them, so it's for that later day."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER + MISSING[:1], "3000.00", "20260915"))
    account = call(app, "accounts")[0]
    assert (account["balance"], account["balance_date"]) == (290000, "2026-09-20")
    call(app, "set_balance", account_id=account["id"], cents=290000, date="2026-09-20")
    assert call(app, "undo_step") is None


CARD_SEPTEMBER = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260912", "-40.00", "SAMPLE CAFE", "S2")]
CARD_OCTOBER = [("20261005", "-30.00", "SAMPLE BAKERY", "O1")]


def _card_read_as_in_your_favour(app):
    """A bank that writes what you owe as a positive number: during onboarding its $120.50 is read as written."""
    _import(app, "card-1.ofx", card_ofx(CARD_SEPTEMBER, "120.50", "20260930"))
    [card] = call(app, "accounts")
    assert card["balance"] == 12050  # in your favour, as the file writes it
    return card


def test_unticking_in_your_favour_on_a_cards_file_balance_is_remembered(app):
    """Unticked on 3. Balances, the card's later files are read that way even when their balance disagrees with
    the app's, like the answer to "Is $120.50 what you owe…?". Without it, the next one with a gap was read as
    written, in your favour, and replaced your balance without a word."""
    card = _card_read_as_in_your_favour(app)
    call(app, "set_balance", account_id=card["id"], cents=12050, in_favour=False, date=card["balance_date"])
    call(app, "finish_onboarding", "2026-09")
    # The bank says $160.50 while the app has you owing $150.50: a $10.00 charge is in no file.
    _import(app, "card-2.ofx", card_ofx(CARD_OCTOBER, "160.50", "20261010"))
    assert call(app, "accounts")[0]["balance"] == -16050  # what you owe, not $160.50 in your favour


def test_a_cards_box_left_as_it_is_remembers_nothing(app):
    card = _card_read_as_in_your_favour(app)
    call(app, "set_balance", account_id=card["id"], cents=12050, in_favour=True, date=card["balance_date"])  # Continue
    call(app, "finish_onboarding", "2026-09")
    _import(app, "card-2.ofx", card_ofx(CARD_OCTOBER, "160.50", "20261010"))
    assert call(app, "accounts")[0]["balance"] == 16050  # as the file writes it: nothing was said about its sign


# ── the page: 3. Balances as you'd use it ────────────────────────────────────

_BALANCES_STEP = r'''
const ACCOUNTS = __ACCOUNTS__;
const out = { calls: [], toasts: [], html: '' };
const state = {};
const root = { hidden: true, innerHTML: '' };
let inputs = [];
function drawn(html) {  // the <input>s the page drew: boxes to type in and tick
  return [...html.matchAll(/<input([^>]*)>/g)].map(m => {
    const attrs = Object.fromEntries([...m[1].matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(a => [a[1], a[2] ?? '']));
    const dataset = Object.fromEntries(Object.entries(attrs).filter(([k]) => k.startsWith('data-'))
      .map(([k, v]) => [k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase()), v]));
    return { attrs, dataset, value: attrs.value ?? '', checked: 'checked' in attrs };
  });
}
const matching = sel => {
  const m = sel.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
  return el => !!m && m[1] in el.attrs && (m[2] === undefined || el.attrs[m[1]] === m[2]);
};
const $ = sel => sel === '#onboard' ? root : inputs.find(matching(sel)) || null;
const $$ = sel => inputs.filter(matching(sel));
const box = id => $(`[data-ob-balance="${id}"]`);
const favour = id => $(`[data-favour="${id}"]`);
async function call(name, args) {
  if (name === 'accounts') return ACCOUNTS;
  if (name === 'set_balance') { out.calls.push(args); return ACCOUNTS; }
  throw new Error('unexpected call: ' + name);
}
function toast(message) { out.toasts.push(message); }
__PAGE__
(async () => {
  ob.step = 2;
  await renderOnboarding();
  out.html = root.innerHTML;
  inputs = drawn(out.html);
  __EDIT__
  renderOnboarding = async () => {};  // 4. Start isn't under test
  await onboardingAction({ dataset: { ob: 'save-balances' } });
  console.log(JSON.stringify(out));
})();
'''


def _balances_step(accounts, edit=""):
    """Node: the page draws onboarding's 3. Balances from `accounts` (the service's answer), `edit` runs (JS: what
    you'd change there, with box(id) and favour(id)), then Continue is clicked. Returns the step's HTML and the
    set_balance calls Continue made, to send to the service."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node")
    page = "\n".join(re.search(rf"^{pattern}", PAGE, re.S | re.M).group(0) for pattern in (
        r"const esc = [^\n]*$", r"const moneyPlain = [^\n]*$", r"const dayYear = [^\n]*$", r"const MAX_CENTS = [^\n]*$",
        r"function parseMoney\(.*?^}$", r"const favourBox = [^\n]*$", r"const inFavour = [^\n]*$",
        r"async function loadAccounts\(\) [^\n]*$", r"const ob = [^\n]*$",
        r"async function renderOnboarding\(\) \{.*?^}$", r"async function onboardingAction\(btn\) \{.*?^}$"))
    script = _BALANCES_STEP.replace("__ACCOUNTS__", json.dumps(accounts)).replace("__PAGE__", page).replace("__EDIT__", edit)
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)


def test_continue_on_3_balances_leaves_a_files_balance_and_its_check(app):
    """The way the page does it (#132's tests skipped this step): 3. Balances draws each box from `accounts`, says
    where its balance came from, and Continue saves every box that has an amount. Left as it is, nothing changes."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    got = _balances_step(call(app, "accounts"))
    assert "The balances from your files are filled in: change one only if it's wrong." in got["html"]
    assert "Balance on Sep 15, 2026, from your file" in got["html"] and "For the others" not in got["html"]
    for args in got["calls"]:
        call(app, "set_balance", **args)
    assert call(app, "undo_step") is None
    call(app, "finish_onboarding", "2026-09")
    _import(app, "chequing-2.ofx", ofx(GAP, "2600.00", "20261020"))
    assert [a["amount"] for a in _adjustments(app)] == [-30000] and _start(app) == 105000


def test_a_box_changed_on_3_balances_is_the_balance_on_its_rows_day(app):
    """Typed over a file's balance, the amount is for the day the row names, not the day you onboard. Unticking a
    card's In your favour there is remembered: its next file is read as what you owe though its balance disagrees."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    _import(app, "card-1.ofx", card_ofx(CARD_SEPTEMBER, "120.50", "20260930"))
    ids = {a["is_debt"]: a["id"] for a in call(app, "accounts")}
    got = _balances_step(call(app, "accounts"), f"box({ids[False]}).value = '2,950.00'; favour({ids[True]}).checked = false;")
    assert "Amount owing on Sep 30, 2026, from your file" in got["html"]
    for args in got["calls"]:
        call(app, "set_balance", **args)
    accounts = {a["id"]: a for a in call(app, "accounts")}
    chequing = accounts[ids[False]]
    assert (chequing["anchor_source"], chequing["anchor_date"], chequing["balance"]) == ("typed", "2026-09-15", 295000)
    call(app, "finish_onboarding", "2026-09")
    _import(app, "card-2.ofx", card_ofx(CARD_OCTOBER, "160.50", "20261010"))
    assert {a["id"]: a for a in call(app, "accounts")}[ids[True]]["balance"] == -16050


def test_3_balances_asks_what_the_bank_shows_now_for_an_account_without_one(app):
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    call(app, "import_files", [str(write(app.test_downloads, "Savings.CSV",
                                        bank_csv([("09/02/2026", "SAMPLE INTEREST", "SAMPLE INTEREST", "1.25")])))])
    accounts = call(app, "accounts")
    got = _balances_step(accounts)
    assert ("The balances from your files are filled in: change one only if it's wrong. "
            "For the others, enter what your bank shows now.") in got["html"]
    assert "Balance · transactions up to Sep 2, 2026" in got["html"]
    assert [c["account_id"] for c in got["calls"]] == [a["id"] for a in accounts if a["balance"] is not None]


# ── 3. Balances: what a card's box says, and a row you typed (issue #143) ────
#
# On a file's row, *In your favour* changed says how the card's files write a balance above zero, like the two answers
# to "Is $120.50 what you owe…?"; changing only the amount says nothing. Any other row is what the bank shows now, a
# balance you typed on an earlier visit too (onboarding starts again at step 1 on every launch until it's finished).

def _csv(app, name, rows):
    return call(app, "import_files", [str(write(app.test_downloads, name, bank_csv(rows)))])


def _continue(app, accounts, edit=""):
    """3. Balances drawn from `accounts`, `edit` done there, Continue: its set_balance calls go to the service."""
    got = _balances_step(accounts, edit)
    for args in got["calls"]:
        call(app, "set_balance", **args)
    return got


def test_changing_only_the_amount_of_a_cards_box_that_shows_owing_remembers_nothing(app):
    """The issue's first case. The card's file balance ($120.50 on Sep 30) is read as in your favour, but the file has
    a charge after that day, so 3. Balances shows owing $9.50 on Oct 2, In your favour unticked. Changing only the
    amount unticks nothing: how its files write a balance above zero stays unsettled."""
    _import(app, "card-1.ofx", card_ofx(CARD_SEPTEMBER + [("20261002", "-130.00", "SAMPLE GROCER", "S3")], "120.50", "20260930"))
    [card] = call(app, "accounts")
    assert (card["balance"], card["balance_date"]) == (-950, "2026-10-02")
    got = _continue(app, [card], f"box({card['id']}).value = '10.00';")
    assert "Amount owing on Oct 2, 2026, from your file" in got["html"]
    call(app, "finish_onboarding", "2026-09")
    # A $20.00 payment; the bank's next file says $5.00 while the app has $10.00 in your favour (a $5.00 gap): neither
    # sign agrees, so it's read as the file writes it, as the first file was.
    _import(app, "card-2.ofx", card_ofx([("20261005", "20.00", "PAYMENT - THANK YOU", "P1")], "5.00", "20261010"))
    assert call(app, "accounts")[0]["balance"] == 500  # not owing $5.00


def test_unticking_in_your_favour_with_another_amount_is_remembered_too(app):
    """Unticking is what's remembered, whatever the amount: the box showed $120.50 in your favour, given as $125.00
    owing."""
    card = _card_read_as_in_your_favour(app)
    _continue(app, [card], f"box({card['id']}).value = '125.00'; favour({card['id']}).checked = false;")
    call(app, "finish_onboarding", "2026-09")
    # The bank says $160.50 while the app has you owing $155.00: $5.50 is in no file.
    _import(app, "card-2.ofx", card_ofx(CARD_OCTOBER, "160.50", "20261010"))
    assert call(app, "accounts")[0]["balance"] == -16050  # what you owe, not $160.50 in your favour


def test_unticking_a_box_a_payment_put_in_your_favour_remembers_nothing(app):
    """The file wrote what you owe below zero (owing $50.00 on Sep 30), and a $70.00 payment after it put the box at
    $20.00 in your favour. Unticking it there says nothing about a balance above zero in the card's files."""
    _import(app, "card-1.ofx", card_ofx(CARD_SEPTEMBER + [("20261002", "70.00", "PAYMENT - THANK YOU", "S3")], "-50.00", "20260930"))
    [card] = call(app, "accounts")
    assert (card["balance"], card["balance_date"]) == (2000, "2026-10-02")
    _continue(app, [card], f"favour({card['id']}).checked = false;")
    call(app, "finish_onboarding", "2026-09")
    # A $100.00 payment: the bank says $75.00 while the app has $80.00 in your favour ($5.00 is in no file).
    _import(app, "card-2.ofx", card_ofx([("20261005", "100.00", "PAYMENT - THANK YOU", "P1")], "75.00", "20261010"))
    assert call(app, "accounts")[0]["balance"] == 7500  # as the file writes it, not owing $75.00


def test_ticking_in_your_favour_back_on_a_files_row_is_remembered(app):
    """The other way round. On the first visit you untick the card's $120.50, so "owed" is remembered. On a later visit
    its next file says $100.00, read as owing because of that, and you tick In your favour: it's remembered, so a file
    whose balance disagrees with the app's is read as the file writes it again."""
    card = _card_read_as_in_your_favour(app)
    call(app, "set_balance", account_id=card["id"], cents=12050, in_favour=False, date=card["balance_date"])  # visit 1
    _import(app, "card-2.ofx", card_ofx([("20261005", "-20.50", "SAMPLE BAKERY", "O2")], "100.00", "20261010"))
    [card] = call(app, "accounts")
    assert (card["balance"], card["anchor_source"]) == (-10000, "file")
    got = _continue(app, [card], f"favour({card['id']}).checked = true;")  # visit 2
    assert "Amount owing on Oct 10, 2026, from your file" in got["html"]
    call(app, "finish_onboarding", "2026-09")
    # A $30.00 charge and a $10.00 one that's in no file: the bank says $60.00 while the app has $70.00 in your favour.
    _import(app, "card-3.ofx", card_ofx([("20261015", "-30.00", "SAMPLE CAFE", "O3")], "60.00", "20261020"))
    assert call(app, "accounts")[0]["balance"] == 6000  # not owing $60.00


def test_a_balance_typed_on_an_earlier_visit_is_todays_when_its_changed(app):
    """The issue's second case. Savings (only CSVs) was typed as $250.00 on Oct 1, onboarding left unfinished, and a
    later visit's CSV brings a $10.00 deposit on Oct 5. Today (Oct 12) the bank shows $255.00 (a $5.00 fee on Oct 8 is
    in no file yet): typed over the box, it's the balance today, as the step says, so the fee's file leaves it."""
    _csv(app, "Savings.CSV", [("09/20/2026", "SAMPLE INTEREST", "SAMPLE INTEREST", "1.25")])
    [savings] = call(app, "accounts")
    call(app, "set_balance", account_id=savings["id"], cents=25000, date="2026-10-01")  # the first visit
    _csv(app, "Savings 2.CSV", [("10/05/2026", "SAMPLE DEPOSIT", "SAMPLE DEPOSIT", "10.00")])
    got = _continue(app, call(app, "accounts"), f"box({savings['id']}).value = '255.00';")
    assert "Enter what your bank shows now for each account." in got["html"]
    assert "Balance · transactions up to Oct 5, 2026" in got["html"] and 'value="260.00"' in got["html"]
    assert got["calls"] == [{"account_id": savings["id"], "cents": 25500, "in_favour": False}]  # no day: today's
    _csv(app, "Savings 3.CSV", [("10/08/2026", "SAMPLE FEE", "SAMPLE FEE", "-5.00")])
    assert call(app, "accounts")[0]["balance"] == 25500  # not $250.00, as the balance on Oct 5 would leave it


def test_a_files_balance_you_changed_is_todays_when_you_come_back_to_it(app):
    """Changed on the step, a file's balance is a typed one: after Back (or on a later visit) its row is like a row
    without one, what the bank shows now. Left as it is, nothing changes; changed again, it's today's balance."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    [chequing] = call(app, "accounts")
    _continue(app, [chequing], f"box({chequing['id']}).value = '2,950.00';")
    accounts = call(app, "accounts")
    got = _continue(app, accounts)  # Back to 3. Balances, and Continue with the box as it is
    assert "Balance · transactions up to Sep 10, 2026" in got["html"] and "from your file" not in got["html"]
    assert call(app, "accounts") == accounts  # still $2,950.00 on Sep 15
    _continue(app, accounts, f"box({chequing['id']}).value = '2,940.00';")
    [chequing] = call(app, "accounts")
    assert (chequing["anchor_source"], chequing["anchor_date"], chequing["balance"]) == ("typed", "2026-10-12", 294000)


def test_in_your_favour_changed_on_a_row_you_typed_remembers_nothing(app):
    """A row you typed is what the bank shows now, and a card can really go from owing to in your favour (a payment):
    ticking it there says nothing about how the card's files are written, so the "owed" you unticked for stays."""
    card = _card_read_as_in_your_favour(app)
    call(app, "set_balance", account_id=card["id"], cents=12050, in_favour=False, date=card["balance_date"])  # visit 1
    [card] = call(app, "accounts")
    got = _continue(app, [card], f"box({card['id']}).value = '20.00'; favour({card['id']}).checked = true;")  # visit 2
    assert "Amount owing (the balance on the card) · transactions up to Sep 12, 2026" in got["html"]
    assert got["calls"] == [{"account_id": card["id"], "cents": 2000, "in_favour": True}]
    call(app, "finish_onboarding", "2026-09")
    # A $100.00 charge and a $5.00 one that's in no file: the bank says $85.00 while the app has you owing $80.00.
    _import(app, "card-2.ofx", card_ofx([("20261014", "-100.00", "SAMPLE HARDWARE", "O4")], "85.00", "20261020"))
    assert call(app, "accounts")[0]["balance"] == -8500  # still read as what you owe


# ── older history (issue #136) ──────────────────────────────────────────────
#
# A file reaching back before the first day of the file the check starts from brings older history: its rows dated
# before that day were in the starting balance already, not money that moved since.

AUGUST = [("20260805", "1000.00", "PAYROLL SAMPLE CO", "B1"), ("20260820", "-200.00", "SAMPLE GROCER CALGARY AB", "B2")]


def test_an_older_file_imported_later_was_in_the_starting_balance_already(app):
    """The issue's case: August's file after September's counted as money that moved since Sep 15, and a −$800.00
    balance adjustment waited for good (the budget's starting balance moving with it)."""
    _first_file(app)
    call(app, "set_budget_start", "2026-08")
    _import(app, "chequing-aug.ofx", ofx(AUGUST, "1050.00", "20260831"))
    assert _adjustments(app) == []
    assert call(app, "month", "2026-08")["summary"]["carried_rta"] == 25000  # the bank's balance on Jul 31
    _import(app, "chequing-oct.ofx", ofx([("20260920", "-100.00", "SAMPLE HARDWARE CALGARY AB", "C1")], "2900.00",
                                         "20261010"))
    assert _adjustments(app) == []


def test_an_older_file_after_a_starting_file_without_rows(app):
    """A quiet month's file (a balance, no transactions) can be the one the check starts from: without rows or a
    first day of its own it covers from its balance's date, and an older file after it adds nothing."""
    _import(app, "chequing-quiet.ofx", ofx([], "1050.00", "20260831"))
    call(app, "finish_onboarding", "2026-08")
    _import(app, "chequing-aug.ofx", ofx(AUGUST, "1050.00", "20260820"))
    assert _adjustments(app) == []
    assert call(app, "month", "2026-08")["summary"]["carried_rta"] == 25000


# A card's charge is dated when it's made, but its file covers the days things posted: the card's Oct 1–3 file
# has a Sep 30 taxi that posted Oct 1.
CARD_START = [("20260930", "-20.00", "SAMPLE TAXI", "K1"), ("20261002", "-30.00", "SAMPLE BAKERY", "K2")]


def test_a_card_charge_dated_before_the_starting_files_first_day_that_posted_after_it_counts(app):
    """The next file (Oct 1–12: it starts on Oct 1 too, not before) brings a Sep 29 parking charge that posted
    Oct 4. The Oct 3 balance doesn't have it, so it counts. By the starting file's first row (Sep 30) it wouldn't,
    and −$15.00 would wait for good."""
    _import(app, "card-1.ofx", card_ofx(CARD_START, "-50.00", "20261003", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    later = CARD_START + [("20260929", "-15.00", "SAMPLE PARKING", "K3"), ("20261010", "-40.00", "SAMPLE GROCER", "K4")]
    _import(app, "card-2.ofx", card_ofx(later, "-105.00", "20261012", start="20261001"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -10500


# September's charges, which posted in September: the card's Oct 1–3 balance ($250.00 owing) already has them.
CARD_SEPTEMBER_POSTED = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260930", "-25.00", "SAMPLE CAFE", "S2")]


def test_a_long_card_file_reaching_back_doesnt_count_what_the_starting_balance_had(app):
    """A 90-day file (Jul 15 – Oct 20) after the Oct 1–3 one: its Sep 30 coffee is dated on or after the starting
    file's first row (the Sep 30 taxi), but it posted Sep 30, so the Oct 3 balance had it. Only its Oct 15 charge is
    new."""
    _import(app, "card-oct.ofx", card_ofx(CARD_START, "-250.00", "20261003", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    long = CARD_SEPTEMBER_POSTED + CARD_START + [("20261015", "-10.00", "SAMPLE NEWS", "S3")]
    _import(app, "card-90.ofx", card_ofx(long, "-260.00", "20261020", start="20260715"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -26000


def test_the_cards_previous_months_file_adds_nothing(app):
    _import(app, "card-oct.ofx", card_ofx(CARD_START, "-250.00", "20261003", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    _import(app, "card-sep.ofx", card_ofx(CARD_SEPTEMBER_POSTED, "-200.00", "20260930", start="20260901"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -25000


def test_a_starting_file_without_rows_covers_from_the_day_it_says(app):
    """The card's Oct 1–2 file has no charges yet (a balance only). The next file brings a charge made Oct 1 that
    posted Oct 4: the Oct 2 balance doesn't have it, so it counts, though it's dated before that balance."""
    _import(app, "card-1.ofx", card_ofx([], "-200.00", "20261002", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    _import(app, "card-2.ofx", card_ofx([("20261001", "-15.00", "SAMPLE PARKING", "K3")], "-215.00", "20261012",
                                        start="20261001"))
    assert _adjustments(app) == []


def test_a_first_day_after_the_files_own_balance_says_nothing(app):
    """A bank writing nonsense: a file "starting" Oct 20 whose balance is for Oct 3. It covers from its first row's
    date (Sep 30) instead, and the next file's charges count."""
    _import(app, "card-1.ofx", card_ofx(CARD_START, "-50.00", "20261003", start="20261020"))
    call(app, "finish_onboarding", "2026-10")
    later = CARD_START + [("20261010", "-40.00", "SAMPLE GROCER", "K4")]
    _import(app, "card-2.ofx", card_ofx(later, "-90.00", "20261012", start="20261001"))
    assert _adjustments(app) == []


def test_merging_in_a_copy_with_older_history_adds_nothing(app):
    _first_file(app)
    # August's file went into an account of its own (a new account number, say).
    path = write(app.test_downloads, "copy.ofx", ofx(AUGUST, "1050.00", "20260831", acct_id="000999888777",
                                                      start="20260801"))
    call(app, "set_file_accounts", str(path), [{"index": 0, "name": "Copy"}])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    out = call(app, "merge_accounts", accounts["Copy"], accounts["SAMPLE CU Chequing"])
    assert (out["moved"], out["dropped"]) == (2, 0)
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 300000


def test_removing_the_starting_file_checks_from_the_next_files_first_day(app):
    """With the file the check starts from removed, the check starts from the next file's balance, and August's
    file, older than that one too, still adds nothing."""
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261010", start="20260916"))
    _import(app, "chequing-aug.ofx", ofx(AUGUST, "1050.00", "20260831", start="20260801"))
    assert _adjustments(app) == []
    first = next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-1.ofx")
    call(app, "undo_import", first)
    assert _adjustments(app) == []


# ── a starting file that stops before its balance's day (issue #146) ─────────
#
# A download of a past date range lists its transactions up to the range's last day (a Quicken file's DTEND), with the
# bank's balance on the day it was made: that balance has what went through in between already.


def _past(text, last_day):
    """A made-up download of a past date range: it says it covers up to `last_day` (YYYYMMDD), written as Tangerine
    writes it (the midnight after that day), and its balance is the bank's on the day it was made."""
    after = date(int(last_day[:4]), int(last_day[4:6]), int(last_day[6:])) + timedelta(days=1)
    return re.sub(r"<DTEND>\d{8}235959", f"<DTEND>{after:%Y%m%d}000000", text, count=1)


def test_a_starting_file_whose_rows_stop_before_its_balance_doesnt_count_them_twice(app):
    """The issue's case: two downloads on Oct 12, each with that day's balance ($2,600.00), for Sep 1–15 and Sep 16 –
    Oct 12. "$400.00 more came in" waited for good, and September's starting Ready to assign was $650.00."""
    first = ofx(SEPTEMBER, "2600.00", "20261012", start="20260901").replace("<DTEND>20261012", "<DTEND>20260915")
    _import(app, "chequing-a.ofx", first)
    _import(app, "chequing-b.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261012", start="20260916"))
    call(app, "finish_onboarding", "2026-09")
    assert _adjustments(app) == [] and _start(app) == 105000


# A card's statement (the days things went through, Sep 3 – Oct 2) and its current period (Oct 3–12), both downloaded
# Oct 12 with that day's balance (owing $140.00). Each has a charge made before its first day that went through in it.
CARD_STATEMENT = [("20260901", "-20.00", "SAMPLE TAXI", "C1"), ("20260910", "-40.00", "SAMPLE GROCER", "C2"),
                  ("20260930", "-25.00", "SAMPLE CAFE", "C3")]
CARD_CURRENT = [("20261001", "-15.00", "SAMPLE PARKING", "C4"), ("20261005", "-30.00", "SAMPLE BAKERY", "C5"),
                ("20261010", "-10.00", "SAMPLE BOOKS", "C6")]


def _card_statement(app):
    _import(app, "card-sep.qfx", _past(card_ofx(CARD_STATEMENT, "-140.00", "20261012", start="20260903"), "20261002"))
    call(app, "finish_onboarding", "2026-09")


def test_a_cards_statement_and_current_period_downloaded_the_same_day_add_nothing(app):
    """"$55.00 more came in" (the current period's charges) waited for good."""
    _card_statement(app)
    _import(app, "card-oct.qfx", card_ofx(CARD_CURRENT, "-140.00", "20261012", start="20261003"))
    assert _adjustments(app) == [] and call(app, "accounts")[0]["balance"] == -14000


def test_a_cards_download_the_same_day_that_overlaps_the_statement_goes_by_date(app):
    """Sep 20 – Oct 12, downloaded Oct 12 too (owing $125.00, as the statement's balance): it starts before the
    statement's last day, so what it brings dated after that day (Oct 5's and Oct 10's charges) was in that balance."""
    _import(app, "card-sep.qfx", _past(card_ofx(CARD_STATEMENT, "-125.00", "20261012", start="20260903"), "20261002"))
    call(app, "finish_onboarding", "2026-09")
    overlap = CARD_STATEMENT[2:] + CARD_CURRENT[1:]
    _import(app, "card-oct.qfx", card_ofx(overlap, "-125.00", "20261012", start="20260920"))
    assert _adjustments(app) == [] and call(app, "accounts")[0]["balance"] == -12500


def test_a_cards_current_period_downloaded_later_counts_by_its_days(app):
    """Accepted: a card goes by the days its downloads cover, never by a charge's date. With the current period
    downloaded only on Oct 20 (Oct 3–20, owing $160.00: a charge from Oct 12 that went through after the statement's
    download on top), its charges count, and the $55.00 the statement's balance had waits."""
    _card_statement(app)
    later = CARD_CURRENT + [("20261012", "-20.00", "SAMPLE GAS", "C7")]
    _import(app, "card-oct.qfx", card_ofx(later, "-160.00", "20261020", start="20261003"))
    assert [a["amount"] for a in _adjustments(app)] == [5500]


def _two_downloads_on_oct_12(app):
    """The issue's case, the way Tangerine writes a past range."""
    _import(app, "chequing-a.ofx", _past(ofx(SEPTEMBER, "2600.00", "20261012", start="20260901"), "20260915"))
    _import(app, "chequing-b.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261012", start="20260916"))
    call(app, "finish_onboarding", "2026-09")


ETRANSFER = [("20261012", "-45.00", "SAMPLE E-TRANSFER", "N1")]  # dated Oct 12, the day of the past range's balance
NEXT_WEEK = [("20261015", "-20.00", "SAMPLE CAFE CALGARY AB", "N2")]


def test_a_later_days_download_brings_what_went_through_after_the_balances_day_download(app):
    """With a download from Oct 12 in, what went through that day before it is in it. So the $45.00 e-transfer dated
    Oct 12 that Oct 20's download (Oct 1–20, $2,535.00) brings went through after it, and counts."""
    _two_downloads_on_oct_12(app)
    _import(app, "chequing-c.ofx", ofx(OCTOBER + ETRANSFER + NEXT_WEEK, "2535.00", "20261020", start="20261001"))
    assert _adjustments(app) == [] and _start(app) == 105000


def test_chequing_goes_by_date_with_a_download_that_ends_after_the_balances_day(app):
    """Sep 1–15 downloaded Oct 12 ($2,600.00), then only Sep 16 – Oct 20, on Oct 20 ($2,580.00): what it brings from
    before Oct 12 was in that balance, and Oct 15's charge counts."""
    _import(app, "chequing-a.ofx", _past(ofx(SEPTEMBER, "2600.00", "20261012", start="20260901"), "20260915"))
    call(app, "finish_onboarding", "2026-09")
    _import(app, "chequing-b.ofx", ofx(MISSING + OCTOBER + NEXT_WEEK, "2580.00", "20261020", start="20260916"))
    assert _adjustments(app) == [] and _start(app) == 105000


def _oct_12_only_in_that_balance(app, balance):
    """Sep 1–15 downloaded Oct 12, then only Sep 16 – Oct 20 on Oct 20 ($2,535.00), with the e-transfer dated Oct 12:
    no download covers Oct 12 but that balance."""
    _import(app, "chequing-a.ofx", _past(ofx(SEPTEMBER, balance, "20261012", start="20260901"), "20260915"))
    call(app, "finish_onboarding", "2026-09")
    _import(app, "chequing-b.ofx", ofx(MISSING + OCTOBER + ETRANSFER + NEXT_WEEK, "2535.00", "20261020", start="20260916"))


def test_on_the_balances_day_with_no_download_from_it_what_went_through_was_in_that_balance(app):
    """A balance covers its whole day: the e-transfer went through on Oct 12 before the download ($2,555.00 has it)."""
    _oct_12_only_in_that_balance(app, "2555.00")
    assert _adjustments(app) == [] and _start(app) == 105000


def test_on_the_balances_day_with_no_download_from_it_what_went_through_after_waits(app):
    """Accepted: dates can't say whether it went through before or after the download. After it (the bank's $2,600.00
    on Oct 12 doesn't have it), "$45.00 more went out" waits, and September starts at $1,095.00."""
    _oct_12_only_in_that_balance(app, "2600.00")
    [adjustment] = _adjustments(app)
    assert adjustment["amount"] == -4500 and "$45.00 more went out" in adjustment["reason"]
    assert _start(app) == 109500


def test_a_second_download_the_same_day_of_files_that_end_on_their_balances_day_counts(app):
    """Downloads that end on their balance's day change nothing: Sep 1 – Oct 12 at 9 a.m. ($2,600.00), then again at
    5 p.m. with a $20.00 charge in between ($2,580.00). By the first one's last transaction (Oct 3) instead of the last
    day it says it covers, the charge wouldn't count, and −$20.00 would wait."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER + MISSING + OCTOBER, "2600.00", "20261012", start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    later = SEPTEMBER + MISSING + OCTOBER + [("20261012", "-20.00", "SAMPLE CAFE CALGARY AB", "N3")]
    _import(app, "chequing-2.ofx", ofx(later, "2580.00", "20261012", start="20260901"))
    assert _adjustments(app) == [] and call(app, "accounts")[0]["balance"] == 258000


def test_a_later_downloads_row_from_the_balances_day_counts_when_the_starting_file_ends_on_it(app):
    """Only a file that stops early makes its balance's day count as in that balance: Sep 1–15, downloaded the morning
    of Sep 15 ($3,000.00), then Sep 15 – Oct 10 with a $30.00 lunch from that afternoon ($2,570.00 on Oct 10)."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915", start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    lunch = [("20260915", "-30.00", "SAMPLE CAFE CALGARY AB", "L1")]
    _import(app, "chequing-2.ofx", ofx(lunch + MISSING + OCTOBER, "2570.00", "20261010", start="20260915"))
    assert _adjustments(app) == [] and _start(app) == 105000


OCT_8 = [("20261008", "-50.00", "SAMPLE GROCER CALGARY AB", "G1")]  # in the bank's $2,550.00 on Oct 12


def _past_range_after_the_first_file(app):
    """The check starts from Sep 1–15 ($3,000.00 on Sep 15). Then Sep 16 – Oct 5, downloaded Oct 12 ($2,550.00): it
    stops before Oct 8's charge."""
    _first_file(app)
    _import(app, "chequing-2.ofx", _past(ofx(MISSING + OCTOBER, "2550.00", "20261012", start="20260916"), "20261005"))


def test_the_reason_says_a_download_may_be_missing_after_the_last_day_a_file_covers(app):
    """It said "between Sep 15 and Sep 20": that file's first transaction, though it says it covers from Sep 16, and
    nothing about the days after Oct 5."""
    _past_range_after_the_first_file(app)
    [adjustment] = _adjustments(app)
    assert adjustment["amount"] == -5000
    assert "a download may be missing between Oct 5 and Oct 12" in adjustment["reason"]


def test_the_starting_file_covers_up_to_its_balances_day_in_the_reason(app):
    """Sep 1–15 downloaded Oct 12, then Oct 1–10 downloaded Oct 20 ($2,580.00, with a $20.00 charge from Oct 15 in no
    file). Sep 16 – Oct 12 is in the first balance: no "between Sep 15 and Oct 1"."""
    _import(app, "chequing-a.ofx", _past(ofx(SEPTEMBER, "2600.00", "20261012", start="20260901"), "20260915"))
    call(app, "finish_onboarding", "2026-09")
    _import(app, "chequing-b.ofx", _past(ofx(OCTOBER, "2580.00", "20261020", start="20261001"), "20261010"))
    [adjustment] = _adjustments(app)
    assert adjustment["amount"] == -2000
    assert "a download may be missing between Oct 12 and Oct 20" in adjustment["reason"]


def test_a_starting_file_without_rows_says_where_a_download_may_be_missing_too(app):
    """The card's Oct 1–2 file has no charges (a balance only), then Oct 10–20: by the days the first one says it
    covers, nothing covers Oct 3–9. It said "a transaction may be missing, or counted twice"."""
    _import(app, "card-1.ofx", card_ofx([], "-200.00", "20261002", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    _import(app, "card-2.ofx", card_ofx([("20261012", "-15.00", "SAMPLE PARKING", "K3")], "-255.00", "20261020",
                                        start="20261010"))
    [adjustment] = _adjustments(app)
    assert adjustment["amount"] == -4000 and "a download may be missing between Oct 2 and Oct 10" in adjustment["reason"]


def test_removing_the_starting_file_checks_from_the_next_files_last_day(app):
    """Oct 1–12, downloaded Oct 12 too, brings Oct 8's charge. With Sep 1–15 removed, the check starts from Sep 16 –
    Oct 5, whose balance has that charge already."""
    _past_range_after_the_first_file(app)
    _import(app, "chequing-3.ofx", ofx(OCTOBER + OCT_8, "2550.00", "20261012", start="20261001"))
    assert _adjustments(app) == []
    first = next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-1.ofx")
    call(app, "undo_import", first)
    assert _adjustments(app) == [] and call(app, "accounts")[0]["balance"] == 255000


@pytest.mark.parametrize("dtend, last_day", [
    ("20261013000000.000[-5:EST]", "2026-10-12"),  # midnight is the end of the day before (Tangerine's)
    ("20261012143005.000[-5:EST]", "2026-10-12"),  # the moment it was downloaded
    ("20260916000000.000[-5:EST]", "2026-09-15"),  # a past range
    ("20261012", "2026-10-12"),                    # a day, no time
    ("20261016000000.000[-5:EST]", "2026-10-15"),  # after its own balance's day: kept, and the check ends on Oct 12
    ("20260815000000.000[-5:EST]", None),          # before its own first day: says nothing
    (None, None),                                  # it doesn't say
])
def test_how_a_files_last_day_is_read(app, dtend, last_day):
    text = ofx(SEPTEMBER, "2600.00", "20261012", start="20260901")
    _import(app, "chequing-1.ofx", re.sub(r"<DTEND>[^<\n]*", f"<DTEND>{dtend}" if dtend else "", text))
    assert app.db.conn.execute("SELECT covers_to FROM imports").fetchone()[0] == last_day
    # The check starting from it ends on the last day it covers, its balance's day at the latest (and when it doesn't say).
    span = ledger.check_span(app.db.conn, call(app, "accounts")[0]["id"])
    assert span.last_day == (min(last_day, "2026-10-12") if last_day else "2026-10-12")


def test_an_existing_database_gets_a_place_to_keep_the_last_day_each_file_covers(tmp_path):
    """Version 11 keeps the last day each file says it covers (issue #146). Imports from before don't know it: a check
    starting from one ends on its balance's day, as it did, and everything else stays as it was."""
    path = tmp_path / "budget.db"
    database = db.Database(path)
    c = database.conn
    c.execute("INSERT INTO accounts (id, name, kind, anchor_date, anchor_balance, anchor_source) "
              "VALUES (1, 'Chequing', 'chequing', '2026-10-12', 260000, 'file')")
    c.execute("INSERT INTO imports (id, filename, sha256, account_id, format, first_date, last_date, covers_from, "
              "anchor_after_date, anchor_after_balance) "
              "VALUES (1, 'a.ofx', 'x', 1, 'ofx', '2026-09-01', '2026-09-10', '2026-09-01', '2026-10-12', 260000)")
    c.execute("UPDATE accounts SET check_from = 1")
    for suffix in ("ins", "upd", "del"):  # the undo triggers name every column
        c.execute(f"DROP TRIGGER IF EXISTS undo_imports_{suffix}")
    c.execute("ALTER TABLE imports DROP COLUMN covers_to")  # a version 10 database
    c.execute("UPDATE meta SET value = '10' WHERE key = 'schema_version'")
    c.commit()
    database.close()
    again = db.Database(path)
    old = again.conn.execute("SELECT * FROM imports").fetchone()
    assert (old["filename"], old["first_date"], old["last_date"], old["covers_from"], old["anchor_after_date"],
            old["covers_to"]) == ("a.ofx", "2026-09-01", "2026-09-10", "2026-09-01", "2026-10-12", None)
    assert again.conn.execute("SELECT anchor_source, check_from FROM accounts").fetchone()[:] == ("file", 1)
    assert ledger.check_span(again.conn, 1).last_day == "2026-10-12"
    assert again.get_meta("schema_version") == "11"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v10-to-v11-*.db"))


# ── Check balance… (issue #137) ──────────────────────────────────────────────
#
# Check balance… doesn't end the check. Its balance adjustment stands for what's happened since the newest file's
# balance: the check leaves it alone until a file whose balance is as new as it comes in after it, which takes it
# over (it goes, or what that file doesn't explain stays as the account's balance adjustment). Categorized, it's
# yours, and counts as one you kept.


def test_a_waiting_adjustment_still_goes_after_a_reconcile(app):
    """The issue's case: Check balance… ended the check, so the −$300.00 was never worked out again, and once the
    missing September file arrived it counted twice (September started at $1,350.00)."""
    _first_file(app)
    _second_file(app)  # −$300.00 waits
    account = call(app, "accounts")[0]["id"]
    call(app, "reconcile", account, 259000)  # Oct 12: the bank shows $10.00 less
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
    assert [a["amount"] for a in _adjustments(app)] == [-1000] and _start(app) == 105000


# October's file without the gap: the bank's $2,600.00 on Oct 10. Check balance… on Oct 12 then finds the bank at
# $2,590.00: two charges from Oct 11 that posted after that file.
LATE = [("20261011", "-4.00", "SAMPLE PARKING CALGARY AB", "P1"), ("20261011", "-6.00", "SAMPLE COFFEE CALGARY AB", "P2")]
LATER = [("20261015", "-20.00", "SAMPLE BAKERY CALGARY AB", "P3")]


def _checked_on_oct_12(app):
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261010"))
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -1000
    return account


def test_a_later_file_that_explains_a_check_balance_adjustment_takes_it_away(app, monkeypatch):
    _checked_on_oct_12(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    out = _import(app, "chequing-3.ofx", ofx(LATE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000
    assert out["results"][0]["balance_note"] == "The missing transactions arrived: the balance adjustment is gone"


def test_an_existing_database_gets_a_place_to_remember_what_a_check_balance_came_after(tmp_path):
    """Version 10 remembers, on a balance adjustment from Check balance…, the newest import whose balance it came
    after (issue #137). Everything else stays as it was."""
    path = tmp_path / "budget.db"
    database = db.Database(path)
    c = database.conn
    c.execute("INSERT INTO accounts (id, name, kind, anchor_date, anchor_balance, anchor_source) "
              "VALUES (1, 'Chequing', 'chequing', '2026-10-10', 500, 'file')")
    c.execute("INSERT INTO transactions (id, account_id, date, amount, hint, reason) "
              "VALUES (1, 1, '2026-10-12', -1000, 'adjustment', 'To match the balance the bank shows')")
    for suffix in ("ins", "upd", "del"):  # the undo triggers name every column
        c.execute(f"DROP TRIGGER IF EXISTS undo_transactions_{suffix}")
    c.execute("ALTER TABLE transactions DROP COLUMN after_import")  # a version 9 database
    c.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    c.commit()
    database.close()
    again = db.Database(path)
    old = again.conn.execute("SELECT * FROM transactions").fetchone()
    assert (old["date"], old["amount"], old["hint"], old["after_import"]) == ("2026-10-12", -1000, "adjustment", None)
    assert again.get_meta("schema_version") == "11"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v9-to-v11-*.db"))


def test_a_later_file_that_doesnt_explain_it_leaves_the_rest_as_the_accounts_balance_adjustment(app, monkeypatch):
    """The two charges are in no file, but the bank's balance on Oct 20 has them: the check takes the −$10.00 over,
    and it waits as the account's balance adjustment, dated that balance's date."""
    _checked_on_oct_12(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(LATER, "2570.00", "20261020"))
    [adjustment] = _adjustments(app)
    assert (adjustment["amount"], adjustment["date"]) == (-1000, "2026-10-20")
    assert "By the bank's balance on Oct 20, $10.00 more went out" in adjustment["reason"]
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_a_categorized_check_balance_adjustment_asks_when_its_money_arrives(app, monkeypatch):
    _checked_on_oct_12(app)
    income = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=income)  # yours now
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    out = _import(app, "chequing-3.ofx", ofx(LATE + LATER, "2570.00", "20261020"))
    [question] = _adjustments(app)  # the app's, cancelling yours out
    assert question["amount"] == 1000 and question["adjustment_question"]["ids"] == [mine["id"]]
    assert question["reason"] == "The $10.00 your balance adjustment stood for has arrived: remove the adjustment?"
    assert out["results"][0]["balance_note"] == ("The $10.00 your balance adjustment stood for has arrived: "
                                                 "the Inbox asks whether to remove it")
    assert _start(app) == 105000
    call(app, "remove_adjustment", question["id"])
    assert _adjustments(app) == [] and mine["id"] not in [t["id"] for t in call(app, "transactions", {})["items"]]
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_check_balance_on_the_day_of_the_last_file_changes_the_balance_not_the_start(app):
    """Every real Quicken file's balance is dated the day it was downloaded. Check balance… that day was in that
    balance: the balance stayed $2,600.00, September's start moved to $1,060.00, and each Match it added another."""
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261012"))  # downloaded today
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -1000
    assert call(app, "accounts")[0]["balance"] == 259000 and _start(app) == 105000
    assert call(app, "reconcile", account, 259000)["adjusted"] == 0  # "It already matches"
    assert [a["amount"] for a in _adjustments(app)] == [-1000]


def test_a_later_file_the_same_day_takes_it_over(app):
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261012"))
    account = call(app, "accounts")[0]["id"]
    call(app, "reconcile", account, 259000)
    _import(app, "chequing-3.ofx", ofx(MISSING + OCTOBER + LATE, "2590.00", "20261012"))  # downloaded again that day
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 259000 and _start(app) == 105000


def test_check_balance_on_the_day_of_a_typed_balance(app):
    """The same for a balance you typed: the bank's balance later that day is that day's, and the days before
    stay as they were."""
    account = call(app, "add_account", "Sample Savings", "savings", 100000, "2026-10-12")
    call(app, "finish_onboarding", "2026-10")
    start = call(app, "month", "2026-10")["summary"]["carried_rta"]
    assert call(app, "reconcile", account, 99000)["adjusted"] == -1000
    assert call(app, "accounts")[0]["balance"] == 99000
    assert call(app, "month", "2026-10")["summary"]["carried_rta"] == start
    assert call(app, "reconcile", account, 99000)["adjusted"] == 0


def test_the_file_that_starts_the_check_takes_a_check_balance_adjustment_over(app, monkeypatch):
    """An account whose balance isn't checked (here a typed one): Check balance… finds the bank at $2,590.00 on
    Oct 12, −$110.00 from what the app has (the Oct 3 charge and the two from Oct 11, in no file yet). The next file's
    balance replaces the typed one and starts the check, so the app's adjustment goes: that file's balance has it."""
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 270000, "2026-09-30")  # typed: the check ends
    assert call(app, "reconcile", account, 259000)["adjusted"] == -11000
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    out = _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000
    assert out["results"][0]["balance_note"] == "The missing transactions arrived: the balance adjustment is gone"


def test_a_changed_box_on_3_balances_takes_the_apps_waiting_adjustment_away(app):
    """At onboarding the Sep 1–15 and October files disagree (−$300.00 waiting), and October's box is changed to
    $2,590.00. The balance you typed stands for everything, so the −$300.00 goes: September starts at $740.00 until
    the missing file arrives, then at $1,040.00, as the balance you typed says (it stayed, and counted twice:
    $1,340.00)."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    _import(app, "chequing-2.ofx", ofx(OCTOBER, "2600.00", "20261010"))
    assert [a["amount"] for a in _adjustments(app)] == [-30000]
    account = call(app, "accounts")[0]
    call(app, "set_balance", account_id=account["id"], cents=259000, date=account["balance_date"])
    call(app, "finish_onboarding", "2026-09")
    assert _adjustments(app) == [] and _start(app) == 74000
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
    assert _adjustments(app) == [] and _start(app) == 104000


def test_a_changed_balance_leaves_a_balance_adjustment_you_categorized(app):
    _first_file(app)
    _second_file(app)
    income = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=income)
    account = call(app, "accounts")[0]
    call(app, "set_balance", account_id=account["id"], cents=259000, date=account["balance_date"])
    assert mine["id"] in [t["id"] for t in call(app, "transactions", {})["items"]]


ONE_CHARGE = [("20261011", "-10.00", "SAMPLE PARKING CALGARY AB", "F1")]  # what the bank had on Oct 12, as one row


def test_the_banks_row_for_the_same_money_takes_a_check_balance_adjustments_place_under_its_own_name(app, monkeypatch):
    """A row of the same amount within 3 days takes the adjustment's place, as for a transaction you added by hand.
    It's the bank's transaction now, so it has the bank's name: it was left as "Balance adjustment"."""
    _checked_on_oct_12(app)
    [adjustment] = _adjustments(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(ONE_CHARGE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    row = next(t for t in call(app, "transactions", {})["items"] if t["id"] == adjustment["id"])
    assert (row["payee_name"], row["raw_name"], row["date"]) == ("Sample Parking", "SAMPLE PARKING CALGARY AB", "2026-10-11")
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_the_banks_row_keeps_the_category_you_gave_the_adjustment(app, monkeypatch):
    _checked_on_oct_12(app)
    income = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=income)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(ONE_CHARGE + LATER, "2570.00", "20261020"))
    row = next(t for t in call(app, "transactions", {})["items"] if t["id"] == mine["id"])
    assert (row["payee_name"], row["category_id"], row["status"]) == ("Sample Parking", income, "done")
    assert _adjustments(app) == [] and _start(app) == 105000


def test_undoing_check_balance_puts_the_balance_back(app):
    """Check balance… is a small step: undone straight away, the day's balance it changed included."""
    account = call(app, "add_account", "Sample Savings", "savings", 100000, "2026-10-12")
    call(app, "finish_onboarding", "2026-10")
    assert call(app, "reconcile", account, 99000)["adjusted"] == -1000
    assert call(app, "undo")["undone"] == "Balance adjustment"
    assert call(app, "accounts")[0]["balance"] == 100000 and _adjustments(app) == []


def test_removing_the_file_that_took_it_over_doesnt_bring_it_back(app, monkeypatch):
    """Accepted (decided on #137): the check works the difference out again from the files' balances. If the bank
    still shows something else, Check balance… again."""
    _checked_on_oct_12(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(LATE + LATER, "2570.00", "20261020"))
    call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-3.ofx"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 260000 and _start(app) == 105000


def test_removing_the_file_a_check_balance_came_after_still_lets_the_next_file_take_it_over(app, monkeypatch):
    """Remove import of October's file after Check balance…: the next file whose balance is as new as it still takes
    the adjustment over, though that file gets the removed one's number."""
    _checked_on_oct_12(app)
    call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-2.ofx"))
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_a_cards_check_balance_adjustment_goes_when_the_next_file_brings_the_charges(app, monkeypatch):
    """A card: you owe $500.00 on Oct 10 by its file, and $530.00 by the bank on Oct 12 (two charges made Oct 11 that
    posted after that file). The Oct 1–20 file brings them, so the −$30.00 goes."""
    _import(app, "card-1.ofx", card_ofx([("20261005", "-500.00", "SAMPLE GROCER", "C1")], "-500.00", "20261010",
                                        start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    card = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", card, 53000)["adjusted"] == -3000
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    later = [("20261005", "-500.00", "SAMPLE GROCER", "C1"), ("20261011", "-12.00", "SAMPLE CAFE", "C2"),
             ("20261011", "-18.00", "SAMPLE TAXI", "C4"), ("20261015", "-20.00", "SAMPLE BAKERY", "C3")]
    _import(app, "card-2.ofx", card_ofx(later, "-550.00", "20261020", start="20261001"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -55000


def test_a_categorized_adjustment_asks_when_the_file_that_starts_the_check_brings_its_money(app, monkeypatch):
    """An account whose balance isn't checked yet (a typed balance here; at the first launch of 1.4 every account whose
    balance came from a file, too): Check balance… on Oct 12 finds −$110.00 (the Oct 3 charge and the two from Oct 11),
    and you categorize it. The next file's balance starts the check, and that file brings the charges. It asks, as on an
    account already checked: it counted twice instead, and September started at $1,160.00."""
    _first_file(app)
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 270000, "2026-09-30")  # typed: the check ends
    assert call(app, "reconcile", account, 259000)["adjusted"] == -11000
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=food)  # yours now
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    out = _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    [question] = _adjustments(app)  # the app's, cancelling yours out
    assert question["amount"] == 11000 and question["adjustment_question"]["ids"] == [mine["id"]]
    assert question["reason"] == "The $110.00 your balance adjustment stood for has arrived: remove the adjustment?"
    assert out["results"][0]["balance_note"] == ("The $110.00 your balance adjustment stood for has arrived: "
                                                 "the Inbox asks whether to remove it")
    assert _start(app) == 105000
    call(app, "remove_adjustment", question["id"])
    assert _adjustments(app) == [] and mine["id"] not in [t["id"] for t in call(app, "transactions", {})["items"]]
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_removing_the_file_the_check_starts_from_still_asks_about_yours(app, monkeypatch):
    """Yours (−$410.00 from Oct 12) is asked about once the Oct 20 file brings its money. Remove import of the Sep
    file, the one the check started from: the check now starts from the Oct 20 file, whose balance has yours in it, so
    it still asks (the question went, and yours counted twice)."""
    _first_file(app)
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -41000
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=food)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-1.ofx"))
    [question] = _adjustments(app)
    assert question["amount"] == 41000 and question["adjustment_question"]["ids"] == [mine["id"]]

# ── a check that starts again (#145's review) ─────────────────────────────────
#
# Merge into… a copy whose newer balance wins, or Set balance and then the next file, start the check again from a
# newer balance. Every balance adjustment inside it from before is taken over alike: yours and the one you kept
# with Keep both still cancel out, and a question waiting about yours stays one question.


N1 = [("20261025", "-5.00", "SAMPLE CAFE CALGARY AB", "N1")]


def _yours_whose_money_arrived(app, monkeypatch):
    """Checked from the Sep file: Check balance… on Oct 12 finds −$410.00 (the late-September payments and October's,
    in no file yet), categorized; the Oct 20 file brings them, so the app asks whether to remove yours."""
    _first_file(app)
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -41000
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=food)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020", start="20260916"))
    [question] = _adjustments(app)
    assert question["adjustment_question"]["ids"] == [mine["id"]] and _start(app) == 105000
    return question


def _merge_in_a_newer_copy(app, monkeypatch):
    """The account's Oct 13–31 download went into an account of its own (another number); Merge into…: its newer
    balance wins, and its check with it."""
    monkeypatch.setenv("INAB_TODAY", "2026-11-01")
    path = write(app.test_downloads, "copy.ofx", ofx(N1, "2565.00", "20261031", acct_id="000999888777", start="20261013"))
    call(app, "set_file_accounts", str(path), [{"index": 0, "name": "Copy"}])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    call(app, "merge_accounts", accounts["Copy"], accounts["SAMPLE CU Chequing"])


def test_keep_both_still_holds_after_a_newer_copy_is_merged_in(app, monkeypatch):
    """You answered Keep both: yours and the one you kept cancel out. After the merge nothing asks again (#145: the app
    asks about yours a second time, and September starts at $640.00 whatever you answer)."""
    question = _yours_whose_money_arrived(app, monkeypatch)
    call(app, "keep_both", question["id"])
    _merge_in_a_newer_copy(app, monkeypatch)
    assert [(a["amount"], a["yours"], bool(a.get("adjustment_question"))) for a in _adjustments(app)] == [(41000, True, False)]
    assert _start(app) == 105000 and call(app, "accounts")[0]["balance"] == 256500


def test_keep_both_still_holds_after_set_balance_and_the_next_file(app, monkeypatch):
    """The same with Set balance (a changed balance ends the check; the next file starts it again). In the page only
    onboarding's 3. Balances can change a balance an account already has, so this one is reachable through the API only."""
    question = _yours_whose_money_arrived(app, monkeypatch)
    call(app, "keep_both", question["id"])
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 257500, "2026-10-20")  # typed, $5.00 out: the check ends
    monkeypatch.setenv("INAB_TODAY", "2026-11-01")
    _import(app, "chequing-3.ofx", ofx(N1, "2565.00", "20261031", start="20261021"))
    assert [(a["amount"], a["yours"], bool(a.get("adjustment_question"))) for a in _adjustments(app)] == [(41000, True, False)]
    assert _start(app) == 105000


def test_a_question_waiting_when_a_newer_copy_is_merged_in_counts_once(app, monkeypatch):
    """The question about yours is still waiting at the merge (#145's first commit: a second +$410.00 joined it, and
    September started at $640.00): its money counts once, and it still asks about yours."""
    question = _yours_whose_money_arrived(app, monkeypatch)
    _merge_in_a_newer_copy(app, monkeypatch)
    [waiting] = [a for a in _adjustments(app) if not a["yours"]]
    assert waiting["amount"] == 41000 and _start(app) == 105000
    assert (waiting.get("adjustment_question") or {}).get("ids") == question["adjustment_question"]["ids"]
