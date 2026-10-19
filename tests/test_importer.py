"""Reading Tangerine exports and importing them without duplicates."""

import importer
from helpers import CARD_FILE, bank_csv, call, card_csv, card_number, categories, txns, write


def test_formats_are_detected_from_the_content(tmp_path):
    assert importer.detect(card_csv([])) == "tangerine_card"
    assert importer.detect(bank_csv([])) == "tangerine_bank"
    assert importer.detect("Account,Date,Payee,Notes,Category_Group,Category,Amount,Split_Amount,Cleared\n") == "actual"
    assert importer.detect("OFXHEADER:100\nDATA:OFXSGML\n<OFX>") == "ofx"
    assert importer.detect("Date,Description,Debit,Credit\n") is None


def test_card_rows_and_which_card(tmp_path):
    path = write(tmp_path, CARD_FILE.replace(".CSV", "-3.CSV"), card_csv([
        ("10/08/2026", "SAMPLE CAFE CALGARY AB", "-7.01", "Restaurant"),
        ("10/07/2026", "AMZN Mktp CA TORONTO ON", "52.15", "Other"),
    ]))
    parsed = importer.parse(path)
    assert parsed.format == "tangerine_card"
    [part] = parsed.parts
    assert part.account.kind == "credit_card" and part.account.last4 == "1234"
    assert part.account.keys[0] == "tangerine-card:1234" and part.account.bank == "Tangerine"
    assert [(r.date, r.amount, r.bank_category) for r in parsed.rows] == [
        ("2026-10-08", -701, "Restaurant"), ("2026-10-07", 5215, "Other")]


def test_bank_account_comes_from_the_file_name(tmp_path):
    for name in ("Chequing.CSV", "Chequing-2.CSV", "Chequing (1).csv"):
        parsed = importer.parse(write(tmp_path, name, bank_csv([("05/01/2026", "Interest Paid", "Interest Paid", "0.03")])))
        assert parsed.parts[0].account.keys == ["tangerine-bank:chequing"]
        assert parsed.parts[0].account.name == "Chequing"
    savings = importer.parse(write(tmp_path, "Savings.CSV", bank_csv([])))
    assert savings.parts[0].account.kind == "savings"


def test_identical_rows_in_one_file_are_both_kept(app):
    path = write(app.test_downloads, CARD_FILE, card_csv([
        ("09/20/2026", "SQ *SAMPLE COFFEE CALGARY", "-5.75", "Restaurant"),
        ("09/20/2026", "SQ *SAMPLE COFFEE CALGARY", "-5.75", "Restaurant"),
    ]))
    result = call(app, "import_files", [str(path)])["results"][0]
    assert (result["added"], result["duplicates"]) == (2, 0)


def test_overlapping_files_import_each_row_once(app):
    first = write(app.test_downloads, CARD_FILE, card_csv([
        ("08/02/2026", "SAMPLE HARDWARE CALGARY AB", "-18.30", "Other"),
        ("08/01/2026", "SQ *SAMPLE COFFEE CALGARY", "-5.75", "Restaurant"),
        ("08/01/2026", "SQ *SAMPLE COFFEE CALGARY", "-5.75", "Restaurant"),
    ]))
    call(app, "import_files", [str(first)])
    second = write(app.test_downloads, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([
        ("08/03/2026", "SAMPLE BOOKS CALGARY AB", "-12.00", "Other"),
        ("08/02/2026", "SAMPLE HARDWARE CALGARY AB", "-18.30", "Other"),
        ("08/01/2026", "SQ *SAMPLE COFFEE CALGARY", "-5.75", "Restaurant"),
        ("08/01/2026", "SQ *SAMPLE COFFEE CALGARY", "-5.75", "Restaurant"),
        ("08/01/2026", "SQ *SAMPLE COFFEE CALGARY", "-5.75", "Restaurant"),  # a third one that day
    ]))
    result = call(app, "import_files", [str(second)])["results"][0]
    assert (result["added"], result["duplicates"]) == (2, 3)
    assert len(txns(app)) == 5
    assert len(call(app, "accounts")) == 1  # both files are the same card


