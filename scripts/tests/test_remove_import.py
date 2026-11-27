# SPDX-License-Identifier: GPL-3.0-only
"""Removing an import puts the balance back the way it was. Made-up data only."""

import pytest

import db
from helpers import call, write


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE CU"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _first_file(app):
    first = write(app.test_downloads, "chequing-1.ofx", ofx([("20260901", "2000.00", "PAYROLL SAMPLE CO", "A1")],
                                                            "3000.00", "20260915"))
    call(app, "import_files", [str(first)])
    call(app, "finish_onboarding", "2026-09")


def _second_file(app):
    second = write(app.test_downloads, "chequing-2.ofx", ofx([("20260916", "-100.00", "SAMPLE HARDWARE CALGARY AB", "A4")],
                                                             "2900.00", "20261010"))
    call(app, "import_files", [str(second)])


def _anchor(app):
    account = call(app, "accounts")[0]
    return account["anchor_date"], account["anchor_balance"]


def test_remove_import_puts_the_balance_back(app):
    """Issue #44: file 2's balance stayed, so September's starting Ready to assign dropped."""
    _first_file(app)
    before = call(app, "month", "2026-09")["summary"]["carried_rta"]
    _second_file(app)
    assert _anchor(app) == ("2026-10-10", 290000)
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-09-15", 300000)
    assert call(app, "month", "2026-09")["summary"]["carried_rta"] == before
    # One undo step: ⌘Z brings the file back, balance and all.
    call(app, "undo")
    assert _anchor(app) == ("2026-10-10", 290000)
    assert len(call(app, "transactions", {})["items"]) == 2


def test_a_balance_a_later_file_or_you_moved_since_is_left_alone(app):
    _first_file(app)
    _second_file(app)
    second_id = call(app, "imports")[0]["id"]
    third = write(app.test_downloads, "chequing-3.ofx", ofx([("20261011", "-50.00", "SAMPLE BAKERY", "A7")],
                                                            "2850.00", "20261012"))
    call(app, "import_files", [str(third)])
    call(app, "undo_import", second_id)  # the third file's balance is the account's now
    assert _anchor(app) == ("2026-10-12", 285000)
    # Removing the third then brings back what was there before it: the second file's balance is gone with
    # its file, so it's the first file's.
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-09-15", 300000)
    # A balance you typed after an import isn't undone by removing it.
    _second_file(app)
    call(app, "set_balance", call(app, "accounts")[0]["id"], 111100, "2026-10-11")
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-10-11", 111100)


def test_removing_the_accounts_first_import_takes_its_balance_with_it(app):
    _first_file(app)
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == (None, None)


def test_removing_an_import_brings_back_the_balance_you_typed_before_it(app):
    _first_file(app)
    call(app, "set_balance", call(app, "accounts")[0]["id"], 310000, "2026-09-20")
    _second_file(app)  # its balance is newer than the one typed
    assert _anchor(app) == ("2026-10-10", 290000)
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-09-20", 310000)


def test_an_existing_database_upgrades_with_imports_that_remember_nothing(tmp_path):
    path = tmp_path / "budget.db"
    database = db.Database(path)
    database.conn.execute("INSERT INTO imports (filename, sha256, format) VALUES ('old.ofx', 'abc', 'ofx')")
    for suffix in ("ins", "upd", "del"):  # the undo triggers name every column
        database.conn.execute(f"DROP TRIGGER IF EXISTS undo_imports_{suffix}")
    for column in ("anchor_before_date", "anchor_before_balance", "anchor_after_date", "anchor_after_balance"):
        database.conn.execute(f"ALTER TABLE imports DROP COLUMN {column}")  # a version 6 database
    database.conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    database.conn.commit()
    database.conn.close()
    again = db.Database(path)
    columns = [r["name"] for r in again.conn.execute("PRAGMA table_info(imports)")]
    assert {"anchor_before_date", "anchor_before_balance", "anchor_after_date", "anchor_after_balance"} <= set(columns)
    assert again.get_meta("schema_version") == str(db.SCHEMA_VERSION) == "14"
    old = again.conn.execute("SELECT * FROM imports").fetchone()
    assert (old["filename"], old["anchor_after_date"]) == ("old.ofx", None)  # removing it leaves the balance as it is
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v6-to-v14-*.db"))


# ── Transactions another imported file still has (issue #148) ────────────────────────────────
#
# An import stores only the rows its account doesn't have yet, so a row two downloads share is stored once. Remove import
# deletes only what no other imported file has: the rest stays, under the earliest file still imported that has it, the
# way that file has it (its date and wording), keeping what was decided about it: where it would be if the removed file
# had never come in.

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import test_balance_check as check
from helpers import bank_csv, categories
from test_card_signs import covers

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")
SHARED = [("20260912", "-40.00", "SAMPLE CAFE CALGARY AB", "S1")]


