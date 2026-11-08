# SPDX-License-Identifier: GPL-3.0-only
"""A transaction added by hand is the same one the bank's file brings later, not a second one.
Made-up data only."""

from helpers import call, categories, write


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE BANK"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _id(added):
    return added["id"]


def _account(app):
    first = write(app.test_downloads, "chq-1.ofx", ofx([("20261001", "2000.00", "PAYROLL DEPOSIT SAMPLE CO", "C1")],
                                                       "2000.00", "20261001"))
    call(app, "import_files", [str(first)])
    call(app, "finish_onboarding", "2026-10")
    return call(app, "accounts")[0]["id"]


def _of(app, amount):
    return [t for t in call(app, "transactions", {})["items"] if t["amount"] == amount]


def _import(app, rows, balance="1920.00", as_of="20261031", name="chq-2.ofx"):
    return call(app, "import_files", [str(write(app.test_downloads, name, ofx(rows, balance, as_of)))])["results"][0]


def test_a_hand_added_transaction_isnt_imported_again(app):
    account = _account(app)
    call(app, "add_transaction", account, "2026-10-05", "SAMPLE GROCER", -8000)
    result = _import(app, [("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")], as_of="20261006")
    assert (result["added"], result["duplicates"]) == (0, 1)
    assert len(_of(app, -8000)) == 1


def test_the_hand_added_one_takes_the_banks_details_and_keeps_what_you_decided(app):
    ids = categories(app, {"Everyday": ["Food"]})
    account = _account(app)
    mine = _id(call(app, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000, ids["Food"], "for the party"))
    result = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    assert result["added"] == 0
    [row] = _of(app, -8000)
    assert row["id"] == mine
    assert (row["date"], row["raw_name"]) == ("2026-10-06", "SAMPLE GROCER CALGARY AB")  # the bank's
    assert (row["category_id"], row["note"], row["status"], row["payee_name"]) == (ids["Food"], "for the party", "done", "Sample Grocer")
    assert row["manual"] is False  # it has the bank's fingerprint and ID now
    # The same file again: nothing new, nothing matched twice.
    again = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")], name="chq-3.ofx")
    assert (again["added"], again["duplicates"]) == (0, 1) and len(_of(app, -8000)) == 1


def test_two_hand_added_a_day_apart_each_match_their_own_row(app):
    account = _account(app)
    first = _id(call(app, "add_transaction", account, "2026-10-05", "Sample Grocer", -8000))
    second = _id(call(app, "add_transaction", account, "2026-10-06", "Sample Grocer", -8000))
    result = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2"),
                           ("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C3")])
    assert (result["added"], result["duplicates"]) == (0, 2)
    rows = {t["id"]: t for t in _of(app, -8000)}
    assert set(rows) == {first, second}
    assert (rows[first]["date"], rows[second]["date"]) == ("2026-10-05", "2026-10-06")


def test_a_hand_added_row_five_days_away_or_of_another_amount_isnt_matched(app):
    account = _account(app)
    call(app, "add_transaction", account, "2026-10-01", "Sample Grocer", -8000)
    call(app, "add_transaction", account, "2026-10-05", "Sample Grocer", -7999)
    result = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    assert (result["added"], result["duplicates"]) == (1, 0)
    assert len(_of(app, -8000)) == 2


def test_a_hand_added_row_in_another_account_isnt_matched(app):
    _account(app)
    wallet = call(app, "add_account", "Wallet", "cash", 10000, "2026-09-30")
    call(app, "add_transaction", wallet, "2026-10-05", "Sample Grocer", -8000)
    result = _import(app, [("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    assert result["added"] == 1 and len(_of(app, -8000)) == 2


def test_the_banks_id_already_on_another_row_doesnt_break_the_match(app):
    """Banks repeat IDs (PR #90): the hand-added row takes the bank's details, but not an ID the
    account already has on another row, which is unique there."""
    account = _account(app)  # its payroll row has the ID C1
    mine = _id(call(app, "add_transaction", account, "2026-10-05", "SAMPLE GROCER", -8000))
    result = _import(app, [("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C1")], as_of="20261006")
    assert (result["added"], result["duplicates"]) == (0, 1)
    [row] = _of(app, -8000)
    assert row["id"] == mine and row["raw_name"] == "SAMPLE GROCER CALGARY AB"
