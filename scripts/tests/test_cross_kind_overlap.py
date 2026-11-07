"""Rows of another kind of file are the same transaction only where the two files overlap
(issue #124): two downloads that only meet (one ends Oct 15, the next starts Oct 16) can't hold
the same transaction twice. Made-up data only."""

import ledger
from helpers import bank_csv, call, write


def qfx(rows, balance, as_of):
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Tangerine"
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>000111222333<ACCTTYPE>SAVINGS</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def fares(app):
    return sorted(t["date"] for t in call(app, "transactions", {})["items"] if t["amount"] == -350)


def test_back_to_back_downloads_keep_both_fares(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/15/2026", "SAMPLE TRANSIT", "SAMPLE TRANSIT", "-3.50"),
        ("10/14/2026", "SAMPLE GROCER", "SAMPLE GROCER", "-61.20"),
        ("10/13/2026", "SAMPLE EMPLOYER PAY", "SAMPLE EMPLOYER PAY", "1500.00")])))])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX", qfx([
        ("20261016", "-3.50", "SAMPLE TRANSIT", "T16"),
        ("20261018", "-12.00", "SAMPLE CAFE", "C18")], "1423.30", "20261018")))])
    assert fares(app) == ["2026-10-15", "2026-10-16"]


def test_the_other_way_round_too(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX", qfx([
        ("20261013", "1500.00", "SAMPLE EMPLOYER PAY", "P13"),
        ("20261015", "-3.50", "SAMPLE TRANSIT", "T15")], "1496.50", "20261015")))])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/18/2026", "SAMPLE CAFE", "SAMPLE CAFE", "-12.00"),
        ("10/16/2026", "SAMPLE TRANSIT", "SAMPLE TRANSIT", "-3.50")])))])
    assert fares(app) == ["2026-10-15", "2026-10-16"]


def test_a_one_day_overlap_keeps_the_fares_either_side(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/15/2026", "SAMPLE TRANSIT", "SAMPLE TRANSIT", "-3.50"),
        ("10/14/2026", "SAMPLE TRANSIT", "SAMPLE TRANSIT", "-3.50"),
        ("10/13/2026", "SAMPLE EMPLOYER PAY", "SAMPLE EMPLOYER PAY", "1500.00")])))])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX", qfx([
        ("20261015", "-3.50", "SAMPLE TRANSIT", "T15"),
        ("20261016", "-3.50", "SAMPLE TRANSIT", "T16"),
        ("20261017", "-12.00", "SAMPLE CAFE", "C17")], "1477.50", "20261017")))])
    assert fares(app) == ["2026-10-14", "2026-10-15", "2026-10-16"]


def test_a_one_day_overlap_the_other_way_round_too(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX", qfx([
        ("20261013", "1500.00", "SAMPLE EMPLOYER PAY", "P13"),
        ("20261014", "-3.50", "SAMPLE TRANSIT", "T14"),
        ("20261015", "-3.50", "SAMPLE TRANSIT", "T15")], "1493.00", "20261015")))])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/17/2026", "SAMPLE CAFE", "SAMPLE CAFE", "-12.00"),
        ("10/16/2026", "SAMPLE TRANSIT", "SAMPLE TRANSIT", "-3.50"),
        ("10/15/2026", "SAMPLE TRANSIT", "SAMPLE TRANSIT", "-3.50")])))])
    assert fares(app) == ["2026-10-14", "2026-10-15", "2026-10-16"]


def test_files_that_overlap_still_match_a_shifted_date(app):
    """The same fare, dated a day later in the second file, where the files cover the same days."""
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/16/2026", "SAMPLE TRANSIT", "SAMPLE TRANSIT", "-3.50"),
        ("10/14/2026", "SAMPLE GROCER", "SAMPLE GROCER", "-61.20")])))])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX", qfx([
        ("20261017", "-3.50", "SAMPLE TRANSIT", "T17"),
        ("20261015", "-61.20", "SAMPLE GROCER", "G15"),
        ("20261018", "-12.00", "SAMPLE CAFE", "C18")], "1423.30", "20261018")))])
    assert fares(app) == ["2026-10-16"]


def _account(app, name, rows):
    """An account holding rows: (date, amount cents, kind of file, import's first date, its last date)."""
    with app.db.write() as c:
        account_id = ledger.create_account(c, name, "chequing", bank="Sample CU")
        imports = {}
        for n, (date, amount, kind, first, last) in enumerate(rows):
            if kind not in imports:
                imports[kind] = c.execute(
                    "INSERT INTO imports (filename, sha256, account_id, format, first_date, last_date) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (f"{name}.{kind}", f"{name}-{kind}", account_id, kind, first, last)).lastrowid
            c.execute("INSERT INTO transactions (account_id, date, amount, raw_name, fingerprint, import_id) "
                      "VALUES (?, ?, ?, 'SAMPLE FARE', ?, ?)", (account_id, date, amount, f"{name}-{n}", imports[kind]))
    return account_id


def test_merging_and_the_imported_twice_check_use_the_same_rule(app):
    # Two accounts whose files only meet: nothing pairs. Where they overlap, the same fare does.
    meets_a = _account(app, "Sample A", [("2026-10-15", -350, "tangerine_bank", "2026-10-13", "2026-10-15")])
    meets_b = _account(app, "Sample B", [("2026-10-16", -350, "ofx", "2026-10-16", "2026-10-18")])
    with app.db.read() as c:
        assert ledger.cross_kind_pairs(c, meets_a, meets_b) == []
    overlap_a = _account(app, "Sample C", [("2026-10-15", -350, "tangerine_bank", "2026-10-13", "2026-10-16")])
    overlap_b = _account(app, "Sample D", [("2026-10-16", -350, "ofx", "2026-10-15", "2026-10-18")])
    with app.db.read() as c:
        assert len(ledger.cross_kind_pairs(c, overlap_a, overlap_b)) == 1


def test_merging_keeps_the_fares_at_the_edges_of_a_one_day_overlap(app):
    a = _account(app, "Sample E", [("2026-10-14", -350, "tangerine_bank", "2026-10-13", "2026-10-15"),
                                   ("2026-10-15", -350, "tangerine_bank", "2026-10-13", "2026-10-15")])
    b = _account(app, "Sample F", [("2026-10-15", -350, "ofx", "2026-10-15", "2026-10-17"),
                                   ("2026-10-16", -350, "ofx", "2026-10-15", "2026-10-17")])
    with app.db.read() as c:
        [(from_a, into_b)] = ledger.cross_kind_pairs(c, a, b)
        dates = [c.execute("SELECT date FROM transactions WHERE id = ?", (i,)).fetchone()[0] for i in (from_a, into_b)]
    assert dates == ["2026-10-15", "2026-10-15"]  # the 14th and the 16th are two more fares