def test_imported_files_move_out_of_downloads(app):
    path = write(app.test_downloads, "Chequing.CSV", bank_csv([("05/01/2026", "Interest Paid", "Interest Paid", "0.03")]))
    call(app, "import_files", [str(path)])
    assert not path.exists()
    archived = list((app.data_dir / "Imported Files").rglob("*Chequing.CSV"))
    assert len(archived) == 1


OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD
<BANKACCTFROM><BANKID>0000<ACCTID>000123456789<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260901<DTEND>20260930
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260908120000[-5:EST]<TRNAMT>-171.25<FITID>A1<NAME>EFT Withdrawal to SAMPLE UTILI<MEMO>EFT Withdrawal to SAMPLE UTILITY</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260914<TRNAMT>1987.65<FITID>A2<NAME>EFT Deposit from SAMPLE EMPLOYER</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>1500.25<DTASOF>20260930</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_ofx_rows_ids_and_balance(app):
    path = write(app.test_downloads, "Statement.QFX", OFX)
    parsed = importer.parse(path)
    assert parsed.format == "ofx"
    assert [(r.date, r.amount, r.fitid) for r in parsed.rows] == [("2026-09-08", -17125, "A1"), ("2026-09-14", 198765, "A2")]
    assert parsed.parts[0].balance == ("2026-09-30", 150025)
    call(app, "import_files", [str(path)])
    accounts = call(app, "accounts")
    assert accounts[0]["balance"] == 150025  # taken from the file
    again = write(app.test_downloads, "Statement-2.QFX", OFX)
    assert call(app, "import_files", [str(again)])["results"][0]["added"] == 0


def test_actual_export_notes_from_an_aggregator_are_cleaned():
    assert importer._actual_description("2%  SQ *SAMPLE COFFEE CALGARY AB Fast Food (Quick Service)") == \
        "SQ *SAMPLE COFFEE CALGARY AB"
    assert importer._actual_description("SAMPLE CAFE CALGARY AB") == "SAMPLE CAFE CALGARY AB"


ACTUAL = """Account,Date,Payee,Notes,Category_Group,Category,Amount,Split_Amount,Cleared
Mastercard,2026-07-30,Sample Cafe,SAMPLE CAFE CALGARY AB,Every Day Spending,Eating Out,-12.40,0,Cleared
Mastercard,2026-07-29,Sample Cafe,2%  SAMPLE CAFE CALGARY AB Restaurants,Every Day Spending,Eating Out,-10.50,0,Cleared
Mastercard,2026-07-28,Sample Phone,SAMPLE PHONE CO CALGARY,Monthly Bills,Phone,-39.00,0,Cleared
Mastercard,2026-07-03,Chequing,Tangerine Credit Card Payment,,,1250.00,0,Cleared
Mastercard,2026-06-16,Starting Balance,,Income,Starting Balances,-400.00,0,Cleared
"""


def test_learning_from_actual_matches_renamed_categories(app):
    ids = categories(app, {"Every Day Spending": ["Eating Out"]})
    call(app, "rename_category", ids["Eating Out"], "Food")
    path = write(app.test_downloads, "Actual export.csv", ACTUAL)
    out = call(app, "import_files", [str(path)])
    assert out["learnt"]["examples"] == 3
    assert out["learnt"]["categories_created"] == ["Phone"]  # "Eating Out" is Food now
    assert path.exists()  # learnt from, not moved
    card = write(app.test_downloads, CARD_FILE, card_csv([("10/01/2026", "SAMPLE CAFE CALGARY AB", "-9.99", "Restaurant")]))
    call(app, "import_files", [str(card)])
    [t] = txns(app)
    assert (t["category_name"], t["status"]) == ("Food", "done")


