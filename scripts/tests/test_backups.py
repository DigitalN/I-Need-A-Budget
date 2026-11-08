# SPDX-License-Identifier: GPL-3.0-only
"""Backups at quit, into Documents (issue #21, docs/adr/0004). Made-up data and dates, in
throwaway folders: nothing here reads or writes the real data folder or Documents."""

import errno
import importlib.util
import os
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

import backups
from db import Database

MAIN = Path(__file__).resolve().parents[2] / "Application Files" / "main.py"
QUIT = datetime(2026, 11, 8, 14, 32, 5)


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


def test_backups_are_named_with_the_date_and_time():
    assert backups.name(QUIT) == "budget-2026-11-08-14-32-05.db"
    assert backups.upgrade_name(7, 8, QUIT) == "budget-before-upgrade-v7-to-v8-2026-11-08-14-32-05.db"
    assert backups.made_at("budget-2026-11-08-14-32-05.db") == QUIT
    assert backups.made_at("budget-before-upgrade-v6-to-v7-2026-10-12.db") == datetime(2026, 10, 12)  # until 1.3.1
    assert backups.made_at("budget-2026-10-12.db") == datetime(2026, 10, 12)
    for other in ("budget.db", "budget-2026-10-12.tmp", "budget-2026-13-01.db", "notes.txt", "budget-2026-10-12 (2).db"):
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
          "notes.txt", "budget-2020-01-01.tmp", "budget-2020-01-01 (2).db")  # not backups: never deleted
    gone = backups.prune(folder, date(2026, 11, 8))
    assert sorted(p.name for p in gone) == [
        "budget-2026-05-01.db", "budget-2026-07-07-23-59-59.db",
        "budget-before-upgrade-v4-to-v5-2026-05-02.db", "budget-before-upgrade-v5-to-v6-2026-06-01-10-00-00.db"]
    assert names(folder) == [
        "budget-2020-01-01 (2).db", "budget-2020-01-01.tmp", "budget-2026-07-08-00-00-00.db",
        "budget-2026-11-07-18-00-00.db", "budget-2026-11-08-09-00-00.db",
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
    today = backups.at_quit(db, datetime(2026, 11, 8, 22, 0, 0))
    assert names(folder) == sorted([last.name, today.name])
    # The day after, the one from yesterday is the newest from before today: June's goes.
    tomorrow = backups.at_quit(db, datetime(2026, 11, 9, 22, 0, 0))
    assert names(folder) == sorted([today.name, tomorrow.name])


def test_a_copy_left_half_made_is_cleaned_up(tmp_path):
    folder = tmp_path / backups.FOLDER_NAME
    touch(folder, ".budget-2026-11-08-14-32-05.db.partial", "budget-2026-11-08-09-00-00.db")
    backups.prune(folder, date(2026, 11, 8))
    assert names(folder) == ["budget-2026-11-08-09-00-00.db"]


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


def test_a_backups_folder_that_cant_be_written_falls_back_to_the_data_folder(tmp_path):
    """Documents access refused in System Settings: the backup still happens, in the old
    folder, and moves over at a launch once Documents can be written again."""
    documents = tmp_path / "Documents"
    documents.mkdir()
    documents.chmod(0o500)
    try:
        db = Database(tmp_path / "data" / "budget.db", backup_dir=documents / backups.FOLDER_NAME)
        made = backups.at_quit(db, QUIT)
        assert made == tmp_path / "data" / "Backups" / "budget-2026-11-08-14-32-05.db"
        assert backups.at_launch(db, datetime(2026, 11, 9, 8, 0, 0)) is None  # the one there counts
    finally:
        documents.chmod(0o700)
    backups.at_launch(db, datetime(2026, 11, 9, 8, 0, 0))
    assert names(documents / backups.FOLDER_NAME) == ["budget-2026-11-08-14-32-05.db"]
    assert not (tmp_path / "data" / "Backups").exists()


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


class FakeNSApp:
    terminated = False

    def terminate_(self, sender):
        self.terminated = True


@pytest.mark.parametrize("route", ["close button", "⌘Q", "Restart now", "quit to install an update"])
def test_every_way_of_quitting_leaves_a_backup_with_the_latest_changes(route, tmp_path, monkeypatch):
    AppKit = pytest.importorskip("AppKit")
    from PyObjCTools import AppHelper

    main = load_main(monkeypatch, tmp_path / "home", tmp_path / "data")
    app = main.BudgetApp.__new__(main.BudgetApp)
    app._db = Database(main.DATA_DIR / "budget.db", backup_dir=main.BACKUP_DIR)
    app._updater = FakeUpdater(main.BACKUP_DIR)
    app._window, app._quitting, app._quit_asked = None, False, None
    ns_app = FakeNSApp()
    monkeypatch.setattr(AppKit, "NSApp", ns_app)
    monkeypatch.setattr(AppHelper, "callAfter", lambda fn, *args: fn(*args))
    add_category(app._db, "Groceries")

    if route == "close button":  # with no page to ask, the window closes and the app quits
        assert app._on_closing() is True
    elif route == "⌘Q":  # the page has saved what you were typing
        main.Api(app).quit()
        assert ns_app.terminated
    elif route == "Restart now":
        app._restart_into_update()
        assert ns_app.terminated
    else:
        app._before_quit()
    app._before_quit()  # asked again while quitting: nothing more

    made = names(main.BACKUP_DIR)
    assert len(made) == 1 and categories_in(main.BACKUP_DIR / made[0]) == ["Groceries"]
    relaunch = route == "Restart now"
    assert app._updater.installs[0] == (relaunch, made)  # the update goes in after the backup
    assert not (tmp_path / "home").exists()  # nothing near the real Documents