def _two_files(app):
    """Chequing's Sep 1–20 file and its Sep 10–30 file both have the $40.00 café charge on Sep 12 (the issue's case)."""
    check._import(app, "a.ofx", check.ofx(check.SEPTEMBER + SHARED, "2910.00", "20260920", start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx(SHARED + check.MISSING, "2610.00", "20260930", start="20260910"))


def _file(app, name):
    return next(i["id"] for i in call(app, "imports") if i["filename"] == name)


def _remove(app, name):
    return call(app, "undo_import", _file(app, name))


def _of(app, amount):
    return [t for t in call(app, "transactions", {})["items"] if t["amount"] == amount]


def _under(app):
    """Every transaction from a file, as (date, amount, the bank's wording, the file it's under)."""
    with app.db.read() as c:
        return Counter(tuple(r) for r in c.execute(
            "SELECT t.date, t.amount, t.raw_name, i.filename FROM transactions t JOIN imports i ON i.id = t.import_id "
            "WHERE t.fingerprint IS NOT NULL"))


def _dump(app):
    with app.db.read() as c:
        return {table: [tuple(r) for r in c.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid")]
                for table in ("transactions", "import_rows", "imports", "accounts", "splits", "payees")}


def _replay(folder, files):
    """A budget the same files went into, in the same order, without the removed one."""
    service = check._service(folder)
    for i, (name, text) in enumerate(files):
        check._import(service, name, text)
        if i == 0:
            call(service, "finish_onboarding", "2026-09")
    return service


def _what_the_check_sees(app):
    [account] = call(app, "accounts")
    return {"balance": account["balance"], "start": check._start(app),
            "waiting": sorted((a["amount"], a["date"]) for a in check._adjustments(app)), "under": _under(app)}


def test_removing_a_file_keeps_the_rows_a_later_file_also_has(app):
    """The issue's test."""
    shared = [("20260912", "-40.00", "SAMPLE CAFE CALGARY AB", "S1")]
    check._import(app, "a.ofx", check.ofx(check.SEPTEMBER + shared, "2910.00", "20260920", start="20260901"))
    check._import(app, "b.ofx", check.ofx(shared + check.MISSING, "2610.00", "20260930", start="20260910"))
    call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == "a.ofx"))
    assert -4000 in [t["amount"] for t in call(app, "transactions", {})["items"]]  # it went, before #148


def test_what_stays_moves_under_the_next_file_keeping_its_category(app):
    ids = categories(app, {"Everyday": ["Food"]})
    _two_files(app)
    [cafe] = _of(app, -4000)
    call(app, "categorize", [cafe["id"]], ids["Food"])
    assert {i["filename"]: (i["rows_new"], i["rows_total"]) for i in call(app, "imports")} == {"a.ofx": (3, 3), "b.ofx": (2, 3)}
    out = _remove(app, "a.ofx")
    assert (out["removed"], out["stayed"]) == (2, 1)  # September's pay and $50.00 café charge go; the $40.00 stays
    [kept] = _of(app, -4000)
    assert (kept["id"], kept["category_id"], kept["status"], kept["date"]) == (cafe["id"], ids["Food"], "done", "2026-09-12")
    assert _under(app) == Counter({("2026-09-12", -4000, "SAMPLE CAFE CALGARY AB", "b.ofx"): 1,
                                   ("2026-09-20", -10000, "SAMPLE PHONE CO", "b.ofx"): 1,
                                   ("2026-09-25", -20000, "SAMPLE INSURANCE CO", "b.ofx"): 1})
    # The file it's under now counts it in its "new" figure.
    assert {i["filename"]: (i["rows_new"], i["rows_total"]) for i in call(app, "imports")} == {"b.ofx": (3, 3)}


def test_removing_the_later_file_changes_nothing_the_earlier_one_has(app):
    _two_files(app)
    before = _under(app)
    out = _remove(app, "b.ofx")
    assert (out["removed"], out["stayed"]) == (2, 0)  # the two late-September payments only it has
    assert _under(app) == before - Counter({("2026-09-20", -10000, "SAMPLE PHONE CO", "b.ofx"): 1,
                                            ("2026-09-25", -20000, "SAMPLE INSURANCE CO", "b.ofx"): 1})


def test_undoing_it_puts_back_exactly_what_was_there(app):
    ids = categories(app, {"Everyday": ["Food"]})
    _two_files(app)
    call(app, "categorize", [_of(app, -4000)[0]["id"]], ids["Food"])
    before = _dump(app)
    _remove(app, "a.ofx")
    assert call(app, "undo_step")["undoes"] == "The 2 transactions from a.ofx come back."
    call(app, "undo")
    assert _dump(app) == before


def test_the_same_file_imported_twice_nothing_goes_and_the_other_takes_them_in_its_new_figure(app):
    text = check.ofx(check.SEPTEMBER + SHARED, "2910.00", "20260920", start="20260901")
    check._import(app, "a.ofx", text)
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "a.ofx", text)  # dropped again
    first, second = sorted(i["id"] for i in call(app, "imports"))
    assert {i["id"]: i["rows_new"] for i in call(app, "imports")} == {first: 3, second: 0}  # its Remove is off
    out = call(app, "undo_import", first)
    assert (out["removed"], out["stayed"]) == (0, 3)  # "Nothing removed: another file has all 3 transactions"
    assert [(i["id"], i["rows_new"]) for i in call(app, "imports")] == [(second, 3)]  # and its Remove is on


def test_a_balance_a_later_file_also_set_stays(app, tmp_path):
    """The same file imported twice, or two downloads made the same day: the later one set the same balance, so removing
    the earlier one leaves it (it took it back, leaving the account with none or an older one), as if the earlier one had
    never come in."""
    text = check.ofx(check.SEPTEMBER + SHARED, "2910.00", "20260920", start="20260901")
    check._import(app, "a.ofx", text)
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "a.ofx", text)
    first = min(i["id"] for i in call(app, "imports"))
    call(app, "undo_import", first)
    assert (call(app, "accounts")[0]["anchor_date"], call(app, "accounts")[0]["anchor_balance"]) == ("2026-09-20", 291000)
    assert _what_the_check_sees(app) == _what_the_check_sees(_replay(tmp_path / "replay", [("a.ofx", text)]))


def test_a_balance_a_later_download_the_same_day_also_set_stays(app, tmp_path):
    files = [("a.ofx", check.ofx(check.SEPTEMBER + SHARED, "2910.00", "20260920", start="20260901")),
             ("b.ofx", check.ofx(SHARED, "2910.00", "20260920", start="20260912"))]
    for i, (name, text) in enumerate(files):
        check._import(app, name, text)
        if i == 0:
            call(app, "finish_onboarding", "2026-09")
    _remove(app, "a.ofx")
    [account] = call(app, "accounts")
    assert (account["anchor_date"], account["anchor_balance"]) == ("2026-09-20", 291000)
    assert _what_the_check_sees(app) == _what_the_check_sees(_replay(tmp_path / "replay", files[1:]))


