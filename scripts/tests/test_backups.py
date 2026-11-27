# SPDX-License-Identifier: GPL-3.0-only
"""Backups at quit, into Documents (issue #21, docs/adr/0004). Made-up data and dates, in
throwaway folders: nothing here reads or writes the real data folder or Documents."""

import errno
import logging
import importlib.util
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

import backups
import computer
from db import SCHEMA_VERSION, Database

MAIN = Path(__file__).resolve().parents[2] / "Application Files" / "main.py"
QUIT = datetime(2026, 11, 8, 14, 32, 5)
PERMISSIONS = pytest.mark.skipif(sys.platform == "win32", reason="a folder's permissions don't stop access on Windows")
MAC = pytest.mark.skipif(sys.platform != "darwin", reason="macOS asks the app's delegate")
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows closes pywebview's WinForms window")


def open_db(folder):
    return Database(folder / "budget.db")


def add_category(db, name):
    with db.write("Add category") as c:
        group = c.execute("INSERT INTO category_groups (name) VALUES ('Everyday')").lastrowid
        c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, name))


def categories_in(path):
    copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in copy.execute("SELECT name FROM categories ORDER BY id")]
    finally:
        copy.close()


def names(folder):
    return sorted(p.name for p in Path(folder).iterdir()) if Path(folder).exists() else []


def touch(folder, *filenames):
    folder.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        (folder / filename).write_bytes(filename.encode())


# ── a backup ──

def test_a_backup_is_one_complete_file_with_the_latest_changes(tmp_path):
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    made = backups.at_quit(db, QUIT)
    assert made == tmp_path / backups.FOLDER_NAME / "budget-2026-11-08-14-32-05.db"
    assert names(made.parent) == [made.name]  # no -wal, -shm or half-made copy beside it
    assert categories_in(made) == ["Groceries"]
    copy = sqlite3.connect(made)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    copy.close()


def test_a_backup_copied_back_after_quitting_is_the_budget_you_get(tmp_path):
    """Quitting folds the write-ahead log into budget.db and empties it: a backup copied back
    while the app is closed isn't mixed with what the log held (it would be, otherwise)."""
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    old = backups.at_quit(db, datetime(2026, 11, 7, 9, 0, 0))
    add_category(db, "Rent")
    backups.at_quit(db, QUIT)
    db.conn.close()  # without closing the database properly, as the app quits
    shutil.copyfile(old, tmp_path / "budget.db")
    assert [r[0] for r in Database(tmp_path / "budget.db").conn.execute("SELECT name FROM categories")] == ["Groceries"]


def test_restoring_a_backup_after_a_crash(tmp_path):
    """CLAUDE.md's steps: with the app closed, delete budget.db-wal and budget.db-shm, then copy the
    backup in as budget.db. After a crash the -wal still holds pages that would be mixed in."""
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    good = backups.at_quit(db, datetime(2026, 11, 7, 9, 0, 0))
    add_category(db, "Rent")
    crashed = tmp_path / "after a crash"
    crashed.mkdir()
    for f in ("budget.db", "budget.db-wal", "budget.db-shm"):  # the files as a crash leaves them
        shutil.copyfile(tmp_path / f, crashed / f)
    assert (crashed / "budget.db-wal").stat().st_size > 0
    (crashed / "budget.db-wal").unlink()
    (crashed / "budget.db-shm").unlink()
    shutil.copyfile(good, crashed / "budget.db")
    restored = open_db(crashed)
    assert [r[0] for r in restored.conn.execute("SELECT name FROM categories")] == ["Groceries"]
    assert restored.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.get_meta("schema_version") == str(SCHEMA_VERSION)
    made = backups.at_launch(restored, QUIT)  # the budget as restored gets a backup of its own
    assert made is not None and categories_in(made) == ["Groceries"]


class FullDisk:
    """The database's connection, but VACUUM INTO writes half a file, then finds the disk full."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql.startswith("VACUUM INTO"):
            Path(args[0][0]).write_bytes(b"half a budget")
            raise sqlite3.OperationalError("database or disk is full")
        return self._conn.execute(sql, *args)


def test_a_full_disk_part_way_through_a_copy_leaves_nothing_under_a_backups_name(tmp_path):
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    db.conn = FullDisk(db.conn)
    try:
        assert backups.at_quit(db, QUIT) is None  # tried in the backups folder, then in the data folder
    finally:
        db.conn = db.conn._conn
    assert names(tmp_path / backups.FOLDER_NAME) == [] and names(tmp_path / "Backups") == []
    assert backups.changed(db)  # so the next launch or quit tries again


def test_backups_are_named_with_the_date_and_time():
    assert backups.name(QUIT) == "budget-2026-11-08-14-32-05.db"
    assert backups.upgrade_name(7, 8, QUIT) == "budget-before-upgrade-v7-to-v8-2026-11-08-14-32-05.db"
    assert backups.made_at("budget-2026-11-08-14-32-05.db") == QUIT
    assert backups.made_at("budget-before-upgrade-v6-to-v7-2026-10-12.db") == datetime(2026, 10, 12)  # until 1.3.1
    assert backups.made_at("budget-2026-10-12.db") == datetime(2026, 10, 12)
    assert backups.made_at("budget-2026-10-12 (2).db") == datetime(2026, 10, 12)  # renamed moving them over
    for other in ("budget.db", "budget-2026-10-12.tmp", "budget-2026-13-01.db", "notes.txt", "budget-2026-10-12.db-wal"):
        assert backups.made_at(other) is None


def test_an_upgrade_copy_goes_to_the_backups_folder(tmp_path):
    db = open_db(tmp_path)
    db.conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    db.conn.close()
    open_db(tmp_path)
    assert [n.startswith("budget-before-upgrade-v6-to-v") for n in names(tmp_path / backups.FOLDER_NAME)] == [True]
    assert not (tmp_path / "Backups").exists()


# ── at launch ──

def test_launching_after_a_normal_quit_makes_no_backup(tmp_path):
    db = open_db(tmp_path)
    add_category(db, "Groceries")  # with undo history, which the next launch clears
    backups.at_quit(db, QUIT)
    db.conn.close()
    again = open_db(tmp_path)
    assert backups.at_launch(again, datetime(2026, 11, 9, 8, 0, 0)) is None
    assert names(tmp_path / backups.FOLDER_NAME) == ["budget-2026-11-08-14-32-05.db"]


def test_launching_when_the_budget_changed_since_the_newest_backup_makes_one(tmp_path):
    """The last session didn't quit normally: a crash or a power cut after a change."""
    db = open_db(tmp_path)
    backups.at_quit(db, QUIT)
    add_category(db, "Groceries")
    db.conn.close()  # no quit
    again = open_db(tmp_path)
    made = backups.at_launch(again, datetime(2026, 11, 9, 8, 0, 0))
    assert made is not None and made.name == "budget-2026-11-09-08-00-00.db"
    assert categories_in(made) == ["Groceries"]
    assert backups.at_launch(again, datetime(2026, 11, 9, 8, 5, 0)) is None  # backed up now


