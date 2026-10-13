"""Files from banks other than Tangerine: RBC's CSV, any bank's Quicken file, and CSVs
set up once by showing the app their columns. Made-up data only."""

import csvlayout
import importer
from helpers import call, txns, write

RBC = """"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2","CAD$","USD$"
Chequing,00000-0004321,10/1/2026,,"e-Transfer sent","SAMPLE PERSON 4XYZ12",-40.00,,
Chequing,00000-0004321,10/2/2026,,"Payroll Deposit","SAMPLE EMPLOYER",1500.00,,
Chequing,00000-0004321,10/3/2026,,"Interac purchase - 1234","SAMPLE GROCER",-61.20,,
Visa,0000000000008765,10/2/2026,,"SAMPLE CAFE CALGARY AB","",-7.25,,
Visa,0000000000008765,10/4/2026,,"PAYMENT - THANK YOU / PAIEMENT - MERCI","",300.00,,
"""


def test_an_rbc_file_has_every_account_in_it(app):
    path = write(app.test_downloads, "csv12345.csv", RBC)
    assert importer.detect(path.read_text()) == "rbc"
    results = call(app, "import_files", [str(path)])["results"]
    assert {(r["account_name"], r["added"]) for r in results} == {("RBC Chequing", 3), ("RBC Visa", 2)}
    accounts = {a["name"]: a for a in call(app, "accounts")}
    assert (accounts["RBC Visa"]["kind"], accounts["RBC Visa"]["last4"], accounts["RBC Visa"]["bank"]) == \
        ("credit_card", "8765", "RBC")
    again = write(app.test_downloads, "csv67890.csv", RBC)
    assert sum(r["added"] for r in call(app, "import_files", [str(again)])["results"]) == 0


