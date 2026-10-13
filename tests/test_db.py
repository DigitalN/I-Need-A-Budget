"""Undo, and keeping only the changes it should."""

from db import Database


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
    with db.write("File transaction") as c:
        c.execute("UPDATE transactions SET category_id = 1, status = 'done' WHERE id = 1")
    assert db.undo_label() == "File transaction"
    assert db.undo() == "File transaction"
    row = db.conn.execute("SELECT category_id, status FROM transactions WHERE id = 1").fetchone()
    assert (row["category_id"], row["status"]) == (None, "review")
    assert db.undo() is None


def test_undo_leaves_later_unrelated_changes_alone(tmp_path):
    db = make(tmp_path)
    with db.write("File transaction") as c:
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
    assert db.get_meta("schema_version") == "3"
    assert db.conn.execute("SELECT name, note FROM categories").fetchone()[:] == ("Rent", "")
    backups = list((tmp_path / "Backups").glob("budget-before-upgrade-v1-to-v3-*.db"))
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
    assert db.get_meta("schema_version") == "3"
    assert [tuple(r) for r in db.conn.execute("SELECT name, bank FROM accounts ORDER BY id")] == [("Chequing", "Tangerine"),
                                                                                              ("Cash", None)]
    assert len(list((tmp_path / "Backups").glob("budget-before-upgrade-v2-to-v3-*.db"))) == 1
