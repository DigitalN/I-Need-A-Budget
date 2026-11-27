# SPDX-License-Identifier: GPL-3.0-only
"""The budget folder (issue #183, docs/adr/0008): saving the budget whole into a folder you choose,
and two computers sharing it. Two data folders sharing one budget folder stand in for two computers;
a second folder copied from the first stands in for the same synced folder seen on another computer
(at another path, or before the sync tool has caught up). Made-up data, throwaway folders."""

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backups
import budget_folder
from budget_folder import FILE_NAME, BudgetFolder
from db import SCHEMA_VERSION, Database
from errors import UserError
from helpers import bank_csv, call, write
from service import BudgetService
from settings import Settings

NOW = datetime(2026, 11, 27, 9, 14, 5, tzinfo=timezone(timedelta(hours=-7)))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def computer(tmp_path, name, folder=None, clock=None, sleep=lambda s: None):
    """One computer: its own data folder and working copy, and the budget folder it uses."""
    data = tmp_path / name
    return BudgetFolder(Database(data / "budget.db"), data, folder, computer=name, app_version="1.5.0",
                        clock=clock or Clock(), now=lambda: NOW, sleep=sleep)


def add_category(db, name):
    with db.write("Add category") as c:
        group = c.execute("SELECT id FROM category_groups WHERE name = 'Everyday'").fetchone()
        group = group[0] if group else c.execute("INSERT INTO category_groups (name) VALUES ('Everyday')").lastrowid
        c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, name))


def categories(db):
    with db.read() as c:
        return [r[0] for r in c.execute("SELECT name FROM categories ORDER BY id")]


def categories_in(path):
    with closing(sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)) as c:
        return [r[0] for r in c.execute("SELECT name FROM categories ORDER BY id")]


def save_in(path):
    with closing(sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)) as c:
        return json.loads(c.execute("SELECT value FROM meta WHERE key = 'save'").fetchone()[0])


def names(folder):
    return sorted(p.name for p in Path(folder).iterdir()) if Path(folder).exists() else []


def not_kept(folder):
    return [folder / "Backups" / n for n in names(folder / "Backups") if n.startswith("budget-not-kept-")]


def saved_elsewhere(folder, computer_name, app_version="1.5.0", schema_version=SCHEMA_VERSION, category=None):
    """Another computer's save over Budget.inab, built on the one there, as that computer would make it."""
    target = folder / FILE_NAME
    scratch = target.with_name(".elsewhere.partial")
    shutil.copyfile(target, scratch)
    with closing(sqlite3.connect(scratch, isolation_level=None)) as c:
        if category:
            group = c.execute("INSERT INTO category_groups (name) VALUES ('Elsewhere')").lastrowid
            c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, category))
        before = json.loads(c.execute("SELECT value FROM meta WHERE key = 'save'").fetchone()[0])
        save_id = uuid.uuid4().hex
        c.execute("UPDATE meta SET value = ? WHERE key = 'save'", (json.dumps({
            "id": save_id, "history": [save_id] + before["history"], "computer": computer_name,
            "saved_at": NOW.isoformat(), "app_version": app_version, "schema_version": schema_version}),))
        c.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(schema_version),))
    os.replace(scratch, target)


# ── saving ──

def test_a_save_is_one_complete_file_and_nothing_beside_it(tmp_path):
    folder = tmp_path / "OneDrive" / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    state = a.move_to(folder)
    assert state["state"] == "saved" and not state["unsaved"]
    assert state["saved"] == {"computer": "PC-A", "saved_at": "2026-11-27T09:14:05-07:00", "app_version": "1.5.0"}
    assert names(folder) == [".Budget open on PC-A.json", FILE_NAME]  # no -wal, -shm or half-made copy
    saved = folder / FILE_NAME
    assert categories_in(saved) == ["Groceries"]
    with closing(sqlite3.connect(f"{saved.as_uri()}?mode=ro", uri=True)) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert c.execute("SELECT COUNT(*) FROM undo_log").fetchone()[0] == 0  # this session's undo stays here
    first = save_in(saved)
    assert {k: first[k] for k in ("computer", "saved_at", "app_version", "schema_version")} == {
        "computer": "PC-A", "saved_at": "2026-11-27T09:14:05-07:00", "app_version": "1.5.0",
        "schema_version": SCHEMA_VERSION}
    assert first["history"] == [first["id"]]
    assert (a.db.path.parent / "Saves" / f"{first['id']}.inab").is_file()  # the save it started from

    add_category(a.db, "Rent")
    a.check(wait=False)
    assert names(folder) == [".Budget open on PC-A.json", FILE_NAME]
    assert categories_in(saved) == ["Groceries", "Rent"]
    second = save_in(saved)
    assert second["history"] == [second["id"], first["id"]]


