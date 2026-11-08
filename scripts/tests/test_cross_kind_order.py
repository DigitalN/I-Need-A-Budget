# SPDX-License-Identifier: GPL-3.0-only
"""Rows of another kind of file are matched over the whole file, not in file order (issue #116).
Made-up data only."""

import ledger
from helpers import bank_csv, call, write


def qfx(rows, balance, as_of):
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Tangerine"
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>000111222333<ACCTTYPE>SAVINGS</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def test_a_new_coffee_isnt_taken_for_yesterdays(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.QFX",
                                         qfx([("20261001", "-4.50", "POS SAMPLE COFFEE 12", "X1")], "95.50", "20261001")))])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/02/2026", "SAMPLE COFFEE", "SAMPLE COFFEE", "-4.50"),
        ("10/01/2026", "SAMPLE COFFEE", "SAMPLE COFFEE", "-4.50")])))])
    assert sorted(t["date"] for t in call(app, "transactions", {})["items"]) == ["2026-10-01", "2026-10-02"]


def test_the_next_download_after_the_upgrade_keeps_a_new_coffee(app):
    app.db.set_meta("onboarded", "1")
    first = write(app.test_downloads, "activity.csv", "Date,Description,Amount\n2026-10-01,SAMPLE COFFEE,-4.50\n"
                  "2026-09-28,SAMPLE GROCER,-61.20\n2026-09-25,SAMPLE EMPLOYER PAY,1500.00\n")
    setup = call(app, "csv_setup", str(first))
    call(app, "setup_csv", str(first), setup["layout"], {"name": "Sample Chequing", "kind": "chequing", "bank": "Sample CU"})
    with app.db.write() as c:
        c.execute("UPDATE imports SET format = 'csv'")  # how every import before this version is stored
    second = write(app.test_downloads, "activity.csv", "Date,Description,Amount\n2026-10-02,SAMPLE COFFEE,-4.50\n"
                   "2026-10-01,SAMPLE COFFEE,-4.50\n2026-09-28,SAMPLE GROCER,-61.20\n")
    call(app, "import_files", [str(second)])
    assert sorted(t["date"] for t in call(app, "transactions", {})["items"] if t["amount"] == -450) == \
        ["2026-10-01", "2026-10-02"]


def test_plain_csv_is_the_same_kind_as_any_set_up_csv_but_other_set_ups_differ():
    assert ledger.same_kind_of_file("csv", "csv:3:ab12cd34") and ledger.same_kind_of_file("csv:3:ab12cd34", "csv")
    assert ledger.same_kind_of_file("csv:3:ab12cd34", "csv:3:ab12cd34") and ledger.same_kind_of_file("ofx", "ofx")
    assert not ledger.same_kind_of_file("csv:3:ab12cd34", "csv:3:ffff0000")  # a layout set up again
    assert not ledger.same_kind_of_file("csv", "ofx") and not ledger.same_kind_of_file("tangerine_bank", "ofx")


def _account(app, name, rows):
    """An account holding rows: (date, amount cents, kind of file, fingerprint)."""
    with app.db.write() as c:
        account_id = ledger.create_account(c, name, "chequing", bank="Sample CU")
        imports = {}
        for date, amount, kind, fingerprint in rows:
            if kind not in imports:
                imports[kind] = c.execute("INSERT INTO imports (filename, sha256, account_id, format) VALUES (?, ?, ?, ?)",
                                          (f"{name}.{kind}", f"{name}-{kind}", account_id, kind)).lastrowid
            c.execute("INSERT INTO transactions (account_id, date, amount, raw_name, fingerprint, import_id) "
                      "VALUES (?, ?, ?, 'SAMPLE COFFEE', ?, ?)", (account_id, date, amount, fingerprint, imports[kind]))
    return account_id


def test_merging_pairs_the_rest_only_after_the_exact_twins_have_gone(app):
    """Merge into… follows the import's rule: a copy's exact twin (same fingerprint) first, then the others
    nearest first, from the rows left over. The 10-02 copy's nearest row, 10-03, is the 10-03 copy's own twin."""
    into = _account(app, "Sample Into", [("2026-10-03", -450, "ofx", "F"), ("2026-10-01", -450, "ofx", "H")])
    copy = _account(app, "Sample Copy", [("2026-10-02", -450, "tangerine_bank", "G"), ("2026-10-03", -450, "ofx", "F")])
    out = call(app, "merge_accounts", copy, into)
    assert (out["moved"], out["dropped"]) == (0, 2)
    with app.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (into,)).fetchone()[0] == 2
