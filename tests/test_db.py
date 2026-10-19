"""Undo, and keeping only the changes it should."""

from db import SCHEMA_VERSION, Database


def make(tmp_path):
    db = Database(tmp_path / "budget.db")
    with db.write() as c:
        c.execute("INSERT INTO accounts (id, name, kind) VALUES (1, 'Chequing', 'chequing')")
        c.execute("INSERT INTO category_groups (id, name) VALUES (1, 'Bills')")
        c.execute("INSERT INTO categories (id, group_id, name) VALUES (1, 1, 'Rent'), (2, 1, 'Food')")
        c.execute("INSERT INTO transactions (id, account_id, date, amount) VALUES (1, 1, '2026-07-01', -100)")
    return db


def test_undo_reverses_one_labelled_change(tmp_path):
    db = make(tmp_path)
    with db.write("Categorize transaction") as c:
        c.execute("UPDATE transactions SET category_id = 1, status = 'done' WHERE id = 1")
    assert db.undo_label() == "Categorize transaction"
    assert db.undo() == "Categorize transaction"
    row = db.conn.execute("SELECT category_id, status FROM transactions WHERE id = 1").fetchone()
    assert (row["category_id"], row["status"]) == (None, "review")
    assert db.undo() is None


def test_undo_leaves_later_unrelated_changes_alone(tmp_path):
    db = make(tmp_path)
    with db.write("Categorize transaction") as c:
        c.execute("UPDATE transactions SET category_id = 2 WHERE id = 1")
    with db.write() as c:  # an import pairing a transfer: not undoable, and must survive
        c.execute("UPDATE transactions SET note = 'kept' WHERE id = 1")
    db.undo()
    row = db.conn.execute("SELECT category_id, note FROM transactions WHERE id = 1").fetchone()
    assert (row["category_id"], row["note"]) == (None, "kept")


def test_undo_brings_back_deleted_rows_and_their_children(tmp_path):
    db = make(tmp_path)
    with db.write() as c:
        c.execute("INSERT INTO budget (month, category_id, assigned) VALUES ('2026-07', 1, 5000)")
        c.execute("UPDATE transactions SET category_id = 1 WHERE id = 1")
    with db.write("Delete group") as c:
        c.execute("DELETE FROM category_groups WHERE id = 1")
    assert db.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
    db.undo()
    assert db.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 2
    assert db.conn.execute("SELECT assigned FROM budget").fetchone()[0] == 5000
    assert db.conn.execute("SELECT category_id FROM transactions WHERE id = 1").fetchone()[0] == 1


def test_nested_writes_are_one_step(tmp_path):
    db = make(tmp_path)
    with db.write("Outer") as c:
        c.execute("UPDATE categories SET name = 'Housing' WHERE id = 1")
        with db.write("Inner") as c2:
            c2.execute("UPDATE categories SET name = 'Groceries' WHERE id = 2")
    assert db.undo() == "Outer"
    names = [r[0] for r in db.conn.execute("SELECT name FROM categories ORDER BY id")]
    assert names == ["Rent", "Food"]


def test_a_failed_write_changes_nothing(tmp_path):
    db = make(tmp_path)
    try:
        with db.write("Broken") as c:
            c.execute("UPDATE categories SET name = 'X' WHERE id = 1")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.conn.execute("SELECT name FROM categories WHERE id = 1").fetchone()[0] == "Rent"
    assert db.undo_label() is None


def test_daily_backup_keeps_one_per_day(tmp_path):
    db = make(tmp_path)
    first = db.backup_daily(tmp_path / "Backups")
    assert first is not None and first.exists()
    assert db.backup_daily(tmp_path / "Backups") is None


def test_an_older_database_is_backed_up_then_upgraded(tmp_path):
    import sqlite3

    path = tmp_path / "budget.db"
    old = sqlite3.connect(path)  # version 1: categories had no note
    old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE category_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'normal',
            sort INTEGER NOT NULL DEFAULT 0, collapsed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE categories (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL, name TEXT NOT NULL,
            sort INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, monthly_budget INTEGER,
            debt_account_id INTEGER UNIQUE, old_names TEXT NOT NULL DEFAULT '');
        INSERT INTO category_groups (id, name) VALUES (1, 'Bills');
        INSERT INTO categories (id, group_id, name) VALUES (1, 1, 'Rent');
    """)
    old.commit()
    old.close()
    db = Database(path)
    columns = [r["name"] for r in db.conn.execute("PRAGMA table_info(categories)")]
    assert "note" in columns
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert db.conn.execute("SELECT name, note FROM categories").fetchone()[:] == ("Rent", "")
    backups = list((tmp_path / "Backups").glob(f"budget-before-upgrade-v1-to-v{SCHEMA_VERSION}-*.db"))
    assert len(backups) == 1
    with db.write("Edit") as c:  # undo still works on the new column
        c.execute("UPDATE categories SET note = 'renews in March' WHERE id = 1")
    db.undo()
    assert db.conn.execute("SELECT note FROM categories").fetchone()[0] == ""


def test_version_2_accounts_from_files_become_tangerine_accounts(tmp_path):
    """Version 2 only read Tangerine's files; accounts added by hand have no bank."""
    db = Database(tmp_path / "budget.db")
    with db.write() as c:
        c.execute("INSERT INTO accounts (id, name, kind) VALUES (1, 'Chequing', 'chequing'), (2, 'Cash', 'cash')")
        c.execute("INSERT INTO account_sources (key, account_id) VALUES ('tangerine-bank:chequing', 1)")
        c.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    for suffix in ("ins", "upd", "del"):
        db.conn.execute(f"DROP TRIGGER undo_accounts_{suffix}")  # they name every column
    db.conn.execute("ALTER TABLE accounts DROP COLUMN bank")
    db.close()
    db = Database(tmp_path / "budget.db")
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert [tuple(r) for r in db.conn.execute("SELECT name, bank FROM accounts ORDER BY id")] == [("Chequing", "Tangerine"),
                                                                                              ("Cash", None)]
    assert len(list((tmp_path / "Backups").glob(f"budget-before-upgrade-v2-to-v{SCHEMA_VERSION}-*.db"))) == 1


