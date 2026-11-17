# SPDX-License-Identifier: GPL-3.0-only
"""Golden fingerprints (issue #79). Made-up rows only.

Every imported transaction keeps its row's importer.fingerprint(), and the next file finds
it by that: "never change fingerprint() (or fingerprints())" (.claude/CLAUDE.md), or every
overlapping file imports twice. The expected strings are what they returned when this was
written, as literals. If one of these fails, the change is the bug: never change a string
here to make a test pass.
"""

import importer
from importer import Row
import csvlayout
import test_balance_check as check
from helpers import CARD_FILE, bank_csv, call, card_csv, card_number, write
from test_card_signs import card_ofx

CAFE_1 = "bd76e32e09c1a550d953"  # 2026-10-01, -$4.50, SAMPLE CAFE CALGARY AB, the first in its file
CAFE_2 = "5548599cf57b3d6f4e31"  # ... the second identical one in the same file
GROCER = "ea778bea705147276e4b"  # 2026-10-02, -$61.20, SAMPLE GROCER


def test_fingerprint():
    assert importer.fingerprint("2026-10-01", -450, "SAMPLE CAFE CALGARY AB", 0) == CAFE_1
    assert importer.fingerprint("2026-10-01", -450, "SAMPLE CAFE CALGARY AB", 1) == CAFE_2
    # Only letters and digits count, in any case.
    assert importer.fingerprint("2026-10-01", -450, "Sample Cafe, Calgary AB", 0) == CAFE_1
    assert importer.fingerprint("2026-10-02", -6120, "SAMPLE GROCER", 0) == GROCER
    assert importer.fingerprint("2026-10-01", 150000, "EFT Deposit from SAMPLE EMPLOYER", 0) == "a47fa3a709517753a77e"


def test_two_identical_rows_in_one_file_count_up():
    rows = [Row("2026-10-01", -450, "SAMPLE CAFE CALGARY AB"), Row("2026-10-02", -6120, "SAMPLE GROCER"),
            Row("2026-10-01", -450, "sample cafe calgary ab")]
    assert importer.fingerprints(rows) == [CAFE_1, GROCER, CAFE_2]


def test_a_tangerine_csv(tmp_path):
    """The Name is fingerprinted, never the Memo."""
    path = write(tmp_path, "Chequing.CSV", bank_csv([
        ("10/01/2026", "SAMPLE CAFE CALGARY AB", "", "-4.50"),
        ("10/01/2026", "SAMPLE CAFE CALGARY AB", "", "-4.50"),
        ("10/02/2026", "EFT Deposit from SAMPLE EMPLOYER", "EFT Deposit from SAMPLE EMPLOYER INC", "1500.00")]))
    [part] = importer.parse(path).parts
    assert importer.fingerprints(part.rows) == [CAFE_1, CAFE_2, "c961ea68729f21df201f"]


def test_a_tangerine_card_csv(tmp_path):
    path = write(tmp_path, CARD_FILE, card_csv([("10/03/2026", "SAMPLE DINER 12345 RED DEER AB", "-23.10", "Restaurants")]))
    [part] = importer.parse(path).parts
    assert importer.fingerprints(part.rows) == ["94f289d523b262982790"]


def quicken_file(org, account, rows):
    """A made-up Quicken (OFX) download. rows: (YYYYMMDD, amount, name, bank ID or None)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}" + (f"<FITID>{f}" if f else "") +
                  f"<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    fi = f"<SIGNONMSGSRSV1><SONRS><FI><ORG>{org}<FID>00000</FI></SONRS></SIGNONMSGSRSV1>" if org else ""
    return (f"OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX>{fi}<BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"{account}\n<BANKTRANLIST>\n{trn}</BANKTRANLIST><LEDGERBAL><BALAMT>1500.25<DTASOF>20261005"
            f"</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


ROWS = [("20261001", "-4.50", "SAMPLE CAFE CALGARY AB", "202610010001"),
        ("20261001", "-4.50", "SAMPLE CAFE CALGARY AB", "202610010002"),
        ("20261002", "-61.20", "SAMPLE GROCER", None)]


def test_a_quicken_file_with_and_without_bank_ids(tmp_path):
    """A bank ID isn't part of the fingerprint: the same row from a CSV and a Quicken file has the same one."""
    account = "<BANKACCTFROM><BANKID>0000<ACCTID>000123456789<ACCTTYPE>CHECKING</BANKACCTFROM>"
    [part] = importer.parse(write(tmp_path, "statement.qfx", quicken_file("Sample Credit Union", account, ROWS))).parts
    assert [r.fitid for r in part.rows] == ["202610010001", "202610010002", None]
    assert importer.fingerprints(part.rows) == [CAFE_1, CAFE_2, GROCER]