def test_a_transfer_that_stays_is_still_a_transfer(app):
    _two_files(app)
    wallet = call(app, "add_account", name="Wallet", kind="cash", balance=0)
    [cafe] = _of(app, -4000)
    call(app, "update_transaction", cafe["id"], {"transfer_account_id": wallet})
    with app.db.read() as c:
        before = c.execute("SELECT transfer_account_id, transfer_id, by_you FROM transactions WHERE id = ?",
                           (cafe["id"],)).fetchone()[:]
    _remove(app, "a.ofx")
    with app.db.read() as c:
        assert c.execute("SELECT transfer_account_id, transfer_id, by_you FROM transactions WHERE id = ?",
                         (cafe["id"],)).fetchone()[:] == before and before[0] == wallet


def test_the_check_counts_what_stays_from_the_file_its_under(app, tmp_path):
    """Removing the file the check starts from: it starts from the Sep 10–30 file, whose balance has the café charge,
    so nothing waits, as if the first file had never come in. Importing that file again adds nothing: before #148 the
    charge came back as money that arrived since, and +$40.00 waited."""
    _two_files(app)
    _remove(app, "a.ofx")
    later = check.ofx(SHARED + check.MISSING, "2610.00", "20260930", start="20260910")
    assert _what_the_check_sees(app) == _what_the_check_sees(_replay(tmp_path / "replay", [("b.ofx", later)]))
    assert check._adjustments(app) == []
    check._import(app, "b-again.ofx", later)
    assert check._adjustments(app) == [] and len(_of(app, -4000)) == 1


def qfx(rows, balance, as_of, start):
    """A made-up Tangerine Quicken download: the file's name says which account (Tangerine calls each one SAVINGS)."""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Tangerine"
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>000111222333<ACCTTYPE>SAVINGS</BANKACCTFROM>\n<BANKTRANLIST>\n"
            f"{covers(start, as_of)}{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _csv_then_quicken(app):
    """Chequing's CSV first (the café charge on Sep 30 as "SAMPLE CAFE"), then its Quicken file (on Oct 1 as "SAMPLE
    CAFE CALGARY AB"): the same transaction, matched by amount and date."""
    check._import(app, "Chequing.CSV", bank_csv([("09/28/2026", "SAMPLE BAKERY", "SAMPLE BAKERY", "-12.00"),
                                                 ("09/29/2026", "SAMPLE GROCER", "SAMPLE GROCER", "-61.20"),
                                                 ("09/30/2026", "SAMPLE CAFE", "SAMPLE CAFE", "-40.00")]))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "Chequing.QFX", qfx([("20260929", "-61.20", "SAMPLE GROCER CALGARY AB", "Q1"),
                                            ("20261001", "-40.00", "SAMPLE CAFE CALGARY AB", "Q2"),
                                            ("20261002", "-25.00", "SAMPLE HARDWARE CALGARY AB", "Q3")],
                                           "500.00", "20261002", start="20260929"))


def test_a_csv_and_its_quicken_file_what_stays_takes_the_quicken_files_date_and_wording(app):
    ids = categories(app, {"Everyday": ["Food"]})
    _csv_then_quicken(app)
    [cafe] = _of(app, -4000)
    assert (cafe["date"], cafe["raw_name"]) == ("2026-09-30", "SAMPLE CAFE")
    call(app, "categorize", [cafe["id"]], ids["Food"])
    csv = next(i["id"] for i in call(app, "imports") if i["filename"] == "Chequing.CSV")
    out = call(app, "undo_import", csv)
    assert (out["removed"], out["stayed"]) == (1, 2)  # the bakery only the CSV has
    [kept] = _of(app, -4000)
    assert (kept["id"], kept["date"], kept["raw_name"], kept["category_id"]) == \
        (cafe["id"], "2026-10-01", "SAMPLE CAFE CALGARY AB", ids["Food"])
    assert _under(app) == Counter({("2026-09-29", -6120, "SAMPLE GROCER CALGARY AB", "Chequing.QFX"): 1,
                                   ("2026-10-01", -4000, "SAMPLE CAFE CALGARY AB", "Chequing.QFX"): 1,
                                   ("2026-10-02", -2500, "SAMPLE HARDWARE CALGARY AB", "Chequing.QFX"): 1})
    # The next Quicken file, from a bank that numbers its rows afresh in every download, doesn't bring it twice.
    check._import(app, "Chequing.QFX", qfx([("20261001", "-40.00", "SAMPLE CAFE CALGARY AB", "R7"),
                                            ("20261003", "-9.00", "SAMPLE PARKING CALGARY AB", "R8")],
                                           "491.00", "20261003", start="20261001"))
    assert len(_of(app, -4000)) == 1 and len(_of(app, -900)) == 1