def test_the_first_launch_makes_a_backup(tmp_path):
    db = open_db(tmp_path)
    assert backups.at_launch(db, QUIT) is not None


# ── at quit ──

def test_quitting_with_nothing_changed_since_the_newest_backup_makes_none_and_says_so(tmp_path, caplog):
    """Opening the app just to look: the newest backup already holds the budget as it is."""
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    first = backups.at_quit(db, QUIT)
    with caplog.at_level(logging.INFO, logger="inab"):
        assert backups.at_quit(db, datetime(2026, 11, 8, 18, 0, 0)) is None
        assert backups.at_quit(db, datetime(2026, 11, 9, 8, 0, 0)) is None
    assert names(tmp_path / backups.FOLDER_NAME) == [first.name]
    said = [r for r in caplog.records if "No backup" in r.getMessage()]
    assert [r.levelname for r in said] == ["INFO", "INFO"]  # one line at each quit
    assert all(first.name in r.getMessage() for r in said)
    assert not [r for r in caplog.records if "Backed up" in r.getMessage()]


def test_quitting_after_a_change_makes_a_backup_holding_it(tmp_path):
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    first = backups.at_quit(db, QUIT)
    add_category(db, "Rent")
    second = backups.at_quit(db, datetime(2026, 11, 8, 18, 0, 0))
    assert categories_in(first) == ["Groceries"]
    assert categories_in(second) == ["Groceries", "Rent"]
    assert backups.at_quit(db, datetime(2026, 11, 8, 19, 0, 0)) is None  # the second is the newest now


def test_a_change_that_was_undone_is_no_change(tmp_path):
    """The budget is the one in the newest backup, though the write-ahead log holds pages: a quit
    makes no backup, and still folds the log in."""
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    first = backups.at_quit(db, QUIT)
    add_category(db, "Rent")
    db.undo()
    wal = tmp_path / "budget.db-wal"
    assert wal.stat().st_size > 0
    assert backups.at_quit(db, datetime(2026, 11, 8, 18, 0, 0)) is None
    assert names(tmp_path / backups.FOLDER_NAME) == [first.name]
    assert wal.stat().st_size == 0


def test_an_older_backup_deleted_by_hand_is_no_change(tmp_path):
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    older = backups.at_quit(db, datetime(2026, 11, 7, 9, 0, 0))
    add_category(db, "Rent")
    newest = backups.at_quit(db, datetime(2026, 11, 8, 9, 0, 0))
    older.unlink()
    assert not backups.changed(db)
    assert backups.at_quit(db, QUIT) is None
    assert names(tmp_path / backups.FOLDER_NAME) == [newest.name]


@pytest.mark.parametrize("when", ["quit", "launch"])
def test_the_newest_backup_deleted_by_hand_counts_as_a_change(when, tmp_path):
    """Older ones remain, but none holds the budget as it is."""
    db = open_db(tmp_path)
    folder = tmp_path / backups.FOLDER_NAME
    add_category(db, "Groceries")
    older = backups.at_quit(db, datetime(2026, 11, 7, 9, 0, 0))
    add_category(db, "Rent")
    backups.at_quit(db, datetime(2026, 11, 8, 9, 0, 0)).unlink()
    assert backups.changed(db)
    again = (backups.at_quit if when == "quit" else backups.at_launch)(db, QUIT)
    assert again is not None and categories_in(again) == ["Groceries", "Rent"]
    assert names(folder) == sorted([older.name, again.name])
    assert not backups.changed(db)


def test_changed_is_false_right_after_a_backup(tmp_path):
    """What `make` keeps in meta about the backup isn't a change in the budget."""
    db = open_db(tmp_path)
    assert backups.changed(db)  # nothing backed up yet
    backups.make(db, QUIT)
    assert not backups.changed(db)
    add_category(db, "Groceries")
    assert backups.changed(db)
    backups.make(db, datetime(2026, 11, 8, 15, 0, 0))
    assert not backups.changed(db)