def test_a_quicken_card_file(tmp_path):
    account = f"<CCACCTFROM><ACCTID>{card_number('8765')}</CCACCTFROM>"
    [part] = importer.parse(write(tmp_path, "statement.qbo", quicken_file("TD Canada Trust", account, ROWS))).parts
    assert importer.fingerprints(part.rows) == [CAFE_1, CAFE_2, GROCER]


def test_an_rbc_csv(tmp_path):
    """RBC's name is Description 1 and 2 together."""
    path = write(tmp_path, "csv12345.csv",
                 '"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2",'
                 '"CAD$","USD$"\n'
                 'Chequing,00000-0004321,10/1/2026,,"e-Transfer sent","SAMPLE PERSON 4XYZ12",-40.00,,\n'
                 'Chequing,00000-0004321,10/3/2026,,"Interac purchase - 1234","SAMPLE GROCER",-61.20,,\n'
                 'Visa,0000000000008765,10/2/2026,,"SAMPLE CAFE CALGARY AB","",-7.25,,\n')
    chequing, visa = importer.parse(path).parts
    assert importer.fingerprints(chequing.rows) == ["e3f4cd7a6cd89f02fc56", "8d95a9c029db255a8506"]
    assert importer.fingerprints(visa.rows) == ["37faad4f2ae92c943d1e"]


def test_a_set_up_csv():
    """A CSV set up once: its description columns joined, in order."""
    text = ("Date,Description,Details,Amount\n2026-10-01,SAMPLE CAFE,CALGARY AB,-4.50\n"
            "2026-10-01,SAMPLE CAFE,CALGARY AB,-4.50\n2026-10-02,SAMPLE GROCER,,-61.20\n")
    layout = {"id": 7, "has_header": True, "date_col": 0, "date_order": "ymd", "desc_cols": [1, 2], "amount_col": 3,
              "out_col": None, "in_col": None, "negate": False}
    rows = [Row(day, cents, description) for day, cents, description in csvlayout.read(text, layout)]
    assert importer.fingerprints(rows) == [CAFE_1, CAFE_2, GROCER]


# ── what an import stores ────────────────────────────────────
# The fingerprints above are worked out from rows; these are read back from transactions.fingerprint after an
# import through the app, so a change on the way there fails too (a card's signs flipped after fingerprinting, a
# Quicken file's NAME read differently, a set-up CSV's columns, the next free occurrence).

def stored(app):
    with app.db.read() as c:
        return [tuple(r) for r in c.execute("SELECT date, amount, fingerprint FROM transactions ORDER BY date, amount, id")]


def imported(app, filename, text):
    return call(app, "import_files", [str(write(app.test_downloads, filename, text))])["results"]


def test_stored_from_a_tangerine_csv(app):
    imported(app, "Chequing.CSV", bank_csv([
        ("10/01/2026", "SAMPLE CAFE CALGARY AB", "", "-4.50"),
        ("10/01/2026", "SAMPLE CAFE CALGARY AB", "", "-4.50"),
        ("10/02/2026", "EFT Deposit from SAMPLE EMPLOYER", "EFT Deposit from SAMPLE EMPLOYER INC", "1500.00")]))
    assert stored(app) == [("2026-10-01", -450, CAFE_1), ("2026-10-01", -450, CAFE_2),
                           ("2026-10-02", 150000, "c961ea68729f21df201f")]