def test_rbcs_quicken_file_finds_the_csvs_account_and_skips_its_rows(app):
    """Royal Bank of Canada in the Quicken file is RBC; its descriptions differ from the
    CSV's, but the same date and amount in the same account is the same transaction."""
    call(app, "import_files", [str(write(app.test_downloads, "csv12345.csv", RBC))])
    qfx = """OFXHEADER:100
DATA:OFXSGML

<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Royal Bank of Canada<FID>00000</FI></SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD
<BANKACCTFROM><BANKID>0003<ACCTID>000000004321<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261001<TRNAMT>-40.00<FITID>R1<NAME>E-TRANSFER SENT<MEMO>SAMPLE PERSON</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261002<TRNAMT>1500.00<FITID>R2<NAME>PAYROLL DEP<MEMO>SAMPLE EMPLOYER</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261003<TRNAMT>-61.20<FITID>R3<NAME>IDP PURCHASE<MEMO>SAMPLE GROCER</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261005<TRNAMT>-12.00<FITID>R4<NAME>SAMPLE BAKERY</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>1386.80<DTASOF>20261005</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""
    [result] = call(app, "import_files", [str(write(app.test_downloads, "Statement.qfx", qfx))])["results"]
    assert (result["account_name"], result["added"], result["duplicates"]) == ("RBC Chequing", 1, 3)
    assert len(call(app, "accounts")) == 2
    # The next Quicken file matches by the bank's own IDs, now saved on the CSV's rows.
    with app.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM transactions WHERE fitid IS NOT NULL").fetchone()[0] == 4


def test_qbo_files_are_bank_files(app):
    assert ".qbo" in importer.SUPPORTED_SUFFIXES


DEBIT_CREDIT = """Account: Sample Chequing,,,,
,,,,
Date,Description,Debit,Credit,Balance
2026-10-01,SAMPLE GROCER,61.20,,938.80
2026-10-02,SAMPLE EMPLOYER PAY,,1500.00,2438.80
2026-10-03,SAMPLE CAFE,4.50,,2434.30
"""

CARD_POSITIVE = """Transaction Date,Posted Date,Description,Amount
10/01/2026,10/02/2026,SAMPLE CAFE,4.50
10/03/2026,10/04/2026,SAMPLE BOOKS,22.00
10/05/2026,10/05/2026,PAYMENT RECEIVED THANK YOU,-26.50
"""

NO_HEADER = """10/01/2026,SAMPLE GROCER,61.20,,938.80
10/02/2026,SAMPLE EMPLOYER PAY,,1500.00,2438.80
10/03/2026,SAMPLE CAFE,4.50,,2434.30
"""


def test_layouts_are_guessed_from_the_columns():
    debit_credit = csvlayout.sniff(DEBIT_CREDIT)
    assert debit_credit["header"][:2] == ["Date", "Description"]  # the preamble above is skipped
    assert {k: debit_credit["guess"][k] for k in ("date_col", "desc_cols", "out_col", "in_col", "date_order")} == \
        {"date_col": 0, "desc_cols": [1], "out_col": 2, "in_col": 3, "date_order": "ymd"}
    card = csvlayout.sniff(CARD_POSITIVE)
    assert (card["guess"]["date_col"], card["guess"]["amount_col"], card["guess"]["desc_cols"]) == (0, 3, [2])
    assert card["guess"]["negate"]  # mostly positive: purchases shown as positive
    bare = csvlayout.sniff(NO_HEADER)
    assert bare["header"] is None and len(bare["rows"]) == 3
    assert (bare["guess"]["out_col"], bare["guess"]["in_col"]) == (2, 3)  # Withdrawals, Deposits, Balance
    assert csvlayout.sniff("Name,Email,Phone\nSample,sample@example.com,555\nOther,o@example.com,556\n") is None
    assert csvlayout.read(DEBIT_CREDIT, debit_credit["guess"]) == [
        ("2026-10-01", -6120, "SAMPLE GROCER"), ("2026-10-02", 150000, "SAMPLE EMPLOYER PAY"),
        ("2026-10-03", -450, "SAMPLE CAFE")]


def _onboarded(app):
    app.db.set_meta("onboarded", "1")


def _settled(app):
    """Files in Downloads finished downloading a while ago (the watcher waits for that)."""
    import os
    import time
    for f in app.test_downloads.iterdir():
        os.utime(f, (time.time() - 10, time.time() - 10))


def test_a_new_kind_of_csv_waits_in_the_inbox_until_its_set_up(app):
    _onboarded(app)
    path = write(app.test_downloads, "Visa transactions 2026-10-06.csv", CARD_POSITIVE)
    _settled(app)
    waiting = app.watch_downloads()  # not imported: it needs setting up
    assert waiting["results"] == [] and [f["needs"] for f in waiting["needs_setup"]] == ["layout"]
    inbox = call(app, "inbox")
    assert [(f["filename"], f["needs"]) for f in inbox["files"]] == [(path.name, "layout")]
    assert inbox["counts"]["inbox"] == 1
    setup = call(app, "csv_setup", str(path))
    layout = setup["layout"]
    assert layout["negate"]  # this card shows purchases as positive, and the guess knows
    preview = call(app, "csv_preview", str(path), layout)
    assert [r["amount"] for r in preview["rows"]] == [-450, -2200, 2650]
    out = call(app, "setup_csv", str(path), layout, {"name": "Sample CU Visa", "kind": "credit_card",
                                                     "bank": "sample credit union"})
    [result] = out["results"]
    assert (result["account_name"], result["new_account"], result["added"]) == ("Sample CU Visa", False, 3)
    assert not path.exists()  # moved into Imported Files, like any import from Downloads
    [visa] = call(app, "accounts")
    assert (visa["kind"], visa["bank"]) == ("credit_card", "sample credit union")
    assert call(app, "inbox")["files"] == []

    # The next download from that bank, same name apart from the date: imported on its own.
    write(app.test_downloads, "Visa transactions 2026-10-13.csv", CARD_POSITIVE.replace("10/05/2026", "10/12/2026"))
    _settled(app)
    [auto] = app.watch_downloads()["results"]
    assert (auto["account_name"], auto["added"], auto["duplicates"]) == ("Sample CU Visa", 1, 2)


def test_same_layout_different_name_only_asks_which_account(app):
    _onboarded(app)
    first = write(app.test_downloads, "chequing.csv", DEBIT_CREDIT)
    setup = call(app, "csv_setup", str(first))
    call(app, "setup_csv", str(first), setup["layout"], {"name": "Sample Chequing", "kind": "chequing", "bank": "ATB"})
    other = write(app.test_downloads, "savings.csv", DEBIT_CREDIT.replace("SAMPLE", "OTHER"))
    [waiting] = call(app, "inbox")["files"]
    assert (waiting["filename"], waiting["needs"]) == ("savings.csv", "account")
    again = call(app, "csv_setup", str(other))
    assert again["saved"] and again["layout"]["out_col"] == 2
    out = call(app, "setup_csv", str(other), again["layout"],
               {"name": "atb financial Savings", "kind": "savings", "bank": "atb financial"})
    assert out["results"][0]["account_name"] == "ATB Savings"  # the bank's usual name, in the name too
    assert {a["bank"] for a in call(app, "accounts")} == {"ATB"}


def test_not_a_bank_file_is_never_asked_about_again(app):
    _onboarded(app)
    path = write(app.test_downloads, "export.csv", CARD_POSITIVE)
    assert len(call(app, "inbox")["files"]) == 1
    call(app, "ignore_file", str(path))
    assert call(app, "inbox")["files"] == [] and path.exists()


def test_bank_names_tidy_up_and_are_suggested(app):
    account = call(app, "add_account", "Cash jar", "cash")
    call(app, "update_account", account, {"bank": "  royal bank of canada "})
    assert call(app, "accounts")[0]["bank"] == "RBC"
    call(app, "update_account", account, {"bank": "Sample Community Credit Union"})
    names = call(app, "bank_names")
    assert names[0] == "Sample Community Credit Union" and "ATB" in names and "Servus Credit Union" in names
    call(app, "update_account", account, {"bank": ""})
    assert call(app, "accounts")[0]["bank"] is None


def test_a_card_payment_goes_to_the_card_its_text_names(app):
    from helpers import CARD_FILE, bank_csv, card_csv
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("09/02/2026", "SAMPLE CAFE CALGARY AB", "-7.01", "Restaurant")])))])
    call(app, "import_files", [str(write(app.test_downloads, "csv1.csv", RBC))])  # an RBC Visa too
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/05/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-50.00"),
        ("10/06/2026", "Online Banking payment - 1234", "Online Banking payment - 1234 SAMPLE CREDIT CARD", "-60.00"),
        ("10/07/2026", "Online Banking payment - 1234", "Online Banking payment - 1234 RBC VISA", "-70.00"),
    ])))])
    rows = {t["amount"]: t for t in txns(app) if t["account_name"] == "Chequing"}
    assert rows[-5000]["transfer_account_name"] == "Tangerine Mastercard"  # it says Tangerine
    assert rows[-7000]["transfer_account_name"] == "RBC Visa"
    assert rows[-6000]["transfer_account_name"] is None and rows[-6000]["status"] == "review"  # asked, not guessed