def ofx_file(org, rows, acct="SAVINGS", acct_id="000123456789", card=False, bankid="0000", balance="1500.25"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, memo, fitid)"""
    fi = f"<SIGNONMSGSRSV1><SONRS><FI><ORG>{org}<FID>00000</FI></SONRS></SIGNONMSGSRSV1>" if org else ""
    acct_block = f"<CCACCTFROM><ACCTID>{acct_id}</CCACCTFROM>" if card else \
        f"<BANKACCTFROM><BANKID>{bankid}<ACCTID>{acct_id}<ACCTTYPE>{acct}</BANKACCTFROM>"
    txns_ = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}<MEMO>{m}</STMTTRN>\n"
                    for d, a, n, m, f in rows)
    return (f"OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX>{fi}<BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"{acct_block}\n<BANKTRANLIST>\n{txns_}</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>20260930"
            f"</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


CHEQUING_ROWS = [
    ("09/01/2026", "SAMPLE GYM", "SAMPLE GYM", "-30.00"),
    ("09/03/2026", "INTERAC e-Transfer From: ALEX", "INTERAC e-Transfer From: ALEX SAMPLE", "25.00"),
    ("09/05/2026", "EFT Withdrawal to SAMPLE UTILI", "EFT Withdrawal to SAMPLE UTILITY", "-171.25"),
    ("09/08/2026", "Interest Paid", "Interest Paid", "0.03"),
]


def as_ofx_rows(rows, extra=()):
    out = [(f"{d[6:]}{d[:2]}{d[3:5]}", a, n, m, f"F{i}") for i, (d, n, m, a) in enumerate(rows)]
    return out + list(extra)


def test_tangerine_quicken_and_money_files_go_into_the_chequing_account(app):
    """Tangerine calls chequing SAVINGS in its OFX files; the file name says which account it is."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    new = ("20260930", "-4.50", "SAMPLE BAKERY", "SAMPLE BAKERY", "F9")
    qfx = write(app.test_downloads, "Chequing.QFX", ofx_file("Tangerine", as_ofx_rows(CHEQUING_ROWS, [new])))
    result = call(app, "import_files", [str(qfx)])["results"][0]
    assert (result["account_name"], result["new_account"], result["added"], result["duplicates"]) == \
        ("Chequing", False, 1, 4)
    money = write(app.test_downloads, "Chequing.OFX", ofx_file("Tangerine", as_ofx_rows(CHEQUING_ROWS, [new])))
    assert call(app, "import_files", [str(money)])["results"][0]["added"] == 0
    assert [a["name"] for a in call(app, "accounts")] == ["Chequing"]
    assert len(txns(app)) == 5


def test_tangerine_card_quicken_file_goes_into_the_card(app):
    """Tangerine sends its Mastercard's QFX as a bank statement (BANKACCTFROM, ACCTTYPE
    CREDITLINE, the full card number), named after the masked card number like its CSV.
    It used to read as chequing, so the "new account?" pop-up didn't offer the card."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("09/02/2026", "SAMPLE CAFE CALGARY AB", "-7.01", "Restaurant")])))])
    app.db.set_meta("onboarded", "1")
    rows = [("20260902", "-7.01", "SAMPLE CAFE CALGARY AB", "", "M1"), ("20260905", "-12.00", "SAMPLE SHOP", "", "M2")]
    qfx = write(app.test_downloads, CARD_FILE.replace(".CSV", ".QFX"), ofx_file(
        "Tangerine", rows, acct="CREDITLINE", acct_id=card_number("1234"), bankid="0614", balance="-19.01"))
    parsed = importer.parse(qfx)
    assert (parsed.parts[0].account.kind, parsed.parts[0].account.last4) == ("credit_card", "1234")
    out = call(app, "import_files", [str(qfx)])
    assert out["needs_setup"] == []
    [result] = out["results"]
    assert (result["account_name"], result["new_account"], result["added"], result["duplicates"]) == \
        ("Tangerine Mastercard", False, 1, 1)
    card = next(a for a in call(app, "accounts") if a["name"] == "Tangerine Mastercard")
    assert card["balance"] == -1901
    assert len(call(app, "accounts")) == 2


def test_a_tangerine_line_of_credit_file_is_debt_whatever_its_nickname(app):
    """Tangerine's file name only tells chequing from savings (both say SAVINGS):
    CREDITLINE is a line of credit even when its nickname doesn't say "credit"."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("09/02/2026", "SAMPLE CAFE CALGARY AB", "-7.01", "Restaurant")])))])
    app.db.set_meta("onboarded", "1")
    loc = write(app.test_downloads, "Home Reno.QFX", ofx_file(
        "Tangerine", [("20260910", "-250.00", "SAMPLE LUMBER", "", "L1")], acct="CREDITLINE", acct_id="0001234567"))
    part = importer.parse(loc).parts[0]
    assert (part.account.kind, part.account.name) == ("line_of_credit", "Home Reno")
    [asked] = call(app, "file_accounts", str(loc))["parts"]
    assert [c["name"] for c in asked["choices"]] == ["Tangerine Mastercard", "Chequing"]  # debt accounts first