def test_nothing_is_saved_when_nothing_changed(tmp_path):
    """What's kept about saves and backups isn't a change: a backup at quit doesn't make a save."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    saved = folder / FILE_NAME
    before, written = saved.read_bytes(), saved.stat().st_mtime_ns
    for _ in range(3):
        assert a.check(wait=False)["state"] == "saved"
    backups.make(a.db)
    assert a.check(wait=False) == {**a.state(), "state": "saved"}
    a.at_quit()
    assert saved.read_bytes() == before and saved.stat().st_mtime_ns == written
    assert names(folder) == ["Backups", FILE_NAME]  # this computer's note went at quit


def test_a_save_waits_until_changes_stop(tmp_path):
    clock = Clock()
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A", clock=clock)
    a.move_to(folder)
    add_category(a.db, "Groceries")
    assert a.check()["state"] == "unsaved"
    clock.t = 3
    add_category(a.db, "Rent")
    assert a.check()["state"] == "unsaved"
    clock.t = 6
    assert a.check()["state"] == "unsaved"  # three seconds since the last change
    assert categories_in(folder / FILE_NAME) == []
    clock.t = 8
    assert a.check()["state"] == "saved"
    assert categories_in(folder / FILE_NAME) == ["Groceries", "Rent"]


# ── two computers ──

def test_two_computers_take_turns_without_being_asked(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    assert b.open_from(folder)["state"] == "saved"

    add_category(a.db, "Groceries")
    a.check(wait=False)
    state = b.check()
    assert state["state"] == "saved" and state["took"]["computer"] == "PC-A"
    assert categories(b.db) == ["Groceries"]

    add_category(b.db, "Rent")
    b.check(wait=False)
    state = a.check()
    assert state["state"] == "saved" and state["took"]["computer"] == "PC-B"
    assert categories(a.db) == ["Groceries", "Rent"]

    saved = (folder / FILE_NAME).read_bytes()
    assert a.check()["state"] == b.check()["state"] == "saved"
    assert (folder / FILE_NAME).read_bytes() == saved  # taking it isn't a change to save again
    assert not_kept(folder) == []  # never asked


def _changed_on_both(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    add_category(a.db, "Groceries")
    a.check(wait=False)
    add_category(b.db, "Rent")
    return folder, a, b


def test_changed_on_two_computers_shows_both_and_saves_neither(tmp_path):
    folder, a, b = _changed_on_both(tmp_path)
    state = b.check(wait=False)
    assert state["state"] == "two_computers" and state["unsaved"]
    assert state["other"] == {"computer": "PC-A", "saved_at": "2026-11-27T09:14:05-07:00", "app_version": "1.5.0"}
    assert b.check(wait=False)["state"] == "two_computers"
    assert categories_in(folder / FILE_NAME) == ["Groceries"]  # not saved over
    assert categories(b.db) == ["Rent"]  # nor taken
    files = b.two_computers()
    assert categories_in(files["started_from"]) == []
    assert categories_in(files["this"]) == ["Rent"]
    assert categories_in(files["other"]) == ["Groceries"]
    assert files["other_save"]["computer"] == "PC-A"


def test_keeping_this_computers_puts_the_other_in_backups(tmp_path):
    folder, a, b = _changed_on_both(tmp_path)
    b.check(wait=False)
    assert b.keep_this()["state"] == "saved"
    assert categories_in(folder / FILE_NAME) == ["Rent"]
    [other] = not_kept(folder)
    assert categories_in(other) == ["Groceries"]
    state = a.check()  # PC-A changed nothing since its save: it takes the one kept
    assert state["state"] == "saved" and state["took"]["computer"] == "PC-B"
    assert categories(a.db) == ["Rent"]
    with pytest.raises(UserError, match="nothing to choose"):
        b.keep_this()


def test_keeping_the_other_computers_puts_this_one_in_backups(tmp_path):
    folder, a, b = _changed_on_both(tmp_path)
    b.check(wait=False)
    saved = (folder / FILE_NAME).read_bytes()
    assert b.keep_other()["state"] == "saved"
    assert categories(b.db) == ["Groceries"] and b.db.undo_label() is None
    assert (folder / FILE_NAME).read_bytes() == saved
    [mine] = not_kept(folder)
    assert categories_in(mine) == ["Rent"]
    assert "took" not in a.check()  # nothing new for PC-A


@pytest.mark.parametrize("copy", ["Budget-PC-B.inab", "Budget 2.inab",
                                  "Budget (PC-B's conflicted copy 2026-11-27).inab"])
def test_a_sync_tools_conflict_copy_is_another_computers_save(tmp_path, copy):
    """OneDrive's, iCloud's and Dropbox's names. Each computer saved before seeing the other's; the
    sync tool kept PC-A's as Budget.inab and made a copy of PC-B's."""
    folder, seen_on_b = tmp_path / "Budget", tmp_path / "PC-B sees" / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    shutil.copytree(folder, seen_on_b)
    b = computer(tmp_path, "PC-B")
    b.open_from(seen_on_b)
    add_category(a.db, "Groceries")
    a.check(wait=False)
    add_category(b.db, "Rent")
    b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / copy)

    state = a.check()
    assert state["state"] == "two_computers" and state["other"]["computer"] == "PC-B"
    files = a.two_computers()
    assert categories_in(files["started_from"]) == []  # the save both started from, before either change
    assert categories_in(files["this"]) == ["Groceries"] and categories_in(files["other"]) == ["Rent"]
    assert a.keep_this()["state"] == "saved"
    assert copy not in names(folder) and categories_in(folder / FILE_NAME) == ["Groceries"]
    assert [categories_in(p) for p in not_kept(folder)] == [["Rent"]]

    shutil.copyfile(folder / FILE_NAME, seen_on_b / FILE_NAME)  # the sync tool takes it to PC-B
    state = b.check()
    assert state["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries"]


def test_a_sync_tools_copy_of_this_computers_own_save_is_put_away(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    shutil.copyfile(folder / FILE_NAME, folder / "Budget 2.inab")
    assert a.check()["state"] == "saved"
    assert "Budget 2.inab" not in names(folder)
    assert [categories_in(p) for p in not_kept(folder)] == [["Groceries"]]  # kept, not deleted


def test_another_computers_note_is_read_back(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    state = b.open_from(folder)
    assert state["open_elsewhere"] == [{"computer": "PC-A", "since": "2026-11-27T09:14:05-07:00"}]
    assert a.others_open() == [{"computer": "PC-B", "since": "2026-11-27T09:14:05-07:00"}]
    b.at_quit()
    assert a.others_open() == []
    relaunched = BudgetFolder(b.db, b.data_dir, folder, computer="PC-B", now=lambda: NOW)
    assert relaunched.at_launch()["open_elsewhere"] == [{"computer": "PC-A", "since": "2026-11-27T09:14:05-07:00"}]


# ── a folder that goes away ──

def test_a_folder_that_goes_away_and_comes_back(tmp_path):
    folder, away = tmp_path / "Drive" / "Budget", tmp_path / "Unplugged"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    folder.rename(away)
    add_category(a.db, "Groceries")
    state = a.check(wait=False)
    assert state["state"] == "unreachable" and state["unsaved"]
    made = backups.make(a.db)
    assert made.parent == a.data_dir / "Backups"  # backups wait in the data folder
    assert a.imports_dir() == a.data_dir / "Imported Files"
    write_kept(a.data_dir / "Imported Files" / "2026-11", "2026-11-26 101500 statement.ofx")
    assert not folder.exists()  # the app never makes the folder again
    relaunched = BudgetFolder(a.db, a.data_dir, folder, computer="PC-A", now=lambda: NOW)
    assert relaunched.at_launch()["state"] == "unreachable" and not folder.exists()

    away.rename(folder)
    assert relaunched.check()["state"] == "saved"
    assert categories_in(folder / FILE_NAME) == ["Groceries"]
    assert made.name in names(folder / "Backups") and not (a.data_dir / "Backups").exists()
    assert names(folder / "Imported Files" / "2026-11") == ["2026-11-26 101500 statement.ofx"]
    assert not (a.data_dir / "Imported Files").exists()
    assert ".Budget open on PC-A.json" in names(folder)


def write_kept(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("made-up file", encoding="utf-8")
    return folder / name


# ── versions ──

def test_a_budget_saved_by_a_newer_version_is_refused(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    saved_elsewhere(folder, "PC-C", app_version="9.0.0", schema_version=SCHEMA_VERSION + 1, category="Gifts")
    saved = (folder / FILE_NAME).read_bytes()
    add_category(a.db, "Rent")
    state = a.check(wait=False)
    assert state["state"] == "newer" and state["other"]["app_version"] == "9.0.0"
    assert state["message"] == "This budget was saved by I Need A Budget 9.0.0 on PC-C. Update this copy to open it."
    assert (folder / FILE_NAME).read_bytes() == saved  # never saved over
    assert categories(a.db) == ["Rent"]  # nor taken
    with pytest.raises(UserError, match="9.0.0"):
        computer(tmp_path, "PC-D").open_from(folder)


def test_a_budget_saved_by_an_older_version_is_upgraded_with_a_copy_first(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    saved_elsewhere(folder, "PC-B", schema_version=SCHEMA_VERSION - 1, category="Gifts")
    saved = (folder / FILE_NAME).read_bytes()
    state = a.check()
    assert state["took"]["computer"] == "PC-B" and categories(a.db) == ["Gifts"]
    assert a.db.get_meta("schema_version") == str(SCHEMA_VERSION)
    upgrade = f"budget-before-upgrade-v{SCHEMA_VERSION - 1}-to-v{SCHEMA_VERSION}-"
    assert [n for n in names(folder / "Backups") if n.startswith(upgrade)]
    assert not a.check(wait=False)["unsaved"]  # the upgrade isn't a change to save
    assert (folder / FILE_NAME).read_bytes() == saved


# ── moving in, opening ──

def test_moving_in_refuses_a_folder_that_already_has_a_budget(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    c = computer(tmp_path, "PC-C")
    add_category(c.db, "Rent")
    assert c.look_in(folder)["budget"]["computer"] == "PC-A"
    before = {n: (folder / n).read_bytes() for n in names(folder)}
    with pytest.raises(UserError) as refused:
        c.move_to(folder)
    assert str(refused.value) == "This folder already has a budget, saved by PC-A on Nov 27, 2026 at 9:14 AM."
    assert {n: (folder / n).read_bytes() for n in names(folder)} == before and c.folder is None

    only_a_copy = tmp_path / "Other"
    only_a_copy.mkdir()
    shutil.copyfile(folder / FILE_NAME, only_a_copy / "Budget (PC-A's conflicted copy 2026-11-27).inab")
    with pytest.raises(UserError, match="already has a budget"):
        c.move_to(only_a_copy)


def test_moving_in_brings_the_backups_and_the_imported_files(tmp_path):
    folder, documents = tmp_path / "Budget", tmp_path / "Documents" / "I Need A Budget Backups"
    a = computer(tmp_path, "PC-A")
    a.db.backup_dir = documents  # where main.py put them until 1.5.0
    add_category(a.db, "Groceries")
    made = backups.make(a.db)
    kept = write_kept(a.data_dir / "Imported Files" / "2026-10", "2026-10-05 093000 statement.ofx")
    a.move_to(folder)
    assert names(folder / "Backups") == [made.name] and not documents.exists()
    assert names(folder / "Imported Files" / "2026-10") == [kept.name]
    assert not (a.data_dir / "Imported Files").exists()
    assert a.db.backup_dir == folder / "Backups"


def test_moving_again_leaves_a_note_where_it_was(tmp_path):
    old, new = tmp_path / "Old" / "Budget", tmp_path / "New" / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(old)
    write_kept(old / "Imported Files" / "2026-11", "2026-11-26 101500 statement.ofx")
    b = computer(tmp_path, "PC-B")
    b.open_from(old)
    a.move_to(new)
    assert names(old) == [".Budget moved.json", ".Budget open on PC-B.json"]
    assert names(new) == [".Budget open on PC-A.json", FILE_NAME, "Imported Files"]
    state = b.check(wait=False)
    assert state["state"] == "moved" and state["moved"]["computer"] == "PC-A"
    add_category(b.db, "Rent")
    assert b.check(wait=False)["state"] == "moved" and FILE_NAME not in names(old)  # not put back
    assert b.open_from(new)["state"] == "two_computers"  # PC-B changed it since: which to keep?


def test_opening_a_budget_on_a_new_computer(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    c = computer(tmp_path, "PC-C")  # new: nothing in it yet
    assert c.open_from(folder)["state"] == "saved"
    assert categories(c.db) == ["Groceries"] and not_kept(folder) == []

    d = computer(tmp_path, "PC-D")  # a budget of its own
    add_category(d.db, "Rent")
    d.db.set_meta("onboarded", "1")
    assert d.open_from(folder)["state"] == "saved"
    assert categories(d.db) == ["Groceries"]
    assert [categories_in(p) for p in not_kept(folder)] == [["Rent"]]

    with pytest.raises(UserError, match="no budget in this folder"):
        computer(tmp_path, "PC-E").open_from(tmp_path / "Empty")


# ── Windows ──

def test_budget_inab_held_open_by_another_program_is_waited_for(tmp_path):
    """On Windows a file a sync tool or an antivirus has open can't be replaced for a moment."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A", sleep=time.sleep)
    a.move_to(folder)
    add_category(a.db, "Groceries")
    held = open(folder / FILE_NAME, "rb")
    letting_go = threading.Timer(0.6, held.close)
    letting_go.start()
    try:
        assert a.check(wait=False)["state"] == "saved"
    finally:
        letting_go.join()
        held.close()
    assert categories_in(folder / FILE_NAME) == ["Groceries"]
    assert names(folder) == [".Budget open on PC-A.json", FILE_NAME]