def test_a_row_the_bank_changed_between_downloads_takes_the_remaining_files_date(app):
    """Matched by the bank's own ID: posted two days after the first download had it."""
    check._import(app, "a.ofx", check.ofx([("20260912", "-40.00", "SAMPLE CAFE", "S1")], "960.00", "20260912",
                                          start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx([("20260914", "-40.00", "SAMPLE CAFE CALGARY AB", "S1")], "960.00", "20260920",
                                          start="20260910"))
    assert len(_of(app, -4000)) == 1
    _remove(app, "a.ofx")
    assert _under(app) == Counter({("2026-09-14", -4000, "SAMPLE CAFE CALGARY AB", "b.ofx"): 1})


def test_two_identical_rows_as_many_stay_as_the_remaining_file_has(app):
    coffee = ("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB")
    check._import(app, "a.ofx", check.ofx([(*coffee, "C1"), (*coffee, "C2")], "992.00", "20260912", start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx([(*coffee, "C1")], "992.00", "20260920", start="20260912"))
    out = _remove(app, "a.ofx")
    assert (out["removed"], out["stayed"]) == (1, 1)
    assert _under(app) == Counter({("2026-09-12", -400, "SAMPLE COFFEE CALGARY AB", "b.ofx"): 1})


def test_two_identical_rows_one_the_bank_redated_both_stay(app):
    """Two identical $4.00 coffees on Sep 12 in the first file. The bank's later download has one of them re-dated to
    Sep 14 and re-worded (its own ID kept) and the other as it was, which is the first coffee's fingerprint (the first of
    its kind that day). Each row is noted with its own coffee (the review of #148: both were noted with the first, so
    Remove import of the first file deleted the second, though the later file has it)."""
    coffee = ("-4.00", "SAMPLE COFFEE CALGARY AB")
    check._import(app, "a.ofx", check.ofx([("20260912", *coffee, "C1"), ("20260912", *coffee, "C2")], "992.00",
                                          "20260912", start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx([("20260914", "-4.00", "SAMPLE COFFEE CALGARY", "C1"),
                                           ("20260912", *coffee, "C2")], "992.00", "20260920", start="20260912"))
    out = _remove(app, "a.ofx")
    assert (out["removed"], out["stayed"]) == (0, 2)
    assert _under(app) == Counter({("2026-09-14", -400, "SAMPLE COFFEE CALGARY", "b.ofx"): 1,
                                   ("2026-09-12", -400, "SAMPLE COFFEE CALGARY AB", "b.ofx"): 1})


def test_what_stays_goes_under_the_earliest_file_that_has_it(app):
    """The same, with a third file that has both coffees as the first did: both go under the second file, the earliest
    that has them, so nothing waits (the second went under the third, and +$4.00 waited for good)."""
    coffee = ("-4.00", "SAMPLE COFFEE CALGARY AB")
    both = [("20260912", *coffee, "C1"), ("20260912", *coffee, "C2")]
    check._import(app, "a.ofx", check.ofx(both, "992.00", "20260912", start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx([("20260914", "-4.00", "SAMPLE COFFEE CALGARY", "C1"),
                                           ("20260912", *coffee, "C2")], "992.00", "20260920", start="20260912"))
    check._import(app, "c.ofx", check.ofx(both + [("20260925", "-10.00", "SAMPLE BAKERY", "C3")], "982.00", "20260925",
                                          start="20260912"))
    _remove(app, "a.ofx")
    assert [t for t in _under(app) if t[1] == -400 and t[3] != "b.ofx"] == []
    assert check._adjustments(app) == []


def test_a_second_purchase_that_looks_like_a_row_the_bank_redated_is_its_own(app):
    """At import (older than #148): the bank re-dated the first coffee to Sep 14 (its ID kept), and a second coffee,
    bought on Sep 12 too, has an ID of its own the account doesn't have. It's a transaction of its own (it was taken for
    the first coffee, lost, and waited as a -$4.00 balance adjustment instead)."""
    check._import(app, "a.ofx", check.ofx([("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "C1")], "996.00", "20260912",
                                          start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    out = check._import(app, "b.ofx", check.ofx([("20260914", "-4.00", "SAMPLE COFFEE CALGARY", "C1"),
                                                 ("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "C2")],
                                                "992.00", "20260920", start="20260912"))
    assert out["results"][0]["added"] == 1
    assert check._adjustments(app) == []


def test_a_file_with_only_the_second_of_two_identical_rows_has_that_one(app):
    """Two identical $4.00 coffees on Sep 12; the bank re-dates the first to Sep 14 when it posts, so a past range ending
    Sep 12 lists only the second. Its row's fingerprint finds the first coffee (the first of its kind that day), but its
    bank ID is the second's, which is the same as the row: it's noted with the second. Remove import of the first file
    then leaves the second under that download on Sep 12 and the first under the next one on Sep 14, as if the first
    file had never come in (both were dated Sep 12)."""
    coffee = ("-4.00", "SAMPLE COFFEE CALGARY AB")
    check._import(app, "a.ofx", check.ofx([("20260912", *coffee, "C1"), ("20260912", *coffee, "C2")], "992.00",
                                          "20260912", start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx([("20260912", *coffee, "C2")], "992.00", "20260912", start="20260905"))
    check._import(app, "c.ofx", check.ofx([("20260914", "-4.00", "SAMPLE COFFEE CALGARY", "C1"),
                                           ("20260912", *coffee, "C2")], "992.00", "20260916", start="20260910"))
    _remove(app, "a.ofx")
    assert sorted(t for t in _under(app) if t[1] == -400) == [("2026-09-12", -400, "SAMPLE COFFEE CALGARY AB", "b.ofx"),
                                                              ("2026-09-14", -400, "SAMPLE COFFEE CALGARY", "c.ofx")]


def test_a_bank_that_numbers_rows_afresh_never_swaps_two_different_transactions(app):
    """The row alone in its file takes the transaction its bank ID finds only when that one is the same as the row: a
    different $4.00 coffee the bank gave that number in an earlier download stays where it is."""
    check._import(app, "a.ofx", check.ofx([("20260910", "-4.00", "SAMPLE COFFEE CALGARY AB", "1"),
                                           ("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "2")], "992.00", "20260912",
                                          start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx([("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "1")], "992.00", "20260912",
                                          start="20260911"))
    with app.db.read() as c:
        noted = c.execute("SELECT t.date FROM import_rows r JOIN imports i ON i.id = r.import_id "
                          "JOIN transactions t ON t.id = r.transaction_id WHERE i.filename = 'b.ofx'").fetchall()
    assert [r[0] for r in noted] == ["2026-09-12"]


def test_two_rows_with_no_bank_id_to_tell_them_apart_stay_as_before(app):
    """Without a bank ID, a row the account already has stays the one transaction: nothing new is added."""
    check._import(app, "a.ofx", check.ofx([("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "C1")], "996.00", "20260912",
                                          start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    text = check.ofx([("20260914", "-4.00", "SAMPLE COFFEE CALGARY", "C1"),
                      ("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "X")], "996.00", "20260920", start="20260912")
    out = check._import(app, "b.ofx", text.replace("<FITID>X", ""))
    assert out["results"][0]["added"] == 0 and len(_of(app, -400)) == 1


def test_a_file_before_the_one_the_check_starts_from(app, tmp_path):
    """A CSV (no balance) first, then the Quicken file the check starts from, then one that also has the CSV's café
    charge: removing the CSV, the charge stays under that last file, older history to the check, so nothing waits, as
    if the CSV had never come in."""
    files = [("Chequing.CSV", bank_csv([("09/01/2026", "SAMPLE EMPLOYER PAY", "SAMPLE EMPLOYER PAY", "2000.00"),
                                        ("09/12/2026", "SAMPLE CAFE", "SAMPLE CAFE", "-40.00")])),
             ("Chequing.QFX", qfx([("20260916", "-100.00", "SAMPLE PHONE CO", "Q1")], "2860.00", "20260920",
                                  start="20260915")),
             ("Chequing.QFX", qfx([("20260912", "-40.00", "SAMPLE CAFE CALGARY AB", "Q5"),
                                   ("20260916", "-100.00", "SAMPLE PHONE CO", "Q1"),
                                   ("20260925", "-200.00", "SAMPLE INSURANCE CO", "Q6")], "2660.00", "20260930",
                                  start="20260910"))]
    for i, (name, text) in enumerate(files):
        check._import(app, name, text)
        if i == 0:
            call(app, "finish_onboarding", "2026-09")
    csv = next(i["id"] for i in call(app, "imports") if i["filename"] == "Chequing.CSV")
    out = call(app, "undo_import", csv)
    assert (out["removed"], out["stayed"]) == (1, 1)  # the pay goes; the café charge stays
    assert _what_the_check_sees(app) == _what_the_check_sees(_replay(tmp_path / "replay", files[1:]))
    assert check._adjustments(app) == []


def test_one_you_added_by_hand_that_another_file_has_stays(app):
    """What happens when no other file has it is #149's."""
    ids = categories(app, {"Everyday": ["Food"]})
    check._import(app, "a.ofx", check.ofx([("20261001", "2000.00", "PAYROLL SAMPLE CO", "C1")], "2000.00", "20261001",
                                          start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    account = call(app, "accounts")[0]["id"]
    mine = call(app, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000, ids["Food"], "for the party")["id"]
    grocer = [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")]
    check._import(app, "b.ofx", check.ofx(grocer, "1920.00", "20261006", start="20261002"))
    check._import(app, "c.ofx", check.ofx(grocer, "1920.00", "20261008", start="20261002"))
    _remove(app, "b.ofx")
    [kept] = _of(app, -8000)
    assert (kept["id"], kept["category_id"], kept["note"]) == (mine, ids["Food"], "for the party")
    assert _under(app)[("2026-10-06", -8000, "SAMPLE GROCER CALGARY AB", "c.ofx")] == 1


def test_after_merge_into_removing_the_accounts_own_file_keeps_what_the_copy_had(app):
    """The copy's file counts as having the transactions it shared with the account."""
    check._import(app, "chequing-1.ofx", check.ofx(check.SEPTEMBER, "3000.00", "20260915"))
    call(app, "finish_onboarding", "2026-09")
    check._new_account(app, "copy.ofx", check.ofx(check.SEPTEMBER + check.MISSING, "2700.00", "20260930",
                                                  acct_id=check.COPY, start="20260901"))
    check._merge(app, "Copy", "SAMPLE CU Chequing")
    out = _remove(app, "chequing-1.ofx")
    assert (out["removed"], out["stayed"]) == (0, 2)  # September's two: the copy's file has them
    assert _under(app) == Counter({("2026-09-01", 200000, "PAYROLL SAMPLE CO", "copy.ofx"): 1,
                                   ("2026-09-10", -5000, "SAMPLE CAFE CALGARY AB", "copy.ofx"): 1,
                                   ("2026-09-20", -10000, "SAMPLE PHONE CO", "copy.ofx"): 1,
                                   ("2026-09-25", -20000, "SAMPLE INSURANCE CO", "copy.ofx"): 1})


# ── files imported before 1.4.1 ──────────────────────────────────────────────────────────────

def _links(service):
    with service.db.read() as c:
        return sorted(tuple(r) for r in c.execute(
            "SELECT i.filename, t.amount, r.date, r.raw_name, r.fingerprint FROM import_rows r "
            "JOIN imports i ON i.id = r.import_id JOIN transactions t ON t.id = r.transaction_id"))


def _as_before_1_4_1(service):
    """What a budget from before 1.4.1 knows: only the file each transaction is under."""
    with service.db.write() as c:
        c.execute("DELETE FROM import_rows WHERE import_id IS NOT (SELECT import_id FROM transactions t "
                  "WHERE t.id = import_rows.transaction_id)")
    service.db.set_meta("read_imported_files", "1")


def test_the_first_launch_reads_the_kept_files_to_note_what_each_one_has(tmp_path):
    service = check._service(tmp_path)
    _two_files_in(service)
    _csv_then_quicken(service)
    links = _links(service)
    _as_before_1_4_1(service)
    missing = len(links) - len(_links(service))
    assert missing == 3  # b.ofx's café charge, and the Quicken file's grocer and café
    with service.db.read() as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM transactions ORDER BY id")]
    assert service.read_imported_files() == missing
    assert _links(service) == links
    with service.db.read() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM transactions ORDER BY id")] == before  # nothing else changed
    assert service.db.get_meta("read_imported_files") is None
    assert service.read_imported_files() == 0  # once
    out = call(service, "undo_import", next(i["id"] for i in call(service, "imports") if i["filename"] == "a.ofx"))
    assert (out["removed"], out["stayed"]) == (2, 1)


def _two_files_in(service):
    """The issue's two files, in a second account."""
    check._import(service, "a.ofx", check.ofx(check.SEPTEMBER + SHARED, "2910.00", "20260920", start="20260901"))
    check._import(service, "b.ofx", check.ofx(SHARED + check.MISSING, "2610.00", "20260930", start="20260910"))


def test_a_file_thats_no_longer_kept_or_was_changed_is_skipped(tmp_path):
    service = check._service(tmp_path)
    _two_files_in(service)
    _as_before_1_4_1(service)
    with service.db.read() as c:
        kept = c.execute("SELECT archived_path FROM imports WHERE filename = 'b.ofx'").fetchone()[0]
    Path(kept).write_text(Path(kept).read_text() + "\n", encoding="utf-8")  # not the file that was imported any more
    assert service.read_imported_files() == 0
    # One deleted from Imported Files since: the same.
    _as_before_1_4_1(service)
    Path(kept).unlink()
    assert service.read_imported_files() == 0
    out = call(service, "undo_import", next(i["id"] for i in call(service, "imports") if i["filename"] == "a.ofx"))
    assert (out["removed"], out["stayed"]) == (3, 0)  # as before 1.4.1


def test_the_upgrade_notes_each_transactions_own_file(tmp_path):
    for version in ("11", "7"):
        folder = tmp_path / f"v{version}"
        service = check._service(folder)
        _two_files_in(service)
        links = _links(service)
        c = service.db.conn
        for suffix in ("ins", "upd", "del"):
            c.execute(f"DROP TRIGGER IF EXISTS undo_import_rows_{suffix}")
        c.execute("DROP TABLE import_rows")  # a version 11 database (and the same tables as 7 for this)
        c.execute("UPDATE imports SET file_balance_date = NULL, file_balance = NULL")
        c.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (version,))
        service.db.close()
        again = db.Database(folder / "budget.db")
        assert again.get_meta("schema_version") == str(db.SCHEMA_VERSION) == "14"
        assert list((folder / "I Need A Budget Backups").glob(f"budget-before-upgrade-v{version}-to-v14-*.db"))
        assert again.get_meta("read_imported_files") == "1"
        with again.read() as c:
            own = c.execute("SELECT COUNT(*) FROM import_rows r JOIN transactions t ON t.id = r.transaction_id "
                            "AND t.import_id = r.import_id").fetchone()[0]
            files = c.execute("SELECT COUNT(*) FROM transactions WHERE fingerprint IS NOT NULL").fetchone()[0]
        assert own == files == 5  # every transaction from a file, under its own file
        from service import BudgetService
        from settings import Settings
        upgraded = BudgetService(again, Settings(folder / "settings.json"), folder / "data")
        upgraded.read_imported_files()
        assert _links(upgraded) == links
        with again.read() as c:
            assert c.execute("SELECT file_balance_date, file_balance FROM imports WHERE filename = 'a.ofx'").fetchone()[:] == \
                ("2026-09-20", 291000)
        again.close()


def test_a_new_budget_has_nothing_to_read(app):
    assert app.db.get_meta("read_imported_files") is None and app.read_imported_files() == 0


def test_the_app_reads_them_at_launch():
    main = (Path(__file__).resolve().parents[2] / "Application Files" / "main.py").read_text(encoding="utf-8")
    assert "read_imported_files" in main


# ── what Remove import says ──────────────────────────────────────────────────────────────────

def _removed_text(result):
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node")
    page = "\n".join(re.search(rf"^{p}", PAGE, re.S | re.M).group(0) for p in (
        r"const plural = [^\n]*$", r"const removedText = [^\n]*$"))
    script = page + f"\nprocess.stdout.write(JSON.stringify(removedText({json.dumps(result)})));"
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, encoding="utf-8", check=True).stdout)


def test_remove_imports_question_and_message_say_what_stays():
    assert "confirmBox('Remove this import?', 'Deletes the transactions only this file has.', 'Remove', true)" in PAGE
    assert _removed_text({"removed": 3, "stayed": 5}) == "Removed 3 transactions · 5 stay: another file has them"
    assert _removed_text({"removed": 3, "stayed": 1}) == "Removed 3 transactions · 1 stays: another file has it"
    assert _removed_text({"removed": 3, "stayed": 0}) == "Removed 3 transactions"
    assert _removed_text({"removed": 1, "stayed": 0}) == "Removed 1 transaction"
    assert _removed_text({"removed": 0, "stayed": 8}) == "Nothing removed: another file has all 8 transactions"
    assert _removed_text({"removed": 0, "stayed": 1}) == "Nothing removed: another file has its transaction"
    assert "toast(removedText(r), { action: undoAction })" in PAGE
    assert "Removing an import deletes the transactions only its file has." in PAGE  # above Imported files
    assert "not ones another file also had" not in PAGE


# ── random made-up histories ─────────────────────────────────────────────────────────────────
#
# One account's downloads, each covering the days before it was made (overlapping, sometimes with a gap, sometimes two
# the same day; identical rows, and rows the bank re-dates when they post), imported as they're made, with some
# transactions categorized by hand. Removing any one file leaves the budget as it would be had that file never come in
# (every transaction's date, amount, wording and file, the balance, the start and what waits in the Inbox), keeping what
# was decided about what stays; ⌘Z then puts back exactly what was there.

import random
from datetime import date, timedelta

NAMES = ["SAMPLE CAFE CALGARY AB", "SAMPLE GROCER CALGARY AB", "SAMPLE PHONE CO", "SAMPLE COFFEE CALGARY AB",
         "PAYROLL SAMPLE CO", "SAMPLE HARDWARE CALGARY AB"]


def _history(rng):
    """Downloads of one made-up account. Some transactions the bank re-dates and re-words two days later, keeping their
    ID (a pending charge that posted): a download made before that has it as it was, one made after as it posted. Some
    days have two identical rows, sometimes one of them re-dated. Some downloads are of a past range, ending a few days
    before the day they're made (with that day's balance)."""
    day0 = date(2026, 9, 1)
    truth = []
    for n in range(rng.randint(12, 28)):
        when = day0 + timedelta(days=rng.randint(0, 40))
        name = rng.choice(NAMES)
        amount = rng.randint(100, 9000) * (1 if name.startswith("PAYROLL") else -1)
        truth.append((when, amount, name, rng.random() < 0.15))
        if rng.random() < 0.2:
            truth.append((when, amount, name, rng.random() < 0.5))  # two identical rows on one day
    rows = []
    for i, (when, amount, name, redated) in enumerate(truth):
        rows.append((when, f"{amount / 100:.2f}", name, f"T{i}", when + timedelta(days=2) if redated else None))
    files, made = [], day0
    while made < date(2026, 10, 12) and len(files) < 6:
        made = min(made + timedelta(days=rng.choice([0, 3, 5, 8, 12])), date(2026, 10, 12))  # 0: the same day again
        start = max(day0, made - timedelta(days=rng.randint(2, 25)))
        last = max(start, made - timedelta(days=rng.randint(1, 4))) if rng.random() < 0.2 else made  # a past range
        mine = []
        for d, a, n, f, posted in rows:
            if posted is not None and posted <= made:
                d, n = posted, n.replace(" CALGARY AB", "") + " POSTED"
            if start <= d <= last:
                mine.append((d.strftime("%Y%m%d"), a, n, f))
        balance = 100000 + sum(round(float(a) * 100) for d, a, n, f, posted in rows if d <= made)
        text = check.ofx(mine, f"{balance / 100:.2f}", made.strftime("%Y%m%d"), start=start.strftime("%Y%m%d"))
        files.append((f"file-{len(files) + 1}.ofx", check._past(text, last.strftime("%Y%m%d")) if last < made else text))
    return files


def _categories(app):
    with app.db.read() as c:
        return {r["id"]: (r["category_id"], r["by_you"], r["note"]) for r in c.execute("SELECT * FROM transactions")}


@pytest.mark.parametrize("seed", range(30))
def test_removing_any_file_is_as_if_it_had_never_come_in(app, tmp_path, seed):
    rng = random.Random(seed)
    files = _history(rng)
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    for i, (name, text) in enumerate(files):
        check._import(app, name, text)
        if i == 0:
            call(app, "finish_onboarding", "2026-09")
    waiting = [t["id"] for t in call(app, "inbox")["items"] if t["payee_name"] != "Balance adjustment"]
    for txn_id in rng.sample(waiting, len(waiting) // 3):
        call(app, "categorize", [txn_id], food)
    for k, (name, _) in enumerate(files):
        before, decided = _dump(app), _categories(app)
        with app.db.read() as c:
            held = {r[0] for r in c.execute("SELECT id FROM transactions WHERE import_id = ?", (_file(app, name),))}
        out = _remove(app, name)
        replay = _replay(tmp_path / f"replay-{k}", [f for j, f in enumerate(files) if j != k])
        assert _what_the_check_sees(app) == _what_the_check_sees(replay), (seed, name)
        with app.db.read() as c:  # a new balance adjustment can take a deleted transaction's number
            after = {r["id"]: (r["category_id"], r["by_you"], r["note"])
                     for r in c.execute("SELECT * FROM transactions WHERE hint != 'adjustment'")}
        assert all(after[t] == decided[t] for t in after if t in decided), (seed, name)  # what was decided stays
        assert (out["removed"], out["stayed"]) == (len(held - set(after)), len(held & set(after))), (seed, name)
        call(app, "undo")
        assert _dump(app) == before, (seed, name)


@pytest.mark.parametrize("seed", range(20))
def test_after_merge_into_removing_any_file_is_as_from_one_account(tmp_path, monkeypatch, seed):
    """#147's random downloads, some under the copy's number, merged: then removing any one file leaves every transaction
    as removing it does where every file went into one account (its date, amount and the file it's under), with the
    same balance, balance adjustment and check start, and ⌘Z puts both back (issues #147 and #169). Both are what a
    fresh budget that got every other file has (#169's "Done when")."""
    from helpers import card_number
    rng = random.Random(seed)
    card = seed % 2 == 1
    own, copy = (card_number("4444"), card_number("5555")) if card else ("000111222333", check.COPY)
    files = check._random_downloads(rng, card)
    numbers = [own] + [rng.choice([own, copy]) for _ in files[1:]]
    if copy not in numbers:
        numbers[rng.randrange(1, len(files))] = copy
    monkeypatch.setenv("INAB_TODAY", "2026-11-01")
    one = check._service(tmp_path / "one")
    check._bring_in(one, files, [own] * len(files), card)
    two = check._service(tmp_path / "two")
    check._bring_in(two, files, numbers, card)
    names = {a["name"]: a["id"] for a in call(two, "accounts")}
    into, other = (f"Account {own[-4:]}", f"Account {copy[-4:]}")[::1 if rng.random() < 0.5 else -1]
    call(two, "merge_accounts", names[other], names[into])
    assert check._outcome(two) == check._outcome(one)
    for k in range(len(files)):
        before = check._outcome(one)
        outs = [_remove(budget, f"file-{k}.ofx") for budget in (one, two)]
        assert check._outcome(two) == check._outcome(one), (seed, k)
        assert outs[0]["stayed"] == outs[1]["stayed"], (seed, k)  # (`removed` counts its balance adjustment too)
        assert _money(two) == _money(_fresh(tmp_path / f"fresh-{k}", files, k, own, card)), (seed, k)
        for budget in (one, two):
            call(budget, "undo")
        assert check._outcome(one) == check._outcome(two) == before, (seed, k)


def _fresh(folder, files, skip, number, card):
    """A fresh budget that got every file but `skip`, in order, under one number, each by its own name."""
    app = check._service(folder)
    for j, f in enumerate(files):
        if j == skip:
            continue
        text = check._download(card, number, *f)
        if call(app, "accounts"):
            check._import(app, f"file-{j}.ofx", text)
        else:
            check._new_account(app, f"file-{j}.ofx", text, f"Account {number[-4:]}")
            call(app, "finish_onboarding", "2026-09")
    return app


def _books(app):
    """The account's balance (date, amount, where it came from) and the file its check starts from, by name."""
    [a] = app.db.conn.execute("SELECT * FROM accounts").fetchall()
    start = app.db.conn.execute("SELECT filename FROM imports WHERE id = ?", (a["check_from"],)).fetchone()
    return a["anchor_date"], a["anchor_balance"], a["anchor_source"], start[0] if start else None


def _money(app):
    """What a removal has to leave as a budget that never had the file would: the balance, the balance adjustment
    waiting, the start, every transaction (date and amount) and the account's balance and check start."""
    balance, waiting, start, rows = check._outcome(app)
    return balance, waiting, start, sorted((d, a) for d, a, _ in rows), _books(app)


def _play(folder, actions, card, number, skip=()):
    """A budget that got `actions` in order, leaving out the files in `skip`: ("file", k, download) or ["typed", cents,
    day], a balance you typed (3. Balances for the day of the account's balance, Set balance… for today). Returns None
    when a balance was typed before any file still there (no account to type it into)."""
    app = check._service(folder)
    made = False
    for act in actions:
        if act[0] == "file":
            if act[1] in skip:
                continue
            text = check._download(card, number, *act[2])
            if made:
                assert not check._import(app, f"file-{act[1]}.ofx", text).get("needs")
            else:
                check._new_account(app, f"file-{act[1]}.ofx", text, "Account")
                call(app, "finish_onboarding", "2026-09")
                made = True
        elif not made:
            return None
        else:
            cents, day = act[1], act[2]
            call(app, "set_balance", account_id=call(app, "accounts")[0]["id"], cents=abs(cents), date=day,
                 in_favour=bool(card and cents > 0))
    return app


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 21, 28, 58, 59, 72, 78])  # #171 before its review: 21 to 78 differed
def test_removing_two_files_with_balances_you_typed_between_them_is_as_if_they_had_never_come_in(tmp_path, monkeypatch,
                                                                                               seed):
    """The review of #171's histories: #147's random downloads in a random order, with balances you typed between
    them. Remove one file, then another: after each, the same as a fresh budget that got the same downloads and typed
    the same balances, without the removed files; the money check is never off, and ⌘Z twice puts it all back."""
    from helpers import card_number
    import ledger
    monkeypatch.setenv("INAB_TODAY", "2026-11-01")
    rng = random.Random(seed)
    card = seed % 2 == 1
    number = card_number("4444") if card else "000111222333"
    files = check._random_downloads(rng, card)
    order = list(range(len(files)))
    rng.shuffle(order)
    plan = []
    for k in order:
        plan.append(("file", k, files[k]))
        if rng.random() < 0.35:
            plan.append(("typed", rng.choice([-1, 1]) * rng.randint(1_00, 300_00), rng.random() < 0.4))
    # What you type is worked out once, from what the app shows on the day, then typed the same in every budget.
    actions, app = [], check._service(tmp_path / "app")
    for act in plan:
        if act[0] == "typed":
            accounts = call(app, "accounts")
            if not accounts:
                continue
            a = accounts[0]
            day = "2026-11-01" if act[2] or a["anchor_date"] is None else a["anchor_date"]
            act = ["typed", (ledger.account_balance(app.db.conn, a["id"], day) or 0) + act[1], day]
            call(app, "set_balance", account_id=a["id"], cents=abs(act[1]), date=day, in_favour=bool(card and act[1] > 0))
        elif not call(app, "accounts"):
            check._new_account(app, f"file-{act[1]}.ofx", check._download(card, number, *act[2]), "Account")
            call(app, "finish_onboarding", "2026-09")
        else:
            check._import(app, f"file-{act[1]}.ofx", check._download(card, number, *act[2]))
        actions.append(act)
    for k in range(len(files)):
        m = rng.choice([x for x in range(len(files)) if x != k])
        before = _dump(app)
        for step, gone in enumerate((k, m)):
            _remove(app, f"file-{gone}.ofx")
            fresh = _play(tmp_path / f"fresh-{k}-{m}-{step}", actions, card, number, skip={k, m} if step else {k})
            if fresh is not None:
                assert _money(app) == _money(fresh), (seed, k, m, step)
            assert not any(call(app, "month", month)["summary"].get("off") for month in ("2026-09", "2026-10", "2026-11"))
        call(app, "undo")
        call(app, "undo")
        assert _dump(app) == before, (seed, k, m)