def test_another_banks_card_sent_as_a_bank_statement_is_a_card(app):
    card = write(app.test_downloads, "Card.QFX", ofx_file(
        "Sample Credit Union", [("20260911", "-8.00", "SAMPLE DINER", "", "C1")], acct="CREDITLINE",
        acct_id=card_number("9876")))
    part = importer.parse(card).parts[0]
    assert (part.account.kind, part.account.name, part.account.last4) == ("credit_card", "Sample Credit Union card", "9876")
    loc = write(app.test_downloads, "Loc.QFX", ofx_file(
        "Sample Credit Union", [("20260911", "-8.00", "SAMPLE DINER", "", "C1")], acct="CREDITLINE", acct_id="0005678"))
    assert importer.parse(loc).parts[0].account.kind == "line_of_credit"


def test_a_renamed_download_finds_its_account_by_its_transactions(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    renamed = write(app.test_downloads, "download.qfx", ofx_file("Tangerine", as_ofx_rows(CHEQUING_ROWS)))
    result = call(app, "import_files", [str(renamed)])["results"][0]
    assert (result["account_name"], result["added"]) == ("Chequing", 0)
    # It learnt the account number: a later file with nothing in common still goes there.
    later = write(app.test_downloads, "export.qfx", ofx_file("Tangerine", [("20261005", "-9.00", "SAMPLE SHOP", "", "Z1")]))
    assert call(app, "import_files", [str(later)])["results"][0]["account_name"] == "Chequing"
    assert len(call(app, "accounts")) == 1


def test_another_banks_files_get_their_own_accounts(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("09/02/2026", "SAMPLE CAFE CALGARY AB", "-7.01", "Restaurant")])))])
    bank = write(app.test_downloads, "Statement.QFX", ofx_file(
        "Sample Credit Union", [("20260910", "-12.00", "SAMPLE SHOP", "", "B1")], acct="CHECKING", acct_id="0005678"))
    card = write(app.test_downloads, "Card.QFX", ofx_file(
        "Sample Credit Union", [("20260911", "-8.00", "SAMPLE DINER", "", "C1")], card=True, acct_id="0009876"))
    results = call(app, "import_files", [str(bank), str(card)])["results"]
    assert {(r["account_name"], r["new_account"]) for r in results} == \
        {("Sample Credit Union Chequing", True), ("Sample Credit Union card", True)}
    assert len(call(app, "accounts")) == 4


