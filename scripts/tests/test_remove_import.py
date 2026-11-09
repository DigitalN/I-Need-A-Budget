# SPDX-License-Identifier: GPL-3.0-only
"""Removing an import puts the balance back the way it was. Made-up data only."""

import db
from helpers import call, write


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE CU"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _first_file(app):
    first = write(app.test_downloads, "chequing-1.ofx", ofx([("20260901", "2000.00", "PAYROLL SAMPLE CO", "A1")],
                                                            "3000.00", "20260915"))
    call(app, "import_files", [str(first)])
    call(app, "finish_onboarding", "2026-09")


def _second_file(app):
    second = write(app.test_downloads, "chequing-2.ofx", ofx([("20260916", "-100.00", "SAMPLE HARDWARE CALGARY AB", "A4")],
                                                             "2900.00", "20261010"))
    call(app, "import_files", [str(second)])


def _anchor(app):
    account = call(app, "accounts")[0]
    return account["anchor_date"], account["anchor_balance"]


def test_remove_import_puts_the_balance_back(app):
    """Issue #44: file 2's balance stayed, so September's starting Ready to assign dropped."""
    _first_file(app)
    before = call(app, "month", "2026-09")["summary"]["carried_rta"]
    _second_file(app)
    assert _anchor(app) == ("2026-10-10", 290000)
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-09-15", 300000)
    assert call(app, "month", "2026-09")["summary"]["carried_rta"] == before
    # One undo step: ⌘Z brings the file back, balance and all.
    call(app, "undo")
    assert _anchor(app) == ("2026-10-10", 290000)
    assert len(call(app, "transactions", {})["items"]) == 2


def test_a_balance_a_later_file_or_you_moved_since_is_left_alone(app):
    _first_file(app)
    _second_file(app)
    second_id = call(app, "imports")[0]["id"]
    third = write(app.test_downloads, "chequing-3.ofx", ofx([("20261011", "-50.00", "SAMPLE BAKERY", "A7")],
                                                            "2850.00", "20261012"))
    call(app, "import_files", [str(third)])
    call(app, "undo_import", second_id)  # the third file's balance is the account's now
    assert _anchor(app) == ("2026-10-12", 285000)
    # Removing the third then brings back what was there before it: the second file's balance is gone with
    # its file, so it's the first file's.
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-09-15", 300000)
    # A balance you typed after an import isn't undone by removing it.
    _second_file(app)
    call(app, "set_balance", call(app, "accounts")[0]["id"], 111100, "2026-10-11")
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-10-11", 111100)


def test_removing_the_accounts_first_import_takes_its_balance_with_it(app):
    _first_file(app)
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == (None, None)


def test_removing_an_import_brings_back_the_balance_you_typed_before_it(app):
    _first_file(app)
    call(app, "set_balance", call(app, "accounts")[0]["id"], 310000, "2026-09-20")
    _second_file(app)  # its balance is newer than the one typed
    assert _anchor(app) == ("2026-10-10", 290000)
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _anchor(app) == ("2026-09-20", 310000)


def test_an_existing_database_upgrades_with_imports_that_remember_nothing(tmp_path):
    path = tmp_path / "budget.db"
    database = db.Database(path)
    database.conn.execute("INSERT INTO imports (filename, sha256, format) VALUES ('old.ofx', 'abc', 'ofx')")
    for suffix in ("ins", "upd", "del"):  # the undo triggers name every column
        database.conn.execute(f"DROP TRIGGER IF EXISTS undo_imports_{suffix}")
    for column in ("anchor_before_date", "anchor_before_balance", "anchor_after_date", "anchor_after_balance"):
        database.conn.execute(f"ALTER TABLE imports DROP COLUMN {column}")  # a version 6 database
    database.conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    database.conn.commit()
    database.conn.close()
    again = db.Database(path)
    columns = [r["name"] for r in again.conn.execute("PRAGMA table_info(imports)")]
    assert {"anchor_before_date", "anchor_before_balance", "anchor_after_date", "anchor_after_balance"} <= set(columns)
    assert again.get_meta("schema_version") == str(db.SCHEMA_VERSION) == "10"
    old = again.conn.execute("SELECT * FROM imports").fetchone()
    assert (old["filename"], old["anchor_after_date"]) == ("old.ofx", None)  # removing it leaves the balance as it is
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v6-to-v10-*.db"))
