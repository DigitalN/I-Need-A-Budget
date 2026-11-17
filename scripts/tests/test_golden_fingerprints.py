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
from helpers import CARD_FILE, bank_csv, card_csv, card_number, write

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
    """A CSV set up once: its description columns joined."""
    text = ("Date,Description,Amount,Balance\n2026-10-01,SAMPLE CAFE CALGARY AB,-4.50,995.50\n"
            "2026-10-01,SAMPLE CAFE CALGARY AB,-4.50,991.00\n2026-10-02,SAMPLE GROCER,-61.20,929.80\n")
    layout = {"id": 7, "has_header": True, "date_col": 0, "date_order": "ymd", "desc_cols": [1], "amount_col": 2,
              "out_col": None, "in_col": None, "negate": False}
    rows = [Row(day, cents, description) for day, cents, description in csvlayout.read(text, layout)]
    assert importer.fingerprints(rows) == [CAFE_1, CAFE_2, GROCER]