def _copy_of_chequing(app, extra):
    """The state version 1.0 left behind: a QFX imported into a second account."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    with app.db.write() as c:
        import ledger
        copy = ledger.create_account(c, "Savings", "savings", keys=["ofx:savings:6789"])
        for t in c.execute("SELECT * FROM transactions WHERE account_id != ?", (copy,)).fetchall():
            c.execute("INSERT INTO transactions (account_id, date, amount, payee_id, raw_name, raw_memo, fingerprint, "
                      "fitid, status, category_id, reason) VALUES (?,?,?,?,?,?,?,?,'review',NULL,'')",
                      (copy, t["date"], t["amount"], t["payee_id"], t["raw_name"], t["raw_memo"], t["fingerprint"],
                       "F" + str(t["id"])))
        c.execute("INSERT INTO transactions (account_id, date, amount, raw_name, fingerprint, fitid) "
                  "VALUES (?, ?, ?, ?, 'only-in-copy', 'F99')", (copy, *extra))
    return copy


def test_a_copied_account_is_spotted_and_merged(app):
    ids = categories(app, {"Bills": ["Utilities"]})
    copy = _copy_of_chequing(app, ("2026-09-30", -450, "SAMPLE BAKERY"))
    chequing = next(a for a in call(app, "accounts") if a["name"] == "Chequing")
    utility = next(t for t in txns(app, account_id=copy) if t["amount"] == -17125)
    call(app, "categorize", txn_ids=[utility["id"]], category_id=ids["Utilities"])

    dup = next(a for a in call(app, "accounts") if a["id"] == copy)["duplicate_of"]
    assert (dup["into_id"], dup["shared"], dup["total"]) == (chequing["id"], 4, 5)
    assert [a["type"] for a in call(app, "inbox")["alerts"]] == ["duplicate", "balance"]  # Chequing's, not the copy's

    out = call(app, "merge_accounts", copy, chequing["id"])
    assert (out["moved"], out["dropped"]) == (1, 4)
    assert [a["name"] for a in call(app, "accounts")] == ["Chequing"]
    rows = txns(app)
    assert len(rows) == 5
    kept = next(t for t in rows if t["amount"] == -17125)
    assert (kept["category_name"], kept["status"]) == ("Utilities", "done")  # what you filed on the copy is kept
    # The copy's key now finds Chequing, so the old copy's files can't recreate it.
    with app.db.read() as c:
        assert c.execute("SELECT account_id FROM account_sources WHERE key = 'ofx:savings:6789'").fetchone()[0] == \
            chequing["id"]
    call(app, "undo")
    assert len(call(app, "accounts")) == 2


def test_cards_and_bank_accounts_are_never_merged(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("09/02/2026", "SAMPLE CAFE CALGARY AB", "-7.01", "Restaurant")])))])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    result = app.dispatch("merge_accounts", [accounts["Tangerine Mastercard"], accounts["Chequing"]])
    assert not result["ok"] and "card" in result["error"]


def test_a_files_balance_corrects_one_typed_in_earlier_that_day(app):
    """A balance typed in on the 12th, before a deposit dated the 12th reached the bank's
    list: the Quicken file downloaded later knows better."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    [chequing] = call(app, "accounts")
    call(app, "set_balance", chequing["id"], 100000, "2026-10-12")
    late = [("20261012", "500.00", "SAMPLE DEPOSIT", "", "L1")]
    qfx = write(app.test_downloads, "Chequing.QFX", ofx_file("Tangerine", as_ofx_rows(CHEQUING_ROWS, late),
                                                             balance="1500.00").replace("20260930", "20261012"))
    call(app, "import_files", [str(qfx)])
    assert call(app, "accounts")[0]["balance"] == 150000
    # An older file doesn't overrule a newer balance.
    old = write(app.test_downloads, "Chequing-2.QFX", ofx_file("Tangerine", as_ofx_rows(CHEQUING_ROWS), balance="1.00"))
    call(app, "import_files", [str(old)])
    assert call(app, "accounts")[0]["balance"] == 150000