def test_stored_from_a_card_file_whose_signs_are_flipped(app):
    """This card writes purchases as positive: a row is fingerprinted once its sign is settled (the $4.50 coffee
    as -450, the same as a CSV's), never as the file wrote it."""
    imported(app, "visa.ofx", card_ofx([("20261001", "4.50", "SAMPLE CAFE CALGARY AB", "A1"),
                                        ("20261002", "61.20", "SAMPLE GROCER", "A2"),
                                        ("20261003", "23.10", "SAMPLE DINER", "A3"),
                                        ("20261004", "-100.00", "PAYMENT THANK YOU", "A4")], "-11.00"))
    assert stored(app) == [("2026-10-01", -450, CAFE_1), ("2026-10-02", -6120, GROCER),
                           ("2026-10-03", -2310, "c6442fe5f9837cee3d88"), ("2026-10-04", 10000, "b215b91de1bf98b2aab5")]


def stmttrn(**tags):
    return "<STMTTRN><TRNTYPE>OTHER" + "".join(f"<{tag}>{value}" for tag, value in tags.items()) + "</STMTTRN>\n"


def test_stored_from_a_quicken_files_names(app):
    """NAME unescaped (& as &amp;), PAYEE when there's no NAME, NAME over PAYEE, and no name at all (never the MEMO)."""
    account = "<BANKACCTFROM><BANKID>0000<ACCTID>000123456789<ACCTTYPE>CHECKING</BANKACCTFROM>"
    text = quicken_file("Sample Credit Union", account, [])
    text = text.replace("<BANKTRANLIST>\n", "<BANKTRANLIST>\n" + "".join([
        stmttrn(DTPOSTED="20261001", TRNAMT="-4.50", FITID="B1", NAME="SAMPLE &amp; CO CAFE"),
        stmttrn(DTPOSTED="20261002", TRNAMT="-5.50", FITID="B2", PAYEE="SAMPLE PAYEE CO"),
        stmttrn(DTPOSTED="20261003", TRNAMT="-6.50", FITID="B3", NAME="SAMPLE NAME", PAYEE="SAMPLE OTHER"),
        stmttrn(DTPOSTED="20261004", TRNAMT="-7.50", FITID="B4", MEMO="SAMPLE MEMO ONLY")]))
    imported(app, "statement.qfx", text)
    assert stored(app) == [("2026-10-01", -450, "b1eee73d0b60fca29dac"), ("2026-10-02", -550, "b3926feb7af3dc80aae5"),
                           ("2026-10-03", -650, "2a36c212ceeee56f1ce6"), ("2026-10-04", -750, "f5c6dec11e6de105d14e")]


def test_stored_from_a_set_up_csv_with_two_description_columns(app):
    app.db.set_meta("onboarded", "1")
    path = write(app.test_downloads, "Sample chequing.csv",
                 "Date,Description,Details,Amount\n2026-10-01,SAMPLE CAFE,CALGARY AB,-4.50\n"
                 "2026-10-01,SAMPLE CAFE,CALGARY AB,-4.50\n2026-10-02,SAMPLE GROCER,,-61.20\n")
    layout = dict(call(app, "csv_setup", str(path))["layout"], desc_cols=[1, 2])
    call(app, "setup_csv", str(path), layout, {"name": "Sample Chequing", "kind": "chequing", "bank": "Sample CU"})
    assert stored(app) == [("2026-10-01", -450, CAFE_1), ("2026-10-01", -450, CAFE_2), ("2026-10-02", -6120, GROCER)]


def test_stored_when_a_rows_own_fingerprint_is_taken(app):
    """The bank re-dated the first coffee (its ID kept), and a second coffee bought the same day comes with an ID of
    its own: its fingerprint is the first's, so it's stored under the next occurrence (_free_fingerprint)."""
    check._import(app, "a.ofx", check.ofx([("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "C1")], "996.00", "20260912",
                                          start="20260901"))
    call(app, "finish_onboarding", "2026-09")
    check._import(app, "b.ofx", check.ofx([("20260914", "-4.00", "SAMPLE COFFEE CALGARY", "C1"),
                                           ("20260912", "-4.00", "SAMPLE COFFEE CALGARY AB", "C2")], "992.00", "20260920",
                                          start="20260912"))
    assert stored(app) == [("2026-09-12", -400, "d80c61ca5a9994f8ef1a"), ("2026-09-12", -400, "be017b6f6ee1dce35f1f")]
