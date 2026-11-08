# SPDX-License-Identifier: GPL-3.0-only
"""One transaction, however many kinds of file it comes in: a re-set-up CSV layout, a bank's CSV and
its Quicken file, a posting date a day later. Made-up data only."""

from helpers import bank_csv, call, write


def ofx(rows, org="Tangerine", acct_id="000111222333", acct="SAVINGS", balance="500.00", as_of="20261031"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>{acct}</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _count(app, amount=None):
    items = call(app, "transactions", {})["items"]
    return len([t for t in items if amount is None or t["amount"] == amount])


def _set_up(app, filename, text, name, kind="chequing", bank="SAMPLE CU"):
    path = write(app.test_downloads, filename, text)
    setup = call(app, "csv_setup", str(path))
    return call(app, "setup_csv", str(path), setup["layout"], {"name": name, "kind": kind, "bank": bank})


OLD = """Date,Description,Debit,Credit
2026-10-01,SAMPLE GROCER,61.20,
2026-10-02,SAMPLE EMPLOYER PAY,,1500.00
2026-10-03,SAMPLE CAFE,4.50,
2026-10-04,SAMPLE BOOKS,22.00,
"""

# The bank added Memo and Balance columns, and the new set-up joins Description and Memo.
NEW = """Date,Description,Memo,Debit,Credit,Balance
2026-10-03,SAMPLE CAFE,PURCHASE 0123,4.50,,2000.00
2026-10-04,SAMPLE BOOKS,PURCHASE 0456,22.00,,1978.00
2026-10-05,SAMPLE REFUND,RETURN 0555,,10.00,1988.00
2026-10-05,SAMPLE GYM,PURCHASE 0789,30.00,,1958.00
"""


def test_a_layout_set_up_again_doesnt_import_the_overlap_twice(app):
    """Issue #59: the new set-up's descriptions differ, so fingerprints no longer match."""
    _set_up(app, "chequing.csv", OLD, "Sample Chequing")
    assert _count(app) == 4
    path = write(app.test_downloads, "chequing-2.csv", NEW)
    again = call(app, "csv_setup", str(path))
    layout = {**again["layout"], "desc_cols": [1, 2]}  # Description and Memo
    out = call(app, "setup_csv", str(path), layout, {"id": call(app, "accounts")[0]["id"]})
    [result] = out["results"]
    assert (result["added"], result["duplicates"]) == (2, 2)
    assert _count(app) == 6


def test_a_csv_account_and_a_quicken_account_of_the_same_bank_account_are_flagged_and_merge_cleanly(app):
    """Issue #59: with different wording nothing flagged them as copies, and merging doubled every row."""
    _set_up(app, "chequing.csv", OLD, "Sample Chequing")
    qfx = ofx([("20261001", "-61.20", "GROCER 0042", "Q1"), ("20261002", "1500.00", "PAYROLL DEPOSIT", "Q2"),
               ("20261003", "-4.50", "CAFE PURCHASE", "Q3"), ("20261004", "-22.00", "BOOKSTORE", "Q4")],
              org="Sample CU", acct_id="000999888777", acct="CHECKING")
    call(app, "import_files", [str(write(app.test_downloads, "Statement.qfx", qfx))])
    accounts = {a["name"]: a for a in call(app, "accounts")}
    assert len(accounts) == 2 and _count(app) == 8
    copy = next(a for a in accounts.values() if a["name"] != "Sample Chequing")
    assert copy["duplicate_of"] and copy["duplicate_of"]["into_id"] == accounts["Sample Chequing"]["id"]
    out = call(app, "merge_accounts", copy["id"], accounts["Sample Chequing"]["id"])
    assert (out["moved"], out["dropped"]) == (0, 4)
    assert _count(app) == 4 and len(call(app, "accounts")) == 1


def test_a_posting_date_a_day_later_isnt_a_new_transaction(app):
    """Issue #59: a CSV with transaction dates, then the QFX with posted dates a day later."""
    csv = bank_csv([("10/01/2026", "SAMPLE GROCER", "SAMPLE GROCER", "-61.20"),
                    ("10/03/2026", "SAMPLE CAFE", "SAMPLE CAFE", "-4.50")])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", csv))])
    qfx = ofx([("20261002", "-61.20", "SAMPLE GROCER", "Q1"), ("20261004", "-4.50", "SAMPLE CAFE", "Q2")])
    [result] = call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX", qfx))])["results"]
    assert (result["added"], result["duplicates"]) == (0, 2)
    assert _count(app) == 2


def test_a_date_further_away_or_a_row_already_matched_is_a_new_transaction(app):
    # The CSV runs past the QFX's first day: files that only meet can't hold one transaction twice (issue #124).
    csv = bank_csv([("10/03/2026", "SAMPLE CAFE", "SAMPLE CAFE", "-7.00"), ("10/01/2026", "SAMPLE GROCER", "SAMPLE GROCER", "-5.00")])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", csv))])
    qfx = ofx([("20261002", "-5.00", "SAMPLE GROCER", "Q1"),     # the same one, a day later
               ("20261002", "-5.00", "SAMPLE GROCER", "Q2"),     # a second one: nothing left to match
               ("20261008", "-5.00", "SAMPLE GROCER", "Q3")])    # a week later
    [result] = call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX", qfx))])["results"]
    assert (result["added"], result["duplicates"]) == (2, 1)
    assert _count(app, -500) == 3