def test_the_digest_leaves_out_what_make_keeps_in_meta(tmp_path):
    db = open_db(tmp_path)
    add_category(db, "Groceries")
    before = backups.digest(db)
    backups.make(db, QUIT)
    assert backups.digest(db) == before
    db.set_meta(backups.FILE_KEY, "budget-2026-11-01-10-30-00.db")
    db.set_meta(backups.DIGEST_KEY, "something else")
    assert backups.digest(db) == before
    db.set_meta("onboarded", "1")  # anything else in meta is the budget's
    assert backups.digest(db) != before


def test_the_digest_sees_the_columns_schemas_9_to_11_added(tmp_path):
    db = open_db(tmp_path)
    with db.write() as c:
        account = c.execute("INSERT INTO accounts (name, kind) VALUES ('Chequing', 'chequing')").lastrowid
        imported = c.execute("INSERT INTO imports (account_id, filename, sha256, format) "
                             "VALUES (?, 'made-up.qfx', 'made-up', 'ofx')", (account,)).lastrowid
        payee = c.execute("INSERT INTO payees (name) VALUES ('SAMPLE CAFE CALGARY AB')").lastrowid
        txn = c.execute("INSERT INTO transactions (account_id, date, amount, payee_id, status) "
                        "VALUES (?, '2026-10-01', -500, ?, 'review')", (account, payee)).lastrowid
    backups.make(db, QUIT)
    with db.write() as c:
        c.execute("UPDATE imports SET covers_from = '2026-09-01' WHERE id = ?", (imported,))  # schema 9
    assert backups.changed(db)
    backups.make(db, datetime(2026, 11, 8, 15, 0, 0))
    with db.write() as c:
        c.execute("UPDATE transactions SET after_import = ? WHERE id = ?", (imported, txn))  # schema 10
    assert backups.changed(db)
    backups.make(db, datetime(2026, 11, 8, 16, 0, 0))
    with db.write() as c:
        c.execute("UPDATE imports SET covers_to = '2026-09-30' WHERE id = ?", (imported,))  # schema 11
    assert backups.changed(db)


def test_a_budget_with_no_backup_file_in_meta_counts_as_changed(tmp_path):
    """Backed up by a version that kept only the budget's fingerprint."""
    db = open_db(tmp_path)
    backups.make(db, QUIT)
    db.conn.execute("DELETE FROM meta WHERE key = ?", (backups.FILE_KEY,))
    assert backups.changed(db)
    assert backups.at_quit(db, datetime(2026, 11, 8, 18, 0, 0)) is not None


def test_a_backup_that_fails_at_quit_doesnt_stop_the_rest(tmp_path, monkeypatch, caplog):
    db = open_db(tmp_path)
    folder = tmp_path / backups.FOLDER_NAME
    touch(folder, "budget-2026-05-20-12-00-00.db", "budget-2026-11-07-18-00-00.db")
    add_category(db, "Groceries")
    wal = tmp_path / "budget.db-wal"
    assert wal.stat().st_size > 0

    def disk_full(db, now=None):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backups, "make", disk_full)
    assert backups.at_quit(db, QUIT) is None
    failed = [r for r in caplog.records if "Backing up at quit failed" in r.getMessage()]
    assert len(failed) == 1 and failed[0].exc_info  # one line, with the traceback
    assert names(folder) == ["budget-2026-11-07-18-00-00.db"]  # old ones still go
    assert wal.stat().st_size == 0                             # and the log is still folded in


@PERMISSIONS
def test_a_backups_folder_that_cant_be_read_makes_a_quit_back_up_in_the_data_folder(tmp_path):
    """Documents access refused after a backup was made there: none can be seen, so none holds
    the budget as it is. That's not an error, and not a reason to skip."""
    documents = tmp_path / "Documents" / backups.FOLDER_NAME
    db = Database(tmp_path / "data" / "budget.db", backup_dir=documents)
    add_category(db, "Groceries")
    backups.at_quit(db, datetime(2026, 11, 7, 9, 0, 0))
    assert not backups.changed(db)
    documents.chmod(0o000)
    try:
        assert backups.changed(db)
        made = backups.at_quit(db, QUIT)
    finally:
        documents.chmod(0o700)
    assert made == tmp_path / "data" / "Backups" / "budget-2026-11-08-14-32-05.db"
    assert categories_in(made) == ["Groceries"]


# ── keeping them four months ──

def test_backups_older_than_four_months_go_before_upgrade_copies_included(tmp_path):
    folder = tmp_path / backups.FOLDER_NAME
    touch(folder,
          "budget-2026-07-07-23-59-59.db",                      # gone: before 2026-07-08
          "budget-before-upgrade-v5-to-v6-2026-06-01-10-00-00.db",  # gone
          "budget-before-upgrade-v4-to-v5-2026-05-02.db",          # gone (named as until 1.3.1)
          "budget-2026-05-01.db",                               # gone (a daily one, until 1.3.1)
          "budget-2026-07-08-00-00-00.db",                      # kept: four months ago today
          "budget-before-upgrade-v6-to-v7-2026-10-12.db",          # kept
          "budget-2026-11-07-18-00-00.db",                      # kept: the newest from before today
          "budget-2026-11-08-09-00-00.db",                      # kept: today
          "budget-2020-01-01 (2).db",                           # gone: renamed by a clash moving them over
          "budget-2020-01-01.tmp",                              # gone: the old daily backup's, half made
          "budget-2026-10-31.tmp",                              # kept: from the last four months
          "notes.txt", "budget-2020-01-01.db.bak", "budget-2020-01-01 copy.db")  # not backups: never deleted
    gone = backups.prune(folder, date(2026, 11, 8))
    assert sorted(p.name for p in gone) == [
        "budget-2020-01-01 (2).db", "budget-2026-05-01.db", "budget-2026-07-07-23-59-59.db",
        "budget-before-upgrade-v4-to-v5-2026-05-02.db", "budget-before-upgrade-v5-to-v6-2026-06-01-10-00-00.db"]
    assert names(folder) == [
        "budget-2020-01-01 copy.db", "budget-2020-01-01.db.bak", "budget-2026-07-08-00-00-00.db",
        "budget-2026-10-31.tmp", "budget-2026-11-07-18-00-00.db", "budget-2026-11-08-09-00-00.db",
        "budget-before-upgrade-v6-to-v7-2026-10-12.db", "notes.txt"]


