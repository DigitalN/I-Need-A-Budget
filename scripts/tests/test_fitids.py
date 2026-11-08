# SPDX-License-Identifier: GPL-3.0-only
"""A bank's own transaction ID (FITID) is a hint that two rows are the same transaction, not
proof: banks repeat them. Made-up data only."""

from helpers import call, write


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE CU"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _amounts(app):
    return sorted(t["amount"] for t in call(app, "transactions", {})["items"])


def test_fitids_numbered_per_file_dont_drop_next_months_file(app):
    """Issue #49: some banks number FITIDs 1, 2, 3… in every download."""
    sep = write(app.test_downloads, "sep.ofx", ofx([("20260905", "-10.00", "SAMPLE GROCER", "1"),
                                                    ("20260910", "-20.00", "SAMPLE CAFE", "2")], "70.00", "20260930"))
    call(app, "import_files", [str(sep)])
    oct_ = write(app.test_downloads, "oct.ofx", ofx([("20261005", "-15.00", "SAMPLE BOOKS", "1"),
                                                     ("20261008", "-15.00", "SAMPLE BAKERY", "2")], "40.00", "20261012"))
    [result] = call(app, "import_files", [str(oct_)])["results"]
    assert (result["added"], result["duplicates"]) == (2, 0)
    assert _amounts(app) == [-2000, -1500, -1500, -1000]


def test_two_identical_rows_sharing_a_fitid_are_both_kept(app):
    f = write(app.test_downloads, "a.ofx", ofx([("20261008", "-5.75", "SAMPLE COFFEE", "SAME"),
                                                ("20261008", "-5.75", "SAMPLE COFFEE", "SAME")], "100.00", "20261012"))
    call(app, "import_files", [str(f)])
    assert _amounts(app) == [-575, -575]
    # The same file again adds nothing: the fingerprint counts identical rows.
    again = write(app.test_downloads, "b.ofx", ofx([("20261008", "-5.75", "SAMPLE COFFEE", "SAME"),
                                                    ("20261008", "-5.75", "SAMPLE COFFEE", "SAME")], "100.00", "20261012"))
    [result] = call(app, "import_files", [str(again)])["results"]
    assert (result["added"], result["duplicates"]) == (0, 2)


def test_different_transactions_sharing_a_date_like_fitid_are_both_kept(app):
    """Issue #49: some banks use the posting date as the FITID."""
    f = write(app.test_downloads, "a.ofx", ofx([("20261008", "-61.20", "SAMPLE GROCER", "20261008"),
                                                ("20261008", "-4.50", "SAMPLE CAFE", "20261008")], "100.00", "20261012"))
    call(app, "import_files", [str(f)])
    assert _amounts(app) == [-6120, -450]
    # Another download with a different transaction on a day that already has one.
    later = write(app.test_downloads, "b.ofx", ofx([("20261008", "-4.50", "SAMPLE CAFE", "20261008"),
                                                    ("20261008", "-12.00", "SAMPLE BAKERY", "20261008")],
                                                   "90.00", "20261012"))
    [result] = call(app, "import_files", [str(later)])["results"]
    assert (result["added"], result["duplicates"]) == (1, 1)
    assert _amounts(app) == [-6120, -1200, -450]


def test_a_real_duplicate_with_the_same_fitid_in_the_next_download_is_still_skipped(app):
    """The bank re-worded the description and moved the date two days: same FITID, same amount."""
    first = write(app.test_downloads, "a.ofx", ofx([("20261005", "-61.20", "SAMPLE GROCER 12", "F1"),
                                                    ("20261006", "-4.50", "SAMPLE CAFE", "F2")], "100.00", "20261012"))
    call(app, "import_files", [str(first)])
    later = write(app.test_downloads, "b.ofx", ofx([("20261007", "-61.20", "SAMPLE GROCER STORE 12", "F1"),
                                                    ("20261006", "-4.50", "SAMPLE CAFE", "F2"),
                                                    ("20261010", "-9.00", "SAMPLE BOOKS", "F3")], "91.00", "20261012"))
    [result] = call(app, "import_files", [str(later)])["results"]
    assert (result["added"], result["duplicates"]) == (1, 2)
    assert _amounts(app) == [-6120, -900, -450]


def test_the_same_fitid_and_amount_weeks_apart_is_a_new_transaction(app):
    first = write(app.test_downloads, "a.ofx", ofx([("20260905", "-15.00", "SAMPLE GYM", "7")], "100.00", "20260930"))
    call(app, "import_files", [str(first)])
    later = write(app.test_downloads, "b.ofx", ofx([("20261005", "-15.00", "SAMPLE GYM", "7")], "85.00", "20261012"))
    [result] = call(app, "import_files", [str(later)])["results"]
    assert (result["added"], result["duplicates"]) == (1, 0)
    assert _amounts(app) == [-1500, -1500]
