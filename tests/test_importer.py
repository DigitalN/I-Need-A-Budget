"""Reading Tangerine exports and importing them without duplicates."""

import importer
from helpers import CARD_FILE, bank_csv, call, card_csv, categories, txns, write


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
    assert parsed.account.kind == "credit_card" and parsed.account.last4 == "1234"
    assert parsed.account.keys[0] == "tangerine-card:1234"
    assert [(r.date, r.amount, r.bank_category) for r in parsed.rows] == [
        ("2026-10-08", -701, "Restaurant"), ("2026-10-07", 5215, "Other")]


def test_bank_account_comes_from_the_file_name(tmp_path):
    for name in ("Chequing.CSV", "Chequing-2.CSV", "Chequing (1).csv"):
        parsed = importer.parse(write(tmp_path, name, bank_csv([("05/01/2026", "Interest Paid", "Interest Paid", "0.03")])))
        assert parsed.account.keys == ["tangerine-bank:chequing"]
        assert parsed.account.name == "Chequing"
    savings = importer.parse(write(tmp_path, "Savings.CSV", bank_csv([])))
    assert savings.account.kind == "savings"


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
    assert parsed.balance == ("2026-09-30", 150025)
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