def test_a_file_held_too_long_is_saved_at_the_next_look(tmp_path, monkeypatch):
    folder = tmp_path / "Budget"
    waited = []
    a = computer(tmp_path, "PC-A", sleep=waited.append)
    a.move_to(folder)
    add_category(a.db, "Groceries")
    replace, busy = os.replace, {"tries": budget_folder.REPLACE_TRIES + 2}

    def held(source, target):
        if Path(target).name == FILE_NAME and busy["tries"]:
            busy["tries"] -= 1
            raise PermissionError(13, "The process cannot access the file")
        return replace(source, target)

    monkeypatch.setattr(budget_folder.os, "replace", held)
    state = a.check(wait=False)
    assert state["state"] == "waiting" and state["unsaved"]
    assert len(waited) == budget_folder.REPLACE_TRIES - 1
    assert names(folder) == [".Budget open on PC-A.json", FILE_NAME] and categories_in(folder / FILE_NAME) == []
    assert a.check(wait=False)["state"] == "saved"  # two more tries, then it goes
    assert categories_in(folder / FILE_NAME) == ["Groceries"]


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_an_imported_file_is_found_from_the_other_computer(tmp_path, separator):
    """The path an import kept is the computer's that imported it, maybe another system's."""
    seen_on_a, seen_on_b = tmp_path / "PC-A" / "OneDrive" / "Budget", tmp_path / "PC-B" / "Dropbox" / "Budget"
    a = service(tmp_path, "PC-A")
    call(a, "move_budget_folder", str(seen_on_a))
    statement = write(a.test_downloads, "statement.csv", bank_csv([("10/05/2026", "SAMPLE CAFE CALGARY AB",
                                                                     "SAMPLE CAFE CALGARY AB", "-4.50")]))
    call(a, "import_files", [str(statement)])
    a.budget_folder_at_quit()
    [kept] = list((seen_on_a / "Imported Files").rglob("*statement.csv"))
    shutil.copytree(seen_on_a, seen_on_b)  # the sync tool takes the folder to PC-B, another path there
    b = service(tmp_path, "PC-B")
    call(b, "open_budget_folder", str(seen_on_b))
    with b.db.read() as c:
        imp = dict(c.execute("SELECT * FROM imports").fetchone())
    found = seen_on_b / "Imported Files" / kept.parent.name / kept.name
    assert b._kept_file(imp) == found
    elsewhere = separator.join(["", "Users", "someone", "Budget", "Imported Files", kept.parent.name, kept.name])
    assert b._kept_file({**imp, "archived_path": elsewhere}) == found


