# SPDX-License-Identifier: GPL-3.0-only
"""Which account a bank file belongs to, when the number is all it has to go on: two accounts
that end in the same four digits stay apart, and a file that can't say which bank it's from
asks. Made-up data only (card numbers built in code)."""

from helpers import call, card_number, write


def ofx(org, statements, balance="100.00", as_of="20261005"):
    """A made-up OFX download. statements: dicts with acct_id, acct (ACCTTYPE), card (CCACCTFROM),
    rows (YYYYMMDD, amount, name, fitid)."""
    fi = f"<SIGNONMSGSRSV1><SONRS><FI><ORG>{org}<FID>00000</FI></SONRS></SIGNONMSGSRSV1>" if org else ""
    out = f"OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX>{fi}<BANKMSGSRSV1>"
    for s in statements:
        who = (f"<CCACCTFROM><ACCTID>{s['acct_id']}</CCACCTFROM>" if s.get("card") else
               f"<BANKACCTFROM><BANKID>0000<ACCTID>{s['acct_id']}<ACCTTYPE>{s.get('acct', 'CHECKING')}</BANKACCTFROM>")
        rows = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n"
                       for d, a, n, f in s["rows"])
        out += (f"<STMTTRNRS><STMTRS><CURDEF>CAD\n{who}\n<BANKTRANLIST>\n{rows}</BANKTRANLIST>"
                f"<LEDGERBAL><BALAMT>{s.get('balance', balance)}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS>")
    return out + "</BANKMSGSRSV1></OFX>\n"


def _import(app, name, text):
    return call(app, "import_files", [str(write(app.test_downloads, name, text))])


def _accounts(app):
    return {a["name"]: a for a in call(app, "accounts")}


def test_a_card_and_a_chequing_account_with_the_same_last_four_stay_apart(app):
    """Issue #58: the card's balance replaced chequing's."""
    _import(app, "chq.ofx", ofx("Sample Bank", [{"acct_id": "000111222333", "acct": "CHECKING",
                                                 "rows": [("20261001", "-5.00", "SAMPLE CAFE", "A1")]}], "1000.00"))
    card_id = card_number("2333")
    _import(app, "visa.ofx", ofx("Sample Bank", [{"acct_id": card_id, "card": True,
                                                  "rows": [("20261002", "-9.00", "SAMPLE ARCADE", "B1")]}], "-75.00"))
    accounts = call(app, "accounts")
    assert len(accounts) == 2
    chequing = next(a for a in accounts if not a["is_debt"])
    assert chequing["balance"] == 100000 and len(call(app, "transactions", {"account_id": chequing["id"]})["items"]) == 1


def test_rbc_chequing_and_savings_ending_in_the_same_four_digits_stay_apart(app):
    csv = ('"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2","CAD$","USD$"\n'
           'Chequing,12345-0002333,10/1/2026,,"Payroll Deposit","SAMPLE EMPLOYER",1500.00,,\n'
           'Savings,67890-0002333,10/2/2026,,"Transfer","SAMPLE",-100.00,,\n')
    results = call(app, "import_files", [str(write(app.test_downloads, "csv12345.csv", csv))])["results"]
    assert sorted(r["account_name"] for r in results) == ["RBC Chequing", "RBC Savings"]
    assert len(call(app, "accounts")) == 2
    again = call(app, "import_files", [str(write(app.test_downloads, "csv67890.csv", csv))])["results"]
    assert sum(r["added"] for r in again) == 0 and len(call(app, "accounts")) == 2  # each finds its own


def test_a_file_without_a_bank_doesnt_join_an_account_at_a_named_bank_by_its_number(app):
    _import(app, "a.ofx", ofx(None, [{"acct_id": "000111222333", "rows": [("20261001", "-5.00", "SAMPLE CAFE", "A1")]}]))
    [first] = call(app, "accounts")
    call(app, "update_account", first["id"], {"bank": "Sample Bank"})
    # Another bank's file, no <ORG> either, an account that happens to end in the same digits.
    _import(app, "b.ofx", ofx(None, [{"acct_id": "999888772333", "rows": [("20261002", "-7.00", "OTHER SHOP", "B1")]}]))
    assert len(call(app, "accounts")) == 2
    # The same account's next file (same whole number) still finds it.
    _import(app, "c.ofx", ofx(None, [{"acct_id": "000111222333", "rows": [("20261003", "-6.00", "SAMPLE BAKERY", "A2")]}]))
    assert len(call(app, "accounts")) == 2
    assert len(call(app, "transactions", {"account_id": first["id"]})["items"]) == 2


def test_two_unnamed_banks_files_with_different_numbers_are_two_accounts(app):
    _import(app, "a.ofx", ofx(None, [{"acct_id": "000111222333", "rows": [("20261001", "-5.00", "SAMPLE CAFE", "A1")]}]))
    _import(app, "b.ofx", ofx(None, [{"acct_id": "999888772333", "rows": [("20261002", "-7.00", "OTHER SHOP", "B1")]}]))
    assert len(call(app, "accounts")) == 2


def test_a_tangerine_file_with_several_statements_doesnt_trust_accttype(app):
    """Tangerine says SAVINGS for every bank account: with no file name to go by, none is savings."""
    text = ofx("Tangerine", [{"acct_id": "000111222111", "acct": "SAVINGS", "rows": [("20261001", "-5.00", "SAMPLE CAFE", "A1")]},
                             {"acct_id": "000111222222", "acct": "SAVINGS", "rows": [("20261002", "-7.00", "SAMPLE SHOP", "B1")]}])
    _import(app, "Statements.QFX", text)
    accounts = call(app, "accounts")
    assert len(accounts) == 2 and all(a["kind"] != "savings" for a in accounts)
    assert len({a["name"] for a in accounts}) == 2  # told apart by their numbers


def test_an_account_known_only_by_its_last_four_is_found_and_learns_its_whole_number(app):
    _import(app, "a.ofx", ofx("Sample Bank", [{"acct_id": "000111222333", "rows": [("20261001", "-5.00", "SAMPLE CAFE", "A1")]}]))
    [account] = call(app, "accounts")
    with app.db.write() as c:  # as an account made by an older version: only the last four on record
        c.execute("DELETE FROM account_sources WHERE key LIKE 'number-full:%'")
    _import(app, "b.ofx", ofx("Sample Bank", [{"acct_id": "000111222333", "rows": [("20261003", "-6.00", "SAMPLE BAKERY", "A2")]}]))
    assert len(call(app, "accounts")) == 1
    with app.db.read() as c:
        assert c.execute("SELECT account_id FROM account_sources WHERE key = 'number-full:samplebank:000111222333'").fetchone()
    _import(app, "c.ofx", ofx("Sample Bank", [{"acct_id": "555666772333", "rows": [("20261004", "-8.00", "OTHER SHOP", "C1")]}]))
    assert len(call(app, "accounts")) == 2  # a different whole number is another account