def test_four_months_back_from_the_end_of_a_month():
    assert backups.months_before(date(2026, 6, 30), 4) == date(2026, 2, 28)
    assert backups.months_before(date(2026, 2, 15), 4) == date(2025, 10, 15)


def test_the_newest_backup_from_before_today_survives_a_five_month_break(tmp_path):
    db = open_db(tmp_path)
    folder = tmp_path / backups.FOLDER_NAME
    touch(folder, "budget-2026-05-20-12-00-00.db", "budget-before-upgrade-v6-to-v7-2026-05-25.db")
    last = backups.at_quit(db, datetime(2026, 6, 1, 21, 0, 0))
    assert len(names(folder)) == 3
    db.conn.close()
    # Five months later: launching changes nothing, so no backup, and only the last one stays.
    db = open_db(tmp_path)
    assert backups.at_launch(db, datetime(2026, 11, 8, 8, 0, 0)) is None
    assert names(folder) == [last.name]
    add_category(db, "Groceries")  # a quit with nothing changed makes no backup
    today = backups.at_quit(db, datetime(2026, 11, 8, 22, 0, 0))
    assert names(folder) == sorted([last.name, today.name])
    # The day after, the one from yesterday is the newest from before today: June's goes.
    add_category(db, "Rent")
    tomorrow = backups.at_quit(db, datetime(2026, 11, 9, 22, 0, 0))
    assert names(folder) == sorted([today.name, tomorrow.name])


def test_a_copy_left_half_made_is_cleaned_up(tmp_path):
    folder = tmp_path / backups.FOLDER_NAME
    touch(folder, ".budget-2026-11-08-14-32-05.db.partial", ".budget-2026-11-08-14-32-05.db.partial-journal",
          "budget-2026-11-08-09-00-00.db")
    backups.prune(folder, date(2026, 11, 8))
    assert names(folder) == ["budget-2026-11-08-09-00-00.db"]


# ── what SQLite leaves beside a backup someone opened in place (DB Browser, sqlite3) ──

def side_files(folder, backup, *suffixes, holding=b""):
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in suffixes:
        (folder / (backup + suffix)).write_bytes(holding)


def test_a_pruned_backup_leaves_nothing_behind(tmp_path):
    folder = tmp_path / backups.FOLDER_NAME
    touch(folder, "budget-2026-05-01.db", "budget-2026-11-07-18-00-00.db")
    side_files(folder, "budget-2026-05-01.db", "-wal", "-shm")
    side_files(folder, "budget-2026-11-07-18-00-00.db", "-wal", "-shm", "-journal", holding=b"pages")
    backups.prune(folder, date(2026, 11, 8))
    assert names(folder) == [  # a kept backup keeps its side files
        "budget-2026-11-07-18-00-00.db", "budget-2026-11-07-18-00-00.db-journal",
        "budget-2026-11-07-18-00-00.db-shm", "budget-2026-11-07-18-00-00.db-wal"]


def test_side_files_whose_backup_is_gone_are_deleted(tmp_path):
    folder = tmp_path / backups.FOLDER_NAME
    touch(folder, "budget-2026-11-08-09-00-00.db", "notes.txt")
    side_files(folder, "budget-2026-11-01-10-30-00.db", "-wal", holding=b"pages")  # its backup deleted by hand
    side_files(folder, "budget-2026-04-01.tmp", "-journal")                         # the old daily backup's
    side_files(folder, "notes.txt", "-wal")                                         # not a backup's: kept
    backups.prune(folder, date(2026, 11, 8))
    assert names(folder) == ["budget-2026-11-08-09-00-00.db", "notes.txt", "notes.txt-wal"]


def test_side_files_holding_nothing_are_not_moved_over(tmp_path):
    old, new = tmp_path / "Backups", tmp_path / backups.FOLDER_NAME
    touch(old, "budget-2026-10-30.db", "budget-2026-10-31.db", "budget-2026-11-01.db")
    side_files(old, "budget-2026-10-30.db", "-wal")                     # an empty -wal,
    side_files(old, "budget-2026-10-30.db", "-shm", holding=b"index")   # so its index holds nothing
    side_files(old, "budget-2026-10-31.db", "-shm", holding=b"index")   # an index with no -wal
    side_files(old, "budget-2026-11-01.db", "-journal")                 # an empty journal
    side_files(old, "budget-2026-10-01.db", "-shm", holding=b"index")   # beside nothing
    assert backups.move_old(old, new) == 3
    assert not old.exists()
    assert names(new) == ["budget-2026-10-30.db", "budget-2026-10-31.db", "budget-2026-11-01.db"]