def test_a_file_for_an_unseen_account_asks_new_or_one_you_have(app):
    """Once the budget is set up, a file for an account the app hasn't seen asks before
    making one: a replaced card or a renumbered account has a new number."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    app.db.set_meta("onboarded", "1")
    other = write(app.test_downloads, "Statement.QFX", ofx_file(
        "Sample Credit Union", [("20260910", "-12.00", "SAMPLE SHOP", "", "B1")], acct="CHECKING", acct_id="0005678"))
    out = call(app, "import_files", [str(other)])
    assert out["results"] == [] and [f["needs"] for f in out["needs_setup"]] == ["new_account"]
    [part] = call(app, "file_accounts", str(other))["parts"]
    assert (part["account_id"], part["name"]) == (None, "Sample Credit Union Chequing")
    assert [c["name"] for c in part["choices"]] == ["Chequing"]
    [result] = call(app, "set_file_accounts", str(other), [{"index": 0, "account_id": part["choices"][0]["id"]}])["results"]
    assert (result["account_name"], result["added"]) == ("Chequing", 1)
    # It remembers the number: the next file from that account goes straight there.
    later = write(app.test_downloads, "Statement-2.QFX", ofx_file(
        "Sample Credit Union", [("20261001", "-5.00", "SAMPLE CAFE", "", "B2")], acct="CHECKING", acct_id="0005678"))
    [result] = call(app, "import_files", [str(later)])["results"]
    assert (result["account_name"], result["added"]) == ("Chequing", 1)
    assert len(call(app, "accounts")) == 1


def test_any_open_account_can_be_picked_when_the_file_looks_like_another_sort(app):
    """What a file says it is can be wrong (Tangerine's card QFX read as chequing): the
    pop-up offers every open account, the file's sort first, and your pick wins."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("09/02/2026", "SAMPLE CAFE CALGARY AB", "-7.01", "Restaurant")])))])
    app.db.set_meta("onboarded", "1")
    other = write(app.test_downloads, "Statement.QFX", ofx_file(
        "Sample Credit Union", [("20260910", "-12.00", "SAMPLE SHOP", "", "B1")], acct="CHECKING", acct_id="0005678"))
    [part] = call(app, "file_accounts", str(other))["parts"]
    assert [c["name"] for c in part["choices"]] == ["Chequing", "Tangerine Mastercard"]
    card = part["choices"][1]["id"]
    [result] = call(app, "set_file_accounts", str(other), [{"index": 0, "account_id": card}])["results"]
    assert (result["account_name"], result["added"]) == ("Tangerine Mastercard", 1)
    # Remembered by the number: the next file from it goes to the card on its own.
    later = write(app.test_downloads, "Statement-2.QFX", ofx_file(
        "Sample Credit Union", [("20261001", "-5.00", "SAMPLE CAFE", "", "B2")], acct="CHECKING", acct_id="0005678"))
    [result] = call(app, "import_files", [str(later)])["results"]
    assert (result["account_name"], result["added"]) == ("Tangerine Mastercard", 1)
    assert len(call(app, "accounts")) == 2


def test_a_new_account_from_downloads_waits_for_its_name(app):
    import os
    import time
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(CHEQUING_ROWS)))])
    app.db.set_meta("onboarded", "1")
    # No card yet, so a card's file has nothing it could be: no question.
    card = write(app.test_downloads, "Card.QFX", ofx_file(
        "Sample Credit Union", [("20260911", "-8.00", "SAMPLE DINER", "", "C1")], card=True, acct_id="0009876"))
    [result] = call(app, "import_files", [str(card)])["results"]
    assert (result["account_name"], result["new_account"]) == ("Sample Credit Union card", True)

    savings = write(app.test_downloads, "Savings.QFX", ofx_file(
        "Sample Credit Union", [("20260912", "50.00", "SAMPLE DEPOSIT", "", "S1")], acct="SAVINGS", acct_id="0004444"))
    os.utime(savings, (time.time() - 10, time.time() - 10))
    waiting = app.watch_downloads()  # found in Downloads: waits in the Inbox
    assert waiting["results"] == [] and [f["needs"] for f in waiting["needs_setup"]] == ["new_account"]
    assert [(f["filename"], f["needs"]) for f in call(app, "inbox")["files"]] == [("Savings.QFX", "new_account")]
    taken = app.dispatch("set_file_accounts", [str(savings), [{"index": 0, "name": "chequing"}]])
    assert not taken["ok"] and "already" in taken["error"]
    [result] = call(app, "set_file_accounts", str(savings), [{"index": 0, "name": "Rainy Day"}])["results"]
    assert (result["account_name"], result["added"]) == ("Rainy Day", 1)
    assert not savings.exists()  # moved into Imported Files, like any import from Downloads
    assert call(app, "inbox")["files"] == []