# ── the service, for the window ──

def service(tmp_path, name):
    data = tmp_path / name
    downloads = data / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    settings = Settings(data / "settings.json")
    settings.update({"downloads_dir": str(downloads)})
    app = BudgetService(Database(data / "budget.db"), settings, data, computer=name)
    app.test_downloads = downloads
    return app


def test_the_window_chooses_moves_and_opens_the_budget_folder(tmp_path):
    folder = tmp_path / "Budget"
    a = service(tmp_path, "PC-A")
    assert call(a, "budget_folder")["state"] == "none"
    assert call(a, "look_in_budget_folder", str(folder)) == {"budget": None, "moved": None}
    assert a.dispatch("move_budget_folder", ["Budget"]) == {"ok": False, "error": "That isn't a folder: choose one."}
    assert call(a, "move_budget_folder", str(folder))["state"] == "saved"
    assert a.settings.get()["budget_folder"] == str(folder)
    call(a, "save_settings", {"budget_folder": str(tmp_path / "Elsewhere")})
    assert a.settings.get()["budget_folder"] == str(folder)  # only moving or opening changes it
    assert a.dispatch("keep_this_budget") == {"ok": False, "error": "There's only one budget now: nothing to choose."}

    b = service(tmp_path, "PC-B")
    assert call(b, "look_in_budget_folder", str(folder))["budget"]["computer"] == "PC-A"
    assert call(b, "open_budget_folder", str(folder))["state"] == "saved"
    call(a, "add_group", "Everyday")
    a.budget_folder_at_quit()
    assert b.watch_budget_folder()["took"]["computer"] == "PC-A"
    assert [g["name"] for g in call(b, "categories")] == ["Everyday"]

    relaunched = BudgetService(a.db, a.settings, a.data_dir, computer="PC-A")
    assert relaunched.folder.folder == folder
    assert relaunched.budget_folder_at_launch()["state"] == "saved"


# ── the working copy ──

def test_replacing_the_working_copy_keeps_the_database_and_clears_undo(tmp_path):
    mine = Database(tmp_path / "mine" / "budget.db")
    add_category(mine, "Rent")
    theirs = Database(tmp_path / "theirs" / "budget.db")
    add_category(theirs, "Groceries")
    theirs.conn.execute("VACUUM INTO ?", (str(tmp_path / "save.inab"),))
    mine.replace_with(tmp_path / "save.inab")
    assert categories(mine) == ["Groceries"] and mine.undo_label() is None
    assert mine.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    add_category(mine, "Gifts")  # undo still works on what it is now
    assert mine.undo() == "Add category" and categories(mine) == ["Groceries"]