def test_side_files_holding_something_move_with_their_backup(tmp_path):
    old, new = tmp_path / "Backups", tmp_path / backups.FOLDER_NAME
    touch(old, "budget-2026-10-29.db", "budget-2026-10-30.db", "budget-2026-11-01.db")
    side_files(old, "budget-2026-10-29.db", "-journal", holding=b"pages")
    side_files(old, "budget-2026-10-30.db", "-wal", holding=b"pages")
    side_files(old, "budget-2026-10-30.db", "-shm", holding=b"index")   # the index of a -wal with pages
    side_files(old, "budget-2026-11-01.db", "-wal", holding=b"other pages")
    side_files(old, "budget-2026-11-01.db", "-shm", holding=b"other index")
    (new / "budget-2026-11-01.db").parent.mkdir()
    (new / "budget-2026-11-01.db").write_bytes(b"something else")  # a name clash: renamed, its side files too
    assert backups.move_old(old, new) == 3
    assert not old.exists()
    assert names(new) == [
        "budget-2026-10-29.db", "budget-2026-10-29.db-journal",
        "budget-2026-10-30.db", "budget-2026-10-30.db-shm", "budget-2026-10-30.db-wal",
        "budget-2026-11-01 (2).db", "budget-2026-11-01 (2).db-shm", "budget-2026-11-01 (2).db-wal",
        "budget-2026-11-01.db"]
    assert (new / "budget-2026-10-30.db-wal").read_bytes() == b"pages"
    assert (new / "budget-2026-11-01 (2).db-wal").read_bytes() == b"other pages"
    assert (new / "budget-2026-11-01 (2).db-shm").read_bytes() == b"other index"
    backups.prune(new, date(2027, 6, 1))  # later they go together (the newest from before today stays)
    assert names(new) == ["budget-2026-11-01.db"]


def test_a_backup_that_cant_be_moved_keeps_its_side_files(tmp_path, monkeypatch):
    old, new = tmp_path / "Backups", tmp_path / backups.FOLDER_NAME
    touch(old, "budget-2026-10-30.db")
    side_files(old, "budget-2026-10-30.db", "-wal", holding=b"pages")
    side_files(old, "budget-2026-10-30.db", "-shm", holding=b"index")
    real_move = backups._move

    def not_the_backup(source, target):
        if source.name.endswith(".db"):
            raise OSError(errno.EIO, "Input/output error")
        real_move(source, target)

    monkeypatch.setattr(backups, "_move", not_the_backup)
    with pytest.raises(OSError):
        backups.move_old(old, new)
    backups.prune(new, date(2026, 11, 8))
    assert names(old) == ["budget-2026-10-30.db", "budget-2026-10-30.db-shm", "budget-2026-10-30.db-wal"]
    assert names(new) == []


# ── the old folder in the data folder ──

OLD = ["budget-2026-10-30.db", "budget-2026-11-08.db", "budget-before-upgrade-v6-to-v7-2026-10-12.db",
       "budget-2026-10-31.tmp"]


def test_the_old_backups_move_over_once(tmp_path):
    touch(tmp_path / "Backups", *OLD, ".DS_Store")
    db = open_db(tmp_path)
    backups.at_launch(db, QUIT)
    folder = tmp_path / backups.FOLDER_NAME
    assert not (tmp_path / "Backups").exists()
    assert [n for n in names(folder) if n in OLD] == sorted(OLD)
    assert all((folder / n).read_bytes() == n.encode() for n in OLD)  # the files themselves
    before = names(folder)
    backups.at_launch(open_db(tmp_path), datetime(2026, 11, 8, 15, 0, 0))
    assert names(folder) == before  # nothing twice


def test_moving_that_stopped_part_way_loses_nothing(tmp_path):
    old, new = tmp_path / "Backups", tmp_path / backups.FOLDER_NAME
    touch(old, *OLD)
    touch(new, OLD[0])                                  # moved, but not yet deleted from the old folder
    (new / OLD[1]).write_bytes(b"something else")       # not the same file: both are kept
    assert backups.move_old(old, new) == 3
    assert not old.exists()
    assert names(new) == sorted(OLD + ["budget-2026-11-08 (2).db"])
    assert (new / OLD[1]).read_bytes() == b"something else"
    assert (new / "budget-2026-11-08 (2).db").read_bytes() == OLD[1].encode()