def test_version_3_e_transfer_payees_start_with_always_ask_on(tmp_path):
    """A payee is just a name (docs/adr/0001): anyone you e-transfer with gets Always ask,
    and being an e-transfer moves from the payee to its transactions."""
    db = Database(tmp_path / "budget.db")
    with db.write() as c:
        c.execute("INSERT INTO accounts (id, name, kind) VALUES (1, 'Chequing', 'chequing')")
        c.execute("INSERT INTO payees (id, name, kind, always_ask) VALUES (1, 'Riley Example', 'person', 0), "
                  "(2, 'Sample Cafe', 'merchant', 0), (3, 'Sample Bookshop', 'merchant', 1), "
                  "(4, 'Cash withdrawal', 'bank', 0), (5, 'Sample Utility', 'organization', 0)")
        c.execute("INSERT INTO transactions (id, account_id, date, amount, payee_id, hint) VALUES "
                  "(1, 1, '2026-07-01', -6000, 1, ''), (2, 1, '2026-07-02', -650, 2, ''), "
                  "(3, 1, '2026-07-03', -10000, 4, 'cash'), (4, 1, '2026-07-04', -4210, 5, '')")
        c.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    db.close()
    db = Database(tmp_path / "budget.db")
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert [tuple(r) for r in db.conn.execute("SELECT name, always_ask FROM payees ORDER BY id")] == [
        ("Riley Example", 1), ("Sample Cafe", 0), ("Sample Bookshop", 1), ("Cash withdrawal", 0), ("Sample Utility", 0)]
    assert [r[0] for r in db.conn.execute("SELECT hint FROM transactions ORDER BY id")] == ["etransfer", "", "cash", ""]
    assert len(list((tmp_path / "Backups").glob(f"budget-before-upgrade-v3-to-v{SCHEMA_VERSION}-*.db"))) == 1


def test_version_4_inbox_items_lose_their_tentative_category(tmp_path):
    """An inbox item counts in no category until it's categorized (docs/adr/0002): the
    top suggestion it used to carry goes; what you categorized and splits stay."""
    db = Database(tmp_path / "budget.db")
    with db.write() as c:
        c.execute("INSERT INTO accounts (id, name, kind) VALUES (1, 'Chequing', 'chequing')")
        c.execute("INSERT INTO category_groups (id, name) VALUES (1, 'Everyday')")
        c.execute("INSERT INTO categories (id, group_id, name) VALUES (1, 1, 'Food'), (2, 1, 'Fun')")
        c.execute("INSERT INTO transactions (id, account_id, date, amount, category_id, status, is_split) VALUES "
                  "(1, 1, '2026-07-01', -650, 1, 'review', 0), (2, 1, '2026-07-02', -900, 1, 'done', 0), "
                  "(3, 1, '2026-07-03', -2000, NULL, 'review', 1)")
        c.execute("INSERT INTO splits (transaction_id, category_id, amount) VALUES (3, 1, -1500), (3, 2, -500)")
        c.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    db.close()
    db = Database(tmp_path / "budget.db")
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert [r[0] for r in db.conn.execute("SELECT category_id FROM transactions ORDER BY id")] == [None, 1, None]
    assert db.conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 2
    assert len(list((tmp_path / "Backups").glob(f"budget-before-upgrade-v4-to-v{SCHEMA_VERSION}-*.db"))) == 1


def test_version_5_what_you_decided_becomes_yours(tmp_path):
    """What you decided stays decided (docs/adr/0005): the reasons that said so become
    `by_you`, and so do inbox items you sent back (no reason) or said aren't a transfer.
    A transfer can't be told apart, so it stays the app's."""
    db = Database(tmp_path / "budget.db")
    reasons = ["Categorized by you", "Filed by you", "Split by you", "Added by you", "Always Food (3 times before)",
               "No longer a transfer", "", "New payee", "Transfer to Mastercard", ""]
    statuses = ["done"] * 5 + ["review"] * 4 + ["done"]
    with db.write() as c:
        c.execute("INSERT INTO accounts (id, name, kind) VALUES (1, 'Chequing', 'chequing')")
        for i, (reason, status) in enumerate(zip(reasons, statuses), start=1):
            c.execute("INSERT INTO transactions (id, account_id, date, amount, status, reason) "
                      "VALUES (?, 1, '2026-07-01', -100, ?, ?)", (i, status, reason))
        c.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    for suffix in ("ins", "upd", "del"):
        db.conn.execute(f"DROP TRIGGER undo_transactions_{suffix}")  # they name every column
    db.conn.execute("ALTER TABLE transactions DROP COLUMN by_you")
    db.close()
    db = Database(tmp_path / "budget.db")
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
    rows = [tuple(r) for r in db.conn.execute("SELECT by_you, reason FROM transactions ORDER BY id")]
    assert [r[0] for r in rows] == [1, 1, 1, 1, 0, 1, 1, 0, 0, 0]
    assert rows[6] == (1, "You sent this back")
    assert len(list((tmp_path / "Backups").glob(f"budget-before-upgrade-v5-to-v{SCHEMA_VERSION}-*.db"))) == 1