def test_moving_to_another_disk_never_leaves_half_a_file_under_a_backups_name(tmp_path, monkeypatch):
    old, new = tmp_path / "Backups", tmp_path / backups.FOLDER_NAME
    touch(old, *OLD)

    def other_disk(source, target):
        raise OSError(errno.EXDEV, "Cross-device link")

    calls = []
    real_replace = os.replace

    def stops_on_the_second_file(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise KeyboardInterrupt  # the app is stopped part way
        real_replace(source, target)

    monkeypatch.setattr(backups.os, "rename", other_disk)
    monkeypatch.setattr(backups.os, "replace", stops_on_the_second_file)
    with pytest.raises(KeyboardInterrupt):
        backups.move_old(old, new)
    monkeypatch.setattr(backups.os, "replace", real_replace)
    assert sorted(names(old) + [n for n in names(new) if not n.endswith(".partial")]) == sorted(OLD)
    assert backups.move_old(old, new) == 3
    backups.prune(new, date(2026, 11, 8))
    assert names(new) == sorted(OLD) and not old.exists()
    assert all((new / n).read_bytes() == n.encode() for n in OLD)


@PERMISSIONS
def test_a_backups_folder_that_cant_be_written_falls_back_to_the_data_folder(tmp_path, caplog):
    """Documents access refused in System Settings: the backup still happens, in the old
    folder, and moves over at a launch once Documents can be written again."""
    documents = tmp_path / "Documents"
    documents.mkdir()
    documents.chmod(0o500)
    try:
        db = Database(tmp_path / "data" / "budget.db", backup_dir=documents / backups.FOLDER_NAME)
        made = backups.at_quit(db, QUIT)
        assert made == tmp_path / "data" / "Backups" / "budget-2026-11-08-14-32-05.db"
        assert not backups.changed(db)  # the one in the data folder holds the budget as it is
        with caplog.at_level(logging.INFO, logger="inab"):
            assert backups.at_launch(db, datetime(2026, 11, 9, 8, 0, 0)) is None  # the one there counts
        assert not [r for r in caplog.records if r.exc_info]  # one line each launch, not a traceback
        assert [r.levelname for r in caplog.records if "old backups" in r.getMessage()] == ["WARNING"]
    finally:
        documents.chmod(0o700)
    backups.at_launch(db, datetime(2026, 11, 9, 8, 0, 0))
    assert names(documents / backups.FOLDER_NAME) == ["budget-2026-11-08-14-32-05.db"]
    assert not (tmp_path / "data" / "Backups").exists()


@PERMISSIONS
def test_the_first_launch_of_1_4_with_documents_refused_then_allowed(tmp_path, caplog):
    """Don't Allow: the copy before the upgrade and the launch backup wait in the data folder's
    Backups/ with 1.3.1's, one line in the log each. Allowed later: the next launch moves them all
    over, and makes no backup."""
    data, documents = tmp_path / "data", tmp_path / "Documents"
    folder = documents / backups.FOLDER_NAME
    documents.mkdir()
    db = Database(data / "budget.db", backup_dir=folder)
    add_category(db, "Groceries")
    db.conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")  # as 1.3.1 left it
    db.conn.close()
    touch(data / "Backups", *OLD)
    documents.chmod(0o500)
    try:
        with caplog.at_level(logging.INFO, logger="inab"):
            db = Database(data / "budget.db", backup_dir=folder)
            made = backups.at_launch(db, datetime(2026, 11, 9, 8, 0, 0))
        assert made == data / "Backups" / "budget-2026-11-09-08-00-00.db"
        upgrade = [n for n in names(data / "Backups") if n.startswith("budget-before-upgrade-v7-to-v")]
        assert len(upgrade) == 1 and set(OLD) <= set(names(data / "Backups"))
        assert not [r for r in caplog.records if r.exc_info]  # lines, not tracebacks
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len([w for w in warnings if w.startswith("Couldn't save a backup in")]) == 2
        assert len([w for w in warnings if w.startswith("Moving the old backups failed")]) == 1
        db.conn.close()
    finally:
        documents.chmod(0o700)
    db = Database(data / "budget.db", backup_dir=folder)
    assert backups.at_launch(db, datetime(2026, 11, 10, 8, 0, 0)) is None
    assert names(folder) == sorted(OLD + upgrade + [made.name]) and not (data / "Backups").exists()
    assert not backups.changed(db)


# ── where they go, and every way of quitting ──

def load_main(monkeypatch, home, data=None):
    monkeypatch.setenv("HOME", str(home))
    if data:
        monkeypatch.setenv("INAB_DATA_DIR", str(data))
    else:
        monkeypatch.delenv("INAB_DATA_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("inab_main_for_backups", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backups_go_to_documents(tmp_path, monkeypatch):
    main = load_main(monkeypatch, tmp_path / "home")
    assert main.BACKUP_DIR == computer.this().documents_dir() / backups.FOLDER_NAME  # the system's Documents
    if sys.platform == "darwin":
        assert main.BACKUP_DIR == tmp_path / "home" / "Documents" / backups.FOLDER_NAME
    assert not (tmp_path / "home").exists()  # working it out writes nothing


def test_with_inab_data_dir_backups_stay_inside_that_folder(tmp_path, monkeypatch):
    main = load_main(monkeypatch, tmp_path / "home", tmp_path / "data")
    assert main.BACKUP_DIR == tmp_path / "data" / backups.FOLDER_NAME
    assert open_db(tmp_path / "preview").backup_dir == tmp_path / "preview" / backups.FOLDER_NAME


class FakeUpdater:
    def __init__(self, folder):
        self.folder, self.installs = folder, []

    def install(self, relaunch_after=False):
        self.installs.append((relaunch_after, names(self.folder)))  # what was backed up by then
        return True


class FakeComputer:
    """This computer, as far as quitting goes: what's sent to the window's thread runs straight away."""
    terminated = False

    def on_main_thread(self, fn, *args):
        fn(*args)

    def quit(self):
        self.terminated = True


ROUTES = ["close button", pytest.param("⌘Q", marks=MAC), pytest.param("Quit in the Dock", marks=MAC), "Restart now",
          "quit to install an update", pytest.param("logging out or shutting down", marks=MAC),
          pytest.param("✕ or Alt+F4", marks=WINDOWS), pytest.param("logging off or shutting down", marks=WINDOWS),
          pytest.param("Task Manager", marks=WINDOWS)]
# What Windows sends the window for each: (message, wParam, lParam).
SC_CLOSE = (0x0112, 0xF060, 0)                  # WM_SYSCOMMAND: ✕, Alt+F4, the taskbar's Close window
WM_CLOSE = (0x0010, 0, 0)                       # without SC_CLOSE first: Task Manager, taskkill
QUERY_LOG_OFF = (0x0011, 0, 0x80000000)         # WM_QUERYENDSESSION, ENDSESSION_LOGOFF: may the session end?
LOGGING_OFF = (0x0016, 1, 0x80000000)           # WM_ENDSESSION: it does
LOG_OFF_CALLED_OFF = (0x0016, 0, 0x80000000)    # WM_ENDSESSION: another app stopped it


class AppleEvent:
    """The Apple event a quit comes with: logging out, restarting or shutting down gives a reason."""

    def __init__(self, reason):
        self.reason = reason

    def attributeDescriptorForKeyword_(self, keyword):
        return self.reason if keyword == int.from_bytes(b"why?", "big") else None


def the_mac_asks_to_quit(monkeypatch, app, event):
    """What macOS does for ⌘Q, Quit in the Dock, logging out and shutting down: it asks the app's
    delegate (computer_mac's applicationShouldTerminate_, which asks main.py's _should_quit), with
    the Apple event the quit came with."""
    import AppKit
    from webview.platforms import cocoa

    from computer_mac import Mac

    class Events:
        @staticmethod
        def sharedAppleEventManager():
            return Events()

        def currentAppleEvent(self):
            return event

    Mac().when_asked_to_quit(app._should_quit, app._quit_called_off)
    monkeypatch.setattr(AppKit, "NSAppleEventManager", Events)
    return cocoa.BrowserView.AppDelegate.alloc().init().applicationShouldTerminate_(None)


def a_windows_window(monkeypatch, app):
    """The app's window as Windows has it: a real WinForms form, whose closing goes the way
    pywebview's does (FormClosing → computer_windows' on_closing → pywebview's own, which raises
    the closing event main.py's _on_closing listens to, and cancels the close when it says so).
    Windows.quit() closes it as pywebview's destroy() does, with Close()."""
    from types import SimpleNamespace

    import webview
    from webview.event import Event
    from webview.platforms import winforms

    import System.Windows.Forms as WinForms
    from computer_windows import Windows

    form = winforms.BrowserView.BrowserForm
    monkeypatch.setattr(form, "on_closing", form.on_closing)  # pywebview's own again after the test
    monkeypatch.setattr(form, "_pywebview_on_closing", None, raising=False)

    class ThisWindows(Windows):  # the window's thread is the test's
        def on_main_thread(self, fn, *args):
            fn(*args)

    app._computer = ThisWindows()
    app._computer.when_asked_to_quit(app._should_quit, app._quit_called_off)
    window = WinForms.Form()
    pywebview_side = SimpleNamespace(closing=Event(None, True), pywebview_window=SimpleNamespace(confirm_close=False))
    pywebview_side.closing += app._on_closing
    window.FormClosing += lambda sender, args: form.on_closing(pywebview_side, sender, args)
    monkeypatch.setattr(webview, "windows", [SimpleNamespace(destroy=window.Close)])
    monkeypatch.setattr(logging.getLogger("pywebview"), "setLevel", lambda level: None)
    return window


def windows_sends(window, message):
    """Windows' message to the window, on its thread (the test's); returns the window's answer."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    return user32.SendMessageW(window.Handle.ToInt64(), *message)


def start_app(monkeypatch, tmp_path):
    """The app as main.py builds it, without its window: a budget in a throwaway folder."""
    main = load_main(monkeypatch, tmp_path / "home", tmp_path / "data")
    app = main.BudgetApp.__new__(main.BudgetApp)
    app._computer = FakeComputer()
    app._db = Database(main.DATA_DIR / "budget.db", backup_dir=main.BACKUP_DIR)
    app._updater = FakeUpdater(main.BACKUP_DIR)
    app._window, app._quitting, app._quit_asked = None, False, None
    return main, app, app._computer


def quit_by(route, main, app, this_computer, monkeypatch):
    asked = []
    monkeypatch.setattr(app, "_ask_page_to_quit", lambda: asked.append(route) or False)  # a page that's there
    if route == "close button":
        assert app._on_closing() is False and asked  # the page saves what you were typing, then quits
        main.Api(app).quit()
        assert this_computer.terminated
    elif route in ("⌘Q", "Quit in the Dock"):  # the Dock's comes as an Apple event with no reason
        import AppKit

        event = AppleEvent(None) if route == "Quit in the Dock" else None
        assert the_mac_asks_to_quit(monkeypatch, app, event) == AppKit.NSTerminateCancel
        assert asked and not app._updater.installs  # nothing until the page answers
        main.Api(app).quit()
        assert this_computer.terminated
    elif route == "Restart now":
        app._restart_into_update()
        assert this_computer.terminated
    elif route == "logging out or shutting down":  # straight away, without asking the page: the Mac is waiting
        import AppKit

        assert the_mac_asks_to_quit(monkeypatch, app, AppleEvent("logging out")) == AppKit.NSTerminateNow
        assert not asked
    elif route == "✕ or Alt+F4":  # the close button's way
        window = a_windows_window(monkeypatch, app)
        windows_sends(window, SC_CLOSE)
        assert not window.IsDisposed  # open while the page saves what you were typing
        assert asked and not app._updater.installs  # nothing until the page answers
        main.Api(app).quit()  # Windows.quit(): Close() comes back through FormClosing, which lets it close
        assert window.IsDisposed and asked == [route]
    elif route == "Task Manager":  # straight away, without asking the page: Windows is waiting
        window = a_windows_window(monkeypatch, app)
        windows_sends(window, WM_CLOSE)
        assert window.IsDisposed and not asked
    elif route == "logging off or shutting down":  # straight away: the backup's made before Windows is answered
        window = a_windows_window(monkeypatch, app)
        assert windows_sends(window, QUERY_LOG_OFF) == 1  # it may end
        assert names(main.BACKUP_DIR) and not asked
        windows_sends(window, LOGGING_OFF)
        assert window.IsDisposed
    else:
        app._before_quit()
    app._before_quit()  # asked again while quitting: nothing more


def test_the_close_button_with_no_page_to_ask_quits_straight_away(tmp_path, monkeypatch):
    main, app, this_computer = start_app(monkeypatch, tmp_path)
    add_category(app._db, "Groceries")
    assert app._on_closing() is True  # the window closes, and the app quits
    made = names(main.BACKUP_DIR)
    assert len(made) == 1 and app._updater.installs == [(False, made)]


@pytest.mark.parametrize("route", ROUTES)
def test_every_way_of_quitting_leaves_a_backup_with_the_latest_changes(route, tmp_path, monkeypatch):
    main, app, this_computer = start_app(monkeypatch, tmp_path)
    add_category(app._db, "Groceries")
    quit_by(route, main, app, this_computer, monkeypatch)

    made = names(main.BACKUP_DIR)
    assert len(made) == 1 and categories_in(main.BACKUP_DIR / made[0]) == ["Groceries"]
    relaunch = route == "Restart now"
    assert app._updater.installs[0] == (relaunch, made)  # the update goes in after the backup
    assert not (tmp_path / "home").exists()  # nothing near the real Documents


@pytest.mark.parametrize("route", ROUTES)
def test_every_way_of_quitting_with_nothing_changed_makes_no_backup_and_the_update_still_goes_in(
        route, tmp_path, monkeypatch):
    main, app, this_computer = start_app(monkeypatch, tmp_path)
    add_category(app._db, "Groceries")
    earlier = backups.at_quit(app._db, QUIT)  # the last time the app quit
    quit_by(route, main, app, this_computer, monkeypatch)

    assert names(main.BACKUP_DIR) == [earlier.name]
    relaunch = route == "Restart now"
    assert app._updater.installs[0] == (relaunch, [earlier.name])
    assert not (tmp_path / "home").exists()  # nothing near the real Documents


@WINDOWS
def test_a_log_off_another_app_stops_is_called_off_and_the_app_carries_on(tmp_path, monkeypatch):
    """Another app stops the log-off and the person cancels: Windows tells the app the session
    isn't ending after all. It carries on: the next ✕ asks the page again and makes a backup with
    what changed since, and Downloads is watched again."""
    main, app, this_computer = start_app(monkeypatch, tmp_path)
    window = a_windows_window(monkeypatch, app)
    add_category(app._db, "Groceries")
    assert windows_sends(window, QUERY_LOG_OFF) == 1
    assert app._quitting and len(names(main.BACKUP_DIR)) == 1
    windows_sends(window, LOG_OFF_CALLED_OFF)
    assert not window.IsDisposed and not app._quitting  # still open, and not quitting

    watched = []
    app._service = type("Service", (), {"watch_downloads": lambda self: watched.append(1)})()
    monkeypatch.setattr(main.time, "sleep", lambda seconds: (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):
        app._watch()
    assert watched  # the watcher's next look

    add_category(app._db, "Rent")
    real_at_quit = backups.at_quit
    monkeypatch.setattr(backups, "at_quit", lambda db: real_at_quit(db, QUIT))  # a later second than the first
    asked = []
    monkeypatch.setattr(app, "_ask_page_to_quit", lambda: asked.append("✕") or False)
    windows_sends(window, SC_CLOSE)
    assert asked and not window.IsDisposed  # through the page, as before the log-off
    main.Api(app).quit()
    assert window.IsDisposed
    assert len(names(main.BACKUP_DIR)) == 2
    assert categories_in(main.BACKUP_DIR / backups.name(QUIT)) == ["Groceries", "Rent"]


@WINDOWS
def test_a_second_log_off_after_one_called_off_quits(tmp_path, monkeypatch):
    main, app, this_computer = start_app(monkeypatch, tmp_path)
    window = a_windows_window(monkeypatch, app)
    windows_sends(window, QUERY_LOG_OFF)
    windows_sends(window, LOG_OFF_CALLED_OFF)
    add_category(app._db, "Groceries")
    assert windows_sends(window, QUERY_LOG_OFF) == 1
    windows_sends(window, LOGGING_OFF)
    assert window.IsDisposed
    newest = sorted(names(main.BACKUP_DIR))[-1]
    assert categories_in(main.BACKUP_DIR / newest) == ["Groceries"]


def test_a_backup_restored_on_another_mac_keeps_the_overspending_choice(app, tmp_path):
    """Overspending and Usual's months are the budget's own (issue #153): a backup carries them."""
    from helpers import call
    from service import BudgetService
    from settings import Settings

    wallet = call(app, "add_account", "Wallet", "cash", 100000, "2026-08-31")
    call(app, "finish_onboarding", "2026-09")
    food = call(app, "add_category", call(app, "add_group", "Everyday"), "Food")
    call(app, "add_transaction", wallet, "2026-09-10", "SAMPLE CAFE CALGARY AB", -5000, food)  # overspent
    call(app, "save_settings", {"overspending": "carry_over"})

    def october(service):
        month = call(service, "month", "2026-10")
        row = next(c for g in month["groups"] for c in g["categories"] if c["id"] == food)
        return month["summary"]["ready_to_assign"], row["available"]

    assert october(app) == (100000, -5000)
    another_mac = tmp_path / "another Mac"
    another_mac.mkdir()
    shutil.copyfile(backups.at_quit(app.db, QUIT), another_mac / "budget.db")
    restored = BudgetService(Database(another_mac / "budget.db"), Settings(another_mac / "settings.json"), another_mac)
    assert october(restored) == (100000, -5000)
