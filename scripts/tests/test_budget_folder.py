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
from budget_folder import FILE_NAME, BudgetFolder, NotABudget
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
    return BudgetFolder(Database(data / "budget.db"), data, folder, computer_name=name, app_version="1.5.0",
                        machine_id=f"machine-{name}",
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


def saved_elsewhere(folder, computer_name, app_version="1.5.0", schema_version=SCHEMA_VERSION, category=None,
                    same_digest=None):
    """Another computer's save over Budget.inab, built on the one there, as that computer would make it."""
    target = folder / FILE_NAME
    scratch = target.with_name(".elsewhere.partial")
    shutil.copyfile(target, scratch)
    with closing(sqlite3.connect(scratch, isolation_level=None)) as c:
        if category:
            group = c.execute("INSERT INTO category_groups (name) VALUES ('Elsewhere')").lastrowid
            c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, category))
        before = json.loads(c.execute("SELECT value FROM meta WHERE key = 'save'").fetchone()[0])
        counts = dict(before["counts"])
        counts[f"id-{computer_name}"] = counts.get(f"id-{computer_name}", 0) + 1
        c.execute("UPDATE meta SET value = ? WHERE key = 'save'", (json.dumps({
            "id": uuid.uuid4().hex, "counts": counts, "computer": computer_name, "saved_at": NOW.isoformat(),
            "app_version": app_version, "schema_version": schema_version, "digest": same_digest}),))
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
    assert names(folder) == [a._note_name(), FILE_NAME]  # no -wal, -shm or half-made copy
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
    assert first["counts"] == {a.computer_id(): 1}
    assert (a.db.path.parent / "Saves" / f"{first['id']}.inab").is_file()  # the save it started from

    add_category(a.db, "Rent")
    a.check(wait=False)
    assert names(folder) == [a._note_name(), FILE_NAME]
    assert categories_in(saved) == ["Groceries", "Rent"]
    second = save_in(saved)
    assert second["counts"] == {a.computer_id(): 2}


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
    assert categories_in(files["started_from"]) == [] and files["can_show_changes"]
    assert files["this"] is b.db and categories(files["this"]) == ["Rent"]  # the working copy, as it is
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
    assert categories(files["this"]) == ["Groceries"] and categories_in(files["other"]) == ["Rent"]
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
    assert not_kept(folder) == []  # Budget.inab has its very contents: nothing to keep a copy of
    add_category(a.db, "Rent")
    a.check(wait=False)
    shutil.copyfile(sorted((a.db.path.parent / "Saves").glob("*.inab"), key=lambda p: p.stat().st_mtime)[0],
                    folder / "Budget 3.inab")  # an older one of this computer's, which it came after
    assert a.check()["state"] == "saved" and "Budget 3.inab" not in names(folder)
    assert [categories_in(p) for p in not_kept(folder)] == [["Groceries"]]  # other contents: kept, not deleted


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
    relaunched = BudgetFolder(b.db, b.data_dir, folder, computer_name="PC-B", now=lambda: NOW)
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
    relaunched = BudgetFolder(a.db, a.data_dir, folder, computer_name="PC-A", now=lambda: NOW)
    assert relaunched.at_launch()["state"] == "unreachable" and not folder.exists()

    away.rename(folder)
    assert relaunched.check()["state"] == "saved"
    assert categories_in(folder / FILE_NAME) == ["Groceries"]
    assert made.name in names(folder / "Backups") and not (a.data_dir / "Backups").exists()
    assert names(folder / "Imported Files" / "2026-11") == ["2026-11-26 101500 statement.ofx"]
    assert not (a.data_dir / "Imported Files").exists()
    assert a._note_name() in names(folder)


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
    assert names(old) == [".Budget moved.json", b._note_name()]
    assert names(new) == [a._note_name(), FILE_NAME, "Imported Files"]
    state = b.check(wait=False)
    assert state["state"] == "moved" and state["moved"]["computer"] == "PC-A"
    add_category(b.db, "Rent")
    assert b.check(wait=False)["state"] == "moved" and FILE_NAME not in names(old)  # not put back
    assert b.open_from(new)["state"] == "saved"  # PC-A's move holds what PC-B started from: PC-B's change goes on top
    assert categories_in(new / FILE_NAME) == ["Rent"]


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
    assert names(folder) == [a._note_name(), FILE_NAME]


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
    assert names(folder) == [a._note_name(), FILE_NAME] and categories_in(folder / FILE_NAME) == []
    assert a.check(wait=False)["state"] == "saved"  # two more tries, then it goes
    assert categories_in(folder / FILE_NAME) == ["Groceries"]


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_an_imported_file_is_found_from_the_other_computer(tmp_path, separator):
    """The path an import kept is the computer's that imported it, maybe another system's."""
    seen_on_a, seen_on_b = tmp_path / "A sees" / "OneDrive" / "Budget", tmp_path / "B sees" / "Dropbox" / "Budget"
    a = service(tmp_path, "PC-A")
    call(a, "move_budget_folder", str(seen_on_a))
    statement = write(a.test_downloads, "statement.csv", bank_csv([("10/05/2026", "SAMPLE CAFE CALGARY AB",
                                                                     "SAMPLE CAFE CALGARY AB", "-4.50")]))
    call(a, "import_files", [str(statement)])
    a.budget_folder_at_quit()
    [kept] = list((seen_on_a / "Imported Files").rglob("*statement.csv"))
    a.db.close()
    shutil.copytree(seen_on_a, seen_on_b)  # the sync tool takes the folder to PC-B, another path there,
    shutil.rmtree(seen_on_a.parent)        # and PC-A's path isn't there on PC-B
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
    app = BudgetService(Database(data / "budget.db"), settings, data, computer_name=name)
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

    relaunched = BudgetService(a.db, a.settings, a.data_dir, computer_name="PC-A")
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


# ── the review of #196 ──

def test_a_long_history_is_still_one_line(tmp_path):
    """However many saves: a save built on another is always known to be (counts, not a list cut short)."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    for i in range(60):
        add_category(a.db, f"Sample {i}")
        a.check(wait=False)
    state = b.check()
    assert state["took"]["computer"] == "PC-A" and len(categories(b.db)) == 60
    add_category(b.db, "Rent")
    b.check(wait=False)
    add_category(a.db, "Gifts")
    assert a.check(wait=False)["state"] == "two_computers"
    assert a.keep_this()["state"] == "saved"
    state = b.check()  # PC-B changed nothing since its save: the one kept counts it
    assert state["took"]["computer"] == "PC-A" and "Gifts" in categories(b.db) and "Rent" not in categories(b.db)


def test_a_copy_that_went_from_saves_is_read_again_never_taken_empty(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    add_category(a.db, "Groceries")
    a.check(wait=False)
    [(_, read)] = b._read_folder(folder)[0] and [(None, b._seen[str(folder / FILE_NAME)][1])]
    read.path.unlink()  # its copy in Saves/ is gone (cleaned by hand)
    state = b.check()
    assert state["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries"]

    add_category(a.db, "Rent")
    a.check(wait=False)
    b._read_folder(folder)
    copy = b._seen[str(folder / FILE_NAME)][1].path
    copy.write_bytes(b"half a file")  # damaged after it was read
    assert b.check()["state"] == "waiting" and categories(b.db) == ["Groceries"]  # nothing taken
    assert b.check()["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries", "Rent"]


def test_replacing_with_a_file_that_isnt_there_changes_nothing(tmp_path):
    db = Database(tmp_path / "budget.db")
    add_category(db, "Rent")
    with pytest.raises(sqlite3.OperationalError):
        db.replace_with(tmp_path / "gone.inab")
    assert categories(db) == ["Rent"] and not (tmp_path / "gone.inab").exists()


def test_a_crash_while_saving_is_saved_again_never_asked(tmp_path, monkeypatch):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    add_category(a.db, "Groceries")
    recording = a._set_mine

    def power_cut(save):
        raise KeyboardInterrupt("power cut")

    a._set_mine = power_cut  # before what's saved is recorded
    with pytest.raises(KeyboardInterrupt):
        a.check(wait=False)
    a._set_mine = recording
    a._seen.clear()
    assert a.check(wait=False)["state"] == "saved" and categories_in(folder / FILE_NAME) == ["Groceries"]

    add_category(a.db, "Rent")
    copy = budget_folder._copy

    def cut_off(source, target):  # after it's recorded, before it's in the folder
        if Path(target).parent == folder:
            raise KeyboardInterrupt("power cut")
        return copy(source, target)

    monkeypatch.setattr(budget_folder, "_copy", cut_off)
    with pytest.raises(KeyboardInterrupt):
        a.check(wait=False)
    monkeypatch.setattr(budget_folder, "_copy", copy)
    a._seen.clear()
    state = a.check(wait=False)
    assert state["state"] == "saved" and categories_in(folder / FILE_NAME) == ["Groceries", "Rent"]
    assert names(folder) == [a._note_name(), FILE_NAME]


def test_a_copy_in_saves_held_by_another_program_is_waited_for(tmp_path, monkeypatch):
    folder = tmp_path / "Budget"
    waited = []
    a = computer(tmp_path, "PC-A", sleep=waited.append)
    a.move_to(folder)
    add_category(a.db, "Groceries")
    replace, busy = os.replace, {"tries": 3}

    def held(source, target):
        if Path(target).parent.name == "Saves" and busy["tries"]:
            busy["tries"] -= 1
            raise PermissionError(13, "The process cannot access the file")
        return replace(source, target)

    monkeypatch.setattr(budget_folder.os, "replace", held)
    assert a.check(wait=False)["state"] == "saved" and waited == [budget_folder.REPLACE_WAIT] * 3
    monkeypatch.setattr(budget_folder.os, "replace", replace)
    assert a.check(wait=False)["state"] == "saved"  # its own save, not another computer's


def test_a_save_with_this_computers_contents_is_the_same_save(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    saved_elsewhere(folder, "PC-B", same_digest=save_in(folder / FILE_NAME)["digest"])
    state = a.check(wait=False)
    assert state["state"] == "saved" and "took" not in state


def test_backups_keep_nothing_about_saves(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    made = backups.make(a.db)
    with closing(sqlite3.connect(f"{made.as_uri()}?mode=ro", uri=True)) as c:
        assert c.execute("SELECT COUNT(*) FROM meta WHERE key = 'save'").fetchone()[0] == 0


def test_restoring_a_backup_into_the_folder_is_taken_by_every_computer(tmp_path):
    """The 1.5 way: quit the app on every computer, copy the backup in as Budget.inab."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    add_category(a.db, "Groceries")
    a.check(wait=False)
    b.check()
    made = backups.make(a.db)
    add_category(a.db, "Rent")
    a.check(wait=False)
    b.check()
    shutil.copyfile(made, folder / FILE_NAME)
    assert a.check()["took"]["computer"] is None and categories(a.db) == ["Groceries"]
    assert save_in(folder / FILE_NAME)["computer"] == "PC-A"  # saved back at once, counting the restore
    assert b.check()["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries"]
    assert a.check()["state"] == b.check()["state"] == "saved"
    add_category(a.db, "Gifts")
    a.check(wait=False)
    assert b.check()["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries", "Gifts"]
    shutil.copyfile(made, folder / FILE_NAME)  # the same backup again: the same save, older than the line now
    assert a.check()["state"] == "older" and b.check()["state"] == "older"
    assert a.keep_other()["state"] == "saved" and categories(a.db) == ["Groceries"]  # Use it
    assert b.check()["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries"]


def test_restoring_a_backup_asks_a_computer_with_changes_not_saved_yet(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    made = backups.make(a.db)
    add_category(a.db, "Bills")
    a.check(wait=False)
    add_category(a.db, "Rent")
    shutil.copyfile(made, folder / FILE_NAME)
    state = a.check(wait=False)
    assert state["state"] == "two_computers" and state["other"]["computer"] is None
    files = a.two_computers()
    assert categories_in(files["started_from"]) == ["Groceries", "Bills"] and categories_in(files["other"]) == ["Groceries"]
    a.keep_this()
    assert categories_in(folder / FILE_NAME) == ["Groceries", "Bills", "Rent"]


def test_a_backup_restored_as_the_working_copy_asks_instead_of_being_replaced(tmp_path):
    """The way before 1.5 (CLAUDE.md's): with the app closed, the backup copied in as budget.db."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")  # aside: quitting backs up again
    add_category(a.db, "Rent")
    a.at_quit()
    a.db.close()
    for side in ("budget.db-wal", "budget.db-shm"):
        (a.data_dir / side).unlink(missing_ok=True)
    shutil.copyfile(made, a.data_dir / "budget.db")
    saved = (folder / FILE_NAME).read_bytes()
    relaunched = BudgetFolder(Database(a.data_dir / "budget.db"), a.data_dir, folder, computer_name="PC-A", now=lambda: NOW)
    assert relaunched.at_launch()["state"] == "two_computers"
    assert categories(relaunched.db) == ["Groceries"] and (folder / FILE_NAME).read_bytes() == saved


def test_a_take_waits_for_a_request_under_way(tmp_path):
    """A request reads, then writes: another computer's save never lands in between."""
    folder = tmp_path / "Budget"
    a = service(tmp_path, "PC-A")
    call(a, "move_budget_folder", str(folder))
    b = service(tmp_path, "PC-B")
    call(b, "open_budget_folder", str(folder))
    call(a, "add_group", "Everyday")
    a.budget_folder_at_quit()
    found = {}
    with b.turns.shared():  # a request under way
        watcher = threading.Thread(target=lambda: found.update(b.watch_budget_folder()))
        watcher.start()
        watcher.join(0.5)
        assert watcher.is_alive()  # the take waits
        call(b, "add_group", "Bills")  # the request's write
    watcher.join(60)  # however slow the machine (an antivirus scanning each new file)
    assert not watcher.is_alive() and found["state"] == "two_computers"  # not taken over PC-B's change: asked
    assert [g["name"] for g in call(b, "categories")] == ["Bills"]


def test_a_take_from_inside_a_request_is_refused_not_stuck():
    turns = budget_folder.Turns()
    with turns.shared(), pytest.raises(RuntimeError):
        with turns.alone():
            pass
    with turns.alone(), turns.alone(), turns.shared():  # one thread: reentrant
        pass


def test_the_save_this_computer_started_from_is_never_pruned(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    add_category(b.db, "Rent")
    for i in range(15):
        add_category(a.db, f"Sample {i}")
        a.check(wait=False)
        assert b.check()["state"] == "two_computers"
    files = b.two_computers()
    assert files["can_show_changes"] and categories_in(files["started_from"]) == []
    assert len(names(b.db.path.parent / "Saves")) <= budget_folder.KEPT_SAVES + 3


def test_when_the_save_both_started_from_is_gone_the_question_is_still_asked(tmp_path):
    folder, seen_on_b = tmp_path / "Budget", tmp_path / "PC-B sees" / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    shutil.copytree(folder, seen_on_b)
    b = computer(tmp_path, "PC-B")
    b.open_from(seen_on_b)
    add_category(b.db, "Rent")
    b.check(wait=False)
    for i in range(budget_folder.KEPT_SAVES + 2):  # PC-A saves on, apart: the save both started from is pruned
        add_category(a.db, f"Sample {i}")
        a.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget 2.inab")
    assert a.check()["state"] == "two_computers"
    files = a.two_computers()
    assert files["started_from"] is None and not files["can_show_changes"]
    assert a.keep_this()["state"] == "saved"
    shutil.copyfile(folder / FILE_NAME, seen_on_b / FILE_NAME)
    assert b.check()["took"]["computer"] == "PC-A"


def test_keeping_this_one_looks_again_first(tmp_path):
    folder, a, b = _changed_on_both(tmp_path)
    b.check(wait=False)
    add_category(a.db, "Gifts")
    a.check(wait=False)  # PC-A saved again after PC-B was asked
    state = b.keep_this()  # what PC-B's person never saw is kept beside it and asked about next (#196's sixth review)
    assert state["state"] == "two_computers" and categories_in(b.two_computers()["other"]) == ["Groceries", "Gifts"]
    assert b.keep_this()["state"] == "saved"
    assert sorted(sorted(categories_in(p)) for p in not_kept(folder)) == [["Gifts", "Groceries"], ["Groceries"]]
    assert a.check()["took"]["computer"] == "PC-B" and categories(a.db) == ["Rent"]

    folder, a, b = _changed_on_both(tmp_path / "newer")
    b.check(wait=False)
    saved_elsewhere(folder, "PC-C", app_version="9.0.0", schema_version=SCHEMA_VERSION + 1)
    saved = (folder / FILE_NAME).read_bytes()
    with pytest.raises(UserError, match="9.0.0"):
        b.keep_this()
    assert (folder / FILE_NAME).read_bytes() == saved


def test_moving_from_a_folder_that_cant_be_reached_asks_first(tmp_path):
    old, new = tmp_path / "Drive" / "Budget", tmp_path / "New" / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(old)
    old.rename(tmp_path / "Unplugged")
    with pytest.raises(UserError, match="can't be reached"):
        a.move_to(new)
    assert not new.exists()
    assert a.move_to(new, old_folder_gone=True)["state"] == "saved"
    assert categories_in(new / FILE_NAME) == ["Groceries"]


def test_a_budget_icloud_hasnt_downloaded_yet_is_still_a_budget(tmp_path):
    folder = tmp_path / "iCloud" / "Budget"
    folder.mkdir(parents=True)
    (folder / ".Budget.inab.icloud").write_bytes(b"placeholder")
    a = computer(tmp_path, "PC-A")
    assert a.look_in(folder)["budget"] == {"computer": None, "saved_at": None, "app_version": None}
    with pytest.raises(UserError, match="already has a budget"):
        a.move_to(folder)
    with pytest.raises(UserError, match="iCloud"):
        a.open_from(folder)
    assert names(folder) == [".Budget.inab.icloud"]


def test_two_computers_backing_up_at_once_keep_both(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    when = datetime(2026, 11, 27, 9, 14, 5)
    made = {backups.make(a.db, when).name, backups.make(b.db, when).name}
    assert made == {f"budget-2026-11-27-09-14-05 on {a._tag()}.db", f"budget-2026-11-27-09-14-05 on {b._tag()}.db"}
    assert made <= set(names(folder / "Backups"))


def test_pruning_leaves_another_computers_backup_being_made(tmp_path):
    folder = tmp_path / "Backups"
    folder.mkdir()
    theirs = folder / ".budget-2026-11-27-09-14-05 on PC-B.db.partial"
    mine = folder / ".budget-2026-11-27-09-14-05 on PC-A.db.partial"
    stale = folder / ".budget-2026-11-20-09-14-05 on PC-C.db.partial"
    for path in (theirs, mine, stale):
        path.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(stale, (two_days_ago, two_days_ago))
    backups.prune(folder, datetime(2026, 11, 27).date(), tag="PC-A")
    assert names(folder) == [theirs.name]


# ── the second review of #196 ──

def _sync(source, target):
    """The sync tool brings one computer's view of the folder to another's: the budgets in it."""
    for name in names(target):
        if name.endswith(".inab") and not (source / name).exists():
            (target / name).unlink()
    for name in names(source):
        if name.endswith(".inab"):
            shutil.copyfile(source / name, target / name)


def test_keeping_the_other_one_is_counted_in_the_folder(tmp_path):
    """PC-A's save became the sync tool's copy, PC-B's Budget.inab; PC-A keeps PC-B's. PC-B's later
    saves are then built on what PC-A has: taken, never asked again."""
    folder, seen_on_a = tmp_path / "Budget", tmp_path / "PC-A sees" / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    shutil.copytree(folder, seen_on_a)
    a.open_from(seen_on_a)
    add_category(a.db, "Groceries")
    a.check(wait=False)
    add_category(b.db, "Rent")
    b.check(wait=False)
    shutil.copyfile(seen_on_a / FILE_NAME, folder / "Budget-PC-A.inab")
    shutil.copyfile(folder / FILE_NAME, seen_on_a / FILE_NAME)
    shutil.copyfile(folder / "Budget-PC-A.inab", seen_on_a / "Budget-PC-A.inab")
    assert a.check(wait=False)["state"] == "two_computers"
    assert a.keep_other()["state"] == "saved" and categories(a.db) == ["Rent"]
    _sync(seen_on_a, folder)
    for i in range(3):
        b.check()
        add_category(b.db, f"Sample {i}")
        b.check(wait=False)
        _sync(folder, seen_on_a)
        state = a.check(wait=False)
        assert state["state"] == "saved" and state["took"]["computer"] == "PC-B"


def test_a_restore_is_taken_whatever_each_computer_had_seen(tmp_path):
    """PC-B saved last and PC-A never saw it; then a backup is restored. Both take it, and what
    either saves after it, the other takes."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(b.db, "Rent")
    b.check(wait=False)
    shutil.copyfile(made, folder / FILE_NAME)
    state = a.check(wait=False)  # PC-A's budget has the backup's very contents: the same save, nothing to take
    assert state["state"] == "saved" and "took" not in state
    assert b.check(wait=False)["took"] and categories(a.db) == categories(b.db) == ["Groceries"]
    add_category(a.db, "Gifts")
    a.check(wait=False)
    state = b.check(wait=False)
    assert state["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries", "Gifts"]
    add_category(b.db, "Bills")
    b.check(wait=False)
    assert a.check(wait=False)["took"]["computer"] == "PC-B"


def test_a_data_folder_copied_to_another_machine_counts_as_another_computer(tmp_path):
    """Migration Assistant: the same computer.json on a new machine. Its id there is new, so its
    change is asked about, never put away as an older save of the first one's."""
    folder, seen_on_b = tmp_path / "Budget", tmp_path / "PC-B sees" / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    a.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copytree(tmp_path / "PC-A", tmp_path / "PC-B")
    b = computer(tmp_path, "PC-B", folder=folder)
    assert b.computer_id() != a.computer_id()
    shutil.copytree(folder, seen_on_b)
    b.open_from(seen_on_b)
    for name in ("Sample 1", "Sample 2"):
        add_category(a.db, name)
        a.check(wait=False)
    add_category(b.db, "Rent")
    b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")
    state = a.check(wait=False)
    assert state["state"] == "two_computers" and state["other"]["computer"] == "PC-B"


def test_a_budget_started_again_is_never_taken_for_the_old_one(tmp_path):
    """PC-A starts its budget again (its data folder's computer.json kept) and moves the new one to
    another folder. PC-B opening it gets the new budget, its own old one in that folder's Backups/."""
    old_folder, new_folder = tmp_path / "Budget", tmp_path / "New Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(old_folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(old_folder)
    for i in range(3):
        add_category(b.db, f"Sample {i}")
        b.check(wait=False)
        a.check()
    a.at_quit()
    a.db.close()
    for side in ("", "-wal", "-shm"):
        (tmp_path / "PC-A" / f"budget.db{side}").unlink(missing_ok=True)
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Rent")
    a.move_to(new_folder)
    assert b.open_from(new_folder)["state"] == "saved"
    assert categories(b.db) == ["Rent"] and categories_in(new_folder / FILE_NAME) == ["Rent"]
    [kept] = not_kept(new_folder)
    assert categories_in(kept) == ["Groceries", "Sample 0", "Sample 1", "Sample 2"]
    assert a.check()["state"] == "saved" and categories(a.db) == ["Rent"]


def test_a_moved_budget_is_never_put_back(tmp_path):
    """The sync tool may bring the old folder's Budget.inab going before the note that it moved."""
    old_folder, new_folder, seen_on_b = tmp_path / "Old", tmp_path / "New", tmp_path / "PC-B sees" / "Old"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(old_folder)
    shutil.copytree(old_folder, seen_on_b)
    b = computer(tmp_path, "PC-B")
    b.open_from(seen_on_b)
    a.move_to(new_folder)
    (seen_on_b / FILE_NAME).unlink()  # the file's going arrives first
    assert b.check(wait=False)["state"] == "missing" and FILE_NAME not in names(seen_on_b)
    shutil.copyfile(old_folder / budget_folder.MOVED_NOTE, seen_on_b / budget_folder.MOVED_NOTE)
    assert b.check(wait=False)["state"] == "moved"
    assert b.save_again()["state"] == "moved" and FILE_NAME not in names(seen_on_b)  # not even when asked


def test_a_budget_file_gone_from_the_folder_is_saved_again_only_when_asked(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    (folder / FILE_NAME).unlink()
    add_category(a.db, "Rent")
    assert a.check(wait=False)["state"] == "missing" and FILE_NAME not in names(folder)
    assert a.check(wait=False)["state"] == "missing"
    assert a.save_again()["state"] == "saved" and categories_in(folder / FILE_NAME) == ["Groceries", "Rent"]


def _older_put_back(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    earlier = shutil.copyfile(folder / FILE_NAME, tmp_path / "earlier version.inab")
    add_category(a.db, "Rent")
    a.check(wait=False)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    shutil.copyfile(earlier, folder / FILE_NAME)  # the sync tool's "restore previous version"
    return folder, a, b


def test_an_older_version_put_back_is_asked_about_never_saved_over(tmp_path):
    folder, a, b = _older_put_back(tmp_path)
    saved = (folder / FILE_NAME).read_bytes()
    state = a.check(wait=False)
    assert state["state"] == "older" and state["other"]["computer"] == "PC-A"
    assert b.check(wait=False)["state"] == "older"  # one in its own line, though PC-B never had it
    add_category(a.db, "Gifts")
    assert a.check(wait=False)["state"] == "older" and (folder / FILE_NAME).read_bytes() == saved
    assert categories(a.db) == ["Groceries", "Rent", "Gifts"]  # nor gone back to
    files = a.two_computers()
    assert categories_in(files["started_from"]) == ["Groceries"] and files["can_show_changes"]


def test_using_the_older_version(tmp_path):
    folder, a, b = _older_put_back(tmp_path)
    a.check(wait=False)
    assert a.keep_other()["state"] == "saved" and categories(a.db) == ["Groceries"]
    assert [categories_in(p) for p in not_kept(folder)] == [["Groceries", "Rent"]]
    state = b.check(wait=False)  # PC-B takes it, never asked
    assert state["took"]["computer"] == "PC-A" and categories(b.db) == ["Groceries"]


def test_keeping_the_newer_one(tmp_path):
    folder, a, b = _older_put_back(tmp_path)
    a.check(wait=False)
    assert a.keep_this()["state"] == "saved" and categories_in(folder / FILE_NAME) == ["Groceries", "Rent"]
    assert [categories_in(p) for p in not_kept(folder)] == [["Groceries"]]
    state = b.check(wait=False)  # PC-B has the newer one already: the same save, nothing to take
    assert state["state"] == "saved" and categories(b.db) == ["Groceries", "Rent"]
    assert b._mine().id == save_in(folder / FILE_NAME)["id"]


def test_a_journal_left_beside_a_half_made_copy_goes(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    a.move_to(folder)
    saves = a.db.path.parent / "Saves"
    for left in (".saving-1.partial", ".saving-1.partial-journal", ".reading-2.partial-wal"):
        (saves / left).write_bytes(b"x")
    add_category(a.db, "Groceries")
    a.check(wait=False)
    assert not [n for n in names(saves) if ".partial" in n]


def test_a_save_waits_for_a_request_under_way(tmp_path):
    folder = tmp_path / "Budget"
    a = service(tmp_path, "PC-A")
    call(a, "move_budget_folder", str(folder))
    call(a, "add_group", "Everyday")
    found = {}
    with a.turns.shared():  # a request between two of its writes
        saving = threading.Thread(target=lambda: found.update(a.folder.check(wait=False)))
        saving.start()
        saving.join(0.5)
        assert saving.is_alive()
        call(a, "add_group", "Bills")
    saving.join(60)
    assert not saving.is_alive() and found["state"] == "saved"
    assert [r[0] for r in sqlite3.connect(folder / FILE_NAME).execute("SELECT name FROM category_groups")] == \
        ["Everyday", "Bills"]


def test_pruning_tells_a_computer_from_one_with_a_number_in_its_name(tmp_path):
    folder = tmp_path / "Backups"
    folder.mkdir()
    second = backups.not_kept_name(datetime(2026, 11, 27, 9, 14, 5), 2, tag="PC")
    assert second == "budget-not-kept-2026-11-27-09-14-05 (2) on PC.db" and backups.made_at(second)
    ours, theirs = folder / f".{second}.partial", folder / ".budget-2026-11-27-09-14-05 on PC (2).db.partial"
    for path in (ours, theirs):
        path.write_bytes(b"x")
    backups.prune(folder, datetime(2026, 11, 27).date(), tag="PC")
    assert names(folder) == [theirs.name]


def test_moving_into_a_folder_a_budget_moved_out_of(tmp_path):
    old_folder, new_folder = tmp_path / "Old", tmp_path / "New"
    a = computer(tmp_path, "PC-A")
    a.move_to(old_folder)
    a.move_to(new_folder)
    assert a.look_in(old_folder)["moved"]["computer"] == "PC-A"
    assert a.move_to(old_folder)["state"] == "saved"
    assert budget_folder.MOVED_NOTE not in names(old_folder) and a.look_in(old_folder)["moved"] is None


# ── the third review of #196 ──

def _restored_once(tmp_path, name="PC-A", folder_name="Budget"):
    """A computer whose budget has a restore in its line."""
    folder = tmp_path / folder_name
    a = computer(tmp_path, name)
    add_category(a.db, "Groceries")
    a.move_to(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / f"{name} backup.db")
    add_category(a.db, "Bills")
    a.check(wait=False)
    shutil.copyfile(made, folder / FILE_NAME)
    assert a.check(wait=False)["took"]
    add_category(a.db, "Rent")
    a.check(wait=False)
    return folder, a


def test_opening_another_budget_after_a_restore_takes_it(tmp_path):
    """A line with a restore in it reads as later than one without: that never makes another
    budget an older one of this computer's."""
    _, a = _restored_once(tmp_path)
    other = tmp_path / "Other"
    c = computer(tmp_path, "PC-C")
    add_category(c.db, "Gifts")
    c.move_to(other)
    saved = (other / FILE_NAME).read_bytes()
    state = a.open_from(other)
    assert state["state"] == "saved" and categories(a.db) == ["Gifts"]
    assert [categories_in(p) for p in not_kept(other)] == [["Groceries", "Rent"]]  # this computer's, kept
    state = c.check(wait=False)
    assert state["state"] == "saved" and "took" not in state and (other / FILE_NAME).read_bytes() == saved


def test_another_budget_opened_has_its_saves_taken(tmp_path):
    folder, other = tmp_path / "Budget", tmp_path / "Other"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    for i in range(2):
        add_category(b.db, f"Sample {i}")
        b.check(wait=False)
        a.check(wait=False)
    c = computer(tmp_path, "PC-C")
    add_category(c.db, "Gifts")
    c.move_to(other)
    a.open_from(other)
    for name in ("Bills", "Rent"):
        add_category(c.db, name)
        c.check(wait=False)
        state = a.check(wait=False)
        assert state["state"] == "saved" and state["took"]["computer"] == "PC-C"


def _held(monkeypatch, folder):
    replace = budget_folder._replace

    def held(source, target, *args, **kwargs):
        if Path(target) == folder / FILE_NAME:
            raise PermissionError(13, "The process cannot access the file")
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(budget_folder, "_replace", held)
    return lambda: monkeypatch.setattr(budget_folder, "_replace", replace)


def test_keeping_this_one_stays_kept_while_its_save_waits(tmp_path, monkeypatch):
    folder, a, b = _changed_on_both(tmp_path)
    b.check(wait=False)
    let_go = _held(monkeypatch, folder)
    state = b.keep_this()
    assert state["state"] == "waiting" and state["unsaved"]
    assert b.check(wait=False)["state"] == "waiting"  # tried again, never "older"
    let_go()
    assert b.check(wait=False)["state"] == "saved" and categories_in(folder / FILE_NAME) == ["Rent"]
    assert a.check()["took"]["computer"] == "PC-B"


def test_keeping_the_other_one_from_a_copy_stays_kept_while_its_save_waits(tmp_path, monkeypatch):
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
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")
    assert a.check(wait=False)["state"] == "two_computers"
    let_go = _held(monkeypatch, folder)
    assert a.keep_other()["state"] == "waiting" and categories(a.db) == ["Rent"]
    assert a.check(wait=False)["state"] == "waiting"
    let_go()
    assert a.check(wait=False)["state"] == "saved" and categories_in(folder / FILE_NAME) == ["Rent"]


def test_the_same_backup_restored_twice_while_a_computer_missed_the_first(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    shutil.copyfile(made, folder / FILE_NAME)  # PC-B is off for the first restore
    assert a.check(wait=False)["took"]
    add_category(a.db, "Bills")
    a.check(wait=False)
    shutil.copyfile(made, folder / FILE_NAME)  # the same backup again: one identity, so it's older than the line now
    assert a.check(wait=False)["state"] == "older"
    assert a.keep_other()["state"] == "saved" and categories(a.db) == ["Groceries"]  # Use it
    state = b.check(wait=False)  # PC-B missed the first and has those very contents: the same save, counted
    assert state["state"] == "saved" and categories(b.db) == ["Groceries"]
    assert b._mine().id == save_in(folder / FILE_NAME)["id"]
    add_category(b.db, "Gifts")
    b.check(wait=False)
    state = a.check(wait=False)
    assert state["took"]["computer"] == "PC-B" and categories(a.db) == ["Groceries", "Gifts"]


def test_what_each_computer_changed_is_shown_from_what_two_computers_gives(tmp_path):
    """#184's changes.compare reads the three budgets as two_computers() hands them over."""
    import changes

    folder, a, b = _changed_on_both(tmp_path)
    b.check(wait=False)
    files = b.two_computers()
    shown = changes.compare(files["started_from"], files["this"], files["other"])
    assert shown["this"]["changed"] and shown["other"]["changed"]
    assert "Rent" in json.dumps(shown["this"]) and "Groceries" not in json.dumps(shown["this"])
    assert "Groceries" in json.dumps(shown["other"]) and "Rent" not in json.dumps(shown["other"])


# ── the fourth review of #196 ──

def _view(c, folder):
    """`c`'s sync tool falls behind: it works in a copy of the folder until it's synced again."""
    shutil.copytree(folder, folder.parent / f"{c.computer} sees" / folder.name, dirs_exist_ok=True)
    c.folder, c._reachable, c._seen = folder.parent / f"{c.computer} sees" / folder.name, None, {}
    return c.folder


def test_keeping_the_other_one_keeps_what_it_saved_since(tmp_path):
    """PC-A is asked; while it is, PC-B saves again. Keep the other one keeps PC-B's newest."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    seen_on_b = _view(b, folder)
    add_category(a.db, "Rent")
    a.check(wait=False)
    add_category(b.db, "Gifts")
    b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)  # PC-B's wins in the folder
    assert a.check(wait=False)["state"] == "two_computers"
    add_category(b.db, "Bills")
    b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)
    assert a.keep_other()["state"] == "saved"
    assert categories(a.db) == ["Groceries", "Gifts", "Bills"]
    assert [categories_in(p) for p in not_kept(folder)] == [["Groceries", "Rent"]]
    shutil.copyfile(folder / FILE_NAME, seen_on_b / FILE_NAME)
    b._seen = {}
    assert b.check(wait=False)["state"] == "saved" and categories(b.db) == ["Groceries", "Gifts", "Bills"]


def _restored_twice(tmp_path):
    """PC-B was off for one restore; then another restore that both take, PC-B's save landing last."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    first = shutil.copyfile(backups.make(a.db), tmp_path / "first.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    b.check(wait=False)
    second = shutil.copyfile(backups.make(a.db), tmp_path / "second.db")
    shutil.copyfile(first, folder / FILE_NAME)
    a.check(wait=False)
    add_category(a.db, "Gifts")
    a.check(wait=False)
    shutil.copyfile(second, folder / FILE_NAME)
    seen_on_b = _view(b, folder)
    a.check(wait=False)
    b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)
    return folder, seen_on_b, a, b


def test_a_change_made_after_a_restore_is_taken_whatever_each_computer_saw(tmp_path):
    folder, seen_on_b, a, b = _restored_twice(tmp_path)
    assert a.check(wait=False)["state"] == "saved" and categories(a.db) == ["Groceries", "Rent"]
    add_category(b.db, "Bills")
    b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)
    state = a.check(wait=False)
    assert state["took"]["computer"] == "PC-B" and categories(a.db) == ["Groceries", "Rent", "Bills"]


def test_a_change_after_a_restore_arriving_as_a_copy_is_taken_never_put_away(tmp_path):
    folder, seen_on_b, a, b = _restored_twice(tmp_path)
    a.check(wait=False)
    add_category(b.db, "Bills")
    b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")
    state = a.check(wait=False)
    assert state["took"]["computer"] == "PC-B" and "Bills" in categories(a.db)


def test_moving_is_refused_while_an_older_one_is_put_back(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    earlier = shutil.copyfile(folder / FILE_NAME, tmp_path / "earlier.inab")
    add_category(a.db, "Rent")
    a.check(wait=False)
    shutil.copyfile(earlier, folder / FILE_NAME)
    assert a.check(wait=False)["state"] == "older"
    with pytest.raises(UserError, match="older one was put back"):
        a.move_to(tmp_path / "Elsewhere")
    assert categories_in(folder / FILE_NAME) == ["Groceries"] and not (tmp_path / "Elsewhere" / FILE_NAME).exists()


def test_settings_learn_of_a_folder_before_anything_is_done_in_it(tmp_path, monkeypatch):
    """A crash part way through Open… relaunches on the folder being opened, not the old one."""
    old, other = tmp_path / "Budget", tmp_path / "Other"
    a = service(tmp_path, "PC-A")
    call(a, "move_budget_folder", str(old))
    c = computer(tmp_path, "PC-C")
    add_category(c.db, "Gifts")
    c.move_to(other)
    said = []
    take = BudgetFolder._take

    def taking(self, *args, **kwargs):
        said.append(a.settings.get()["budget_folder"])
        return take(self, *args, **kwargs)

    monkeypatch.setattr(BudgetFolder, "_take", taking)
    call(a, "open_budget_folder", str(other))
    assert said == [str(other)]

    def failing(self, *args, **kwargs):
        raise NotABudget("gone")

    monkeypatch.setattr(BudgetFolder, "_take", failing)
    assert not a.dispatch("open_budget_folder", [str(old)])["ok"]
    assert a.settings.get()["budget_folder"] == str(other)  # a step that failed puts it back



# ── the fifth review of #196: its reproductions, kept as they were written ──

def everything_kept(tmp_path):
    found = set()
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".inab", ".db") and p.name != "budget.db" and "Saves" not in p.parts \
                and p.name != "the backup.db":
            found |= set(categories_in(p))
    return found


def test_keep_this_over_a_restore_then_the_restore_arrives_as_a_conflict_copy(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    b.check(wait=False)
    shutil.copyfile(made, folder / FILE_NAME)          # the person restores a backup
    seen_on_b = _view(b, folder)                        # PC-B's sync tool falls behind (it has the restore)
    assert b.check(wait=False)["took"]                  # PC-B takes it, saves it back as itself (in its view)
    add_category(a.db, "Bills")                         # PC-A has a change not saved yet
    assert a.check(wait=False)["state"] == "two_computers"
    a.keep_this()                                       # the person keeps PC-A's: Groceries, Rent, Bills
    assert "Bills" in categories_in(folder / FILE_NAME)
    # PC-B's sync catches up: both changed, so its save becomes a conflict copy
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")
    b.folder, b._reachable, b._seen = folder, None, {}
    state = a.check(wait=False)
    print("A after the copy:", state["state"], state.get("took"), categories(a.db), names(folder))
    b.check(wait=False)
    print("B:", categories(b.db), "folder:", categories_in(folder / FILE_NAME))
    print("anywhere kept:", sorted(everything_kept(tmp_path)))
    assert "Bills" in categories(a.db), "PC-A's kept change was taken away without asking"


def delete_category(db, name):
    with db.write("Delete category") as c:
        c.execute("DELETE FROM categories WHERE name = ?", (name,))


def test_both_changed_after_a_restore_nobody_is_asked(tmp_path):
    """PC-A keeps its own over the restore; PC-B, which took the restore, adds Bills on top of it.
    Both changed: one of the two should be asked. Here each silently takes the other's."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    b.check(wait=False)
    shutil.copyfile(made, folder / FILE_NAME)
    seen_on_b = _view(b, folder)
    assert b.check(wait=False)["took"]
    add_category(b.db, "Gifts")                          # PC-B's change on top of the restore
    b.check(wait=False)
    add_category(a.db, "Bills")
    assert a.check(wait=False)["state"] == "two_computers"
    a.keep_this()                                        # PC-A's change, kept by choice
    # the sync tool keeps PC-B's as Budget.inab and makes PC-A's the copy
    shutil.copyfile(folder / FILE_NAME, folder / "Budget-PC-A.inab")
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)
    b.folder, b._reachable, b._seen = folder, None, {}
    sa = a.check(wait=False)
    sb = b.check(wait=False)
    print("A:", sa["state"], categories(a.db), "| B:", sb["state"], categories(b.db),
          "| folder:", categories_in(folder / FILE_NAME), names(folder / "Backups"))
    for _ in range(3):
        a.check(wait=False); b.check(wait=False)
    print("after more looks A:", a.state()["state"], categories(a.db), "| B:", b.state()["state"], categories(b.db))
    print("backups:", [categories_in(p) for p in sorted((folder / "Backups").glob("*.db"))])
    assert "two_computers" in (sa["state"], sb["state"]), "both changed and nobody was asked"


def test_a_change_back_to_contents_once_had_is_not_new(tmp_path):
    """PC-B deletes a category PC-A added while PC-A adds another: both changed. PC-B's budget is
    then exactly one PC-A once had, so PC-A treats it as nothing new."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    add_category(a.db, "Gifts")
    a.check(wait=False)
    assert b.check(wait=False)["took"]
    seen_on_b = _view(b, folder)
    delete_category(b.db, "Gifts")                       # PC-B's change: Gifts goes
    b.check(wait=False)
    add_category(a.db, "Rent")                           # PC-A's change: Rent
    a.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)  # PC-B's lands last (no copy kept)
    b.folder, b._reachable, b._seen = folder, None, {}
    sa = a.check(wait=False)
    sb = b.check(wait=False)
    print("A:", sa["state"], categories(a.db), "| B:", sb["state"], categories(b.db),
          "| folder:", categories_in(folder / FILE_NAME),
          "| backups:", [categories_in(p) for p in sorted((folder / "Backups").glob("*.db"))])
    assert "two_computers" in (sa["state"], sb["state"]), "PC-B's deletion was undone without asking"


def test_two_restores_on_two_computers_order_by_clock(tmp_path):
    """Two different backups restored, one on each computer while PC-B's sync is behind; each then
    adds something. The lines know nothing of each other, so the order is the computers' clocks."""
    base = datetime(2026, 11, 27, 9, 0, tzinfo=timezone(timedelta(hours=-7)))
    clocks = {"PC-A": base, "PC-B": base + timedelta(minutes=10)}   # PC-B's clock is 10 minutes ahead

    def comp(name):
        data = tmp_path / name
        return BudgetFolder(Database(data / "budget.db"), data, None, computer_name=name, app_version="1.5.0",
                            machine_id=f"machine-{name}", clock=Clock(), now=lambda: clocks[name], sleep=lambda s: None)

    folder = tmp_path / "Budget"
    a = comp("PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = comp("PC-B")
    b.open_from(folder)
    first = shutil.copyfile(backups.make(a.db), tmp_path / "first.db")
    add_category(a.db, "Rent")
    a.check(wait=False); b.check(wait=False)
    second = shutil.copyfile(backups.make(a.db), tmp_path / "second.db")
    add_category(a.db, "Bills")
    a.check(wait=False); b.check(wait=False)
    seen_on_b = _view(b, folder)
    shutil.copyfile(second, seen_on_b / FILE_NAME)      # restored at PC-B first (real time)...
    assert b.check(wait=False)["took"]
    clocks["PC-A"] += timedelta(minutes=2)
    shutil.copyfile(first, folder / FILE_NAME)          # ...and another one at PC-A two minutes later
    assert a.check(wait=False)["took"]
    add_category(a.db, "Gifts"); a.check(wait=False)
    add_category(b.db, "Pets"); b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")
    b.folder, b._reachable, b._seen = folder, None, {}
    sa = a.check(wait=False)
    sb = b.check(wait=False)
    kept = set()
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".inab", ".db") and p.name not in ("budget.db", "first.db", "second.db") \
                and "Saves" not in p.parts and "PC-B sees" not in p.parts:
            kept |= set(categories_in(p))
    print("A:", sa["state"], sa.get("took"), categories(a.db), "| B:", sb["state"], categories(b.db), "| anywhere:", sorted(kept))
    assert "Gifts" in kept | set(categories(a.db)) | set(categories(b.db)), "PC-A's Gifts lost without asking"



@pytest.mark.parametrize("a_looks_first", [True, False])
def test_a_save_in_flight_when_a_backup_is_restored(tmp_path, a_looks_first):
    """PC-B saves Pets; its sync is behind. A backup is restored in the folder. The sync tool keeps
    PC-B's as Budget.inab and makes the restored one the copy. Nobody is asked; Pets is in no file."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False); b.check(wait=False)
    seen_on_b = _view(b, folder)
    add_category(b.db, "Pets")
    b.check(wait=False)                                  # PC-B's save, not synced yet
    shutil.copyfile(made, folder / FILE_NAME)            # the person restores a backup
    if a_looks_first:
        assert a.check(wait=False)["took"]               # PC-A takes it and saves it back as itself
    shutil.copyfile(folder / FILE_NAME, folder / "Budget-PC-A.inab")   # the sync tool keeps PC-B's
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)
    b.folder, b._reachable, b._seen = folder, None, {}
    states = []
    for _ in range(3):
        states += [a.check(wait=False)["state"], b.check(wait=False)["state"]]
    kept = set()
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".inab", ".db") and p.name not in ("budget.db", "the backup.db") \
                and "Saves" not in p.parts and "PC-B sees" not in p.parts:
            kept |= set(categories_in(p))
    print("states:", states, "A:", categories(a.db), "B:", categories(b.db), "anywhere:", sorted(kept))
    assert "Pets" in kept | set(categories(a.db)) | set(categories(b.db)) or "two_computers" in states


def test_opening_the_same_folder_after_a_restore(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    add_category(a.db, "Bills")                          # not saved yet
    shutil.copyfile(made, folder / FILE_NAME)
    state = a.open_from(folder)                          # Settings' Open… on its own folder
    print("state:", state["state"], categories(a.db), [categories_in(p) for p in sorted((folder / "Backups").glob("*not-kept*"))])
    assert state["state"] == "two_computers", "same budget treated as another: no question"


def test_the_same_change_saved_on_two_computers_is_joined_without_a_question(tmp_path):
    """PC-A and PC-B make the very same change apart (random seed 4045): their saves' counts are
    apart, their contents the same. Joining them leaves Budget.inab, which PC-B has just joined,
    not an older one put back."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    seen_on_b = _view(b, folder)
    add_category(a.db, "Gifts")
    assert a.check(wait=False)["state"] == "saved"
    add_category(b.db, "Gifts")                          # the same change, saved where PC-B's sync is behind
    assert b.check(wait=False)["state"] == "saved"
    assert backups.digest(a.db) == backups.digest(b.db)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")  # the sync tool keeps PC-A's
    b.folder, b._reachable, b._seen = folder, None, {}
    b._point_backups()
    assert b.check(wait=False)["state"] == "saved"
    assert a.check(wait=False)["state"] == "saved"
    assert names(folder).count(FILE_NAME) == 1 and "Budget-PC-B.inab" not in names(folder)
    assert categories(a.db) == categories(b.db) == ["Groceries", "Gifts"]
    assert not not_kept(folder)


def test_following_a_moved_budget_brings_what_reached_the_old_folder_since(tmp_path):
    """PC-B's sync is behind when PC-A moves the budget (random seed 582): a copy PC-B put in
    Backups/ meanwhile (Keep this one) reaches the old folder after the move, and follows the budget."""
    folder, moved = tmp_path / "Budget", tmp_path / "Moved"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    seen_on_b = _view(b, folder)
    a.move_to(moved)
    b._point_backups()
    backups.save(b.db, b._not_kept_name())               # what a question on PC-B put away
    shutil.copytree(seen_on_b / "Backups", folder / "Backups", dirs_exist_ok=True)  # synced after the move
    b.folder, b._reachable, b._seen = folder, None, {}
    b._point_backups()
    assert b.check(wait=False)["state"] == "moved"
    b.open_from(moved)
    assert len(not_kept(moved)) == 1
    assert not (folder / "Backups").exists()


def test_moving_an_unchanged_budget_is_the_same_save_and_a_changed_one_a_new_save(tmp_path, monkeypatch):
    """Move… with nothing changed puts the very save in the new folder (random seed 172); a change
    that slips in before the copy is made (a request) makes it a new save, never other contents
    under the same one."""
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(tmp_path / "Budget")
    first = a._mine()
    a.move_to(tmp_path / "Moved")
    assert save_in(tmp_path / "Moved" / FILE_NAME)["id"] == first.id

    def unchanged():  # it looks unchanged, then a request changes it before the copy is made
        add_category(a.db, "Rent")
        return True
    monkeypatch.setattr(a, "_unchanged", unchanged)
    a.move_to(tmp_path / "Moved again")
    moved = save_in(tmp_path / "Moved again" / FILE_NAME)
    assert moved["id"] != first.id and moved["counts"] != first.counts
    assert categories_in(tmp_path / "Moved again" / FILE_NAME) == ["Groceries", "Rent"]


def test_keep_this_over_a_restore_is_taken_by_a_computer_that_only_took_the_restore(tmp_path):
    """Restoring by copying a backup in (the maintainer's choice on #196). PC-B changed nothing and
    takes the restore: its budget goes into Backups/ first, and its counts are the restore's alone.
    PC-A had a change not saved yet, so it's asked; Keep this counts the restore too, so PC-B takes
    it without a question."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    assert b.check(wait=False)["took"]
    add_category(a.db, "Bills")                          # not saved yet
    shutil.copyfile(made, folder / FILE_NAME)            # the person restores the backup
    assert b.check(wait=False)["took"] and categories(b.db) == ["Groceries"]
    assert b._mine().counts == {budget_folder.restore_key(backups.digest(b.db)): 1}
    assert [categories_in(p) for p in not_kept(folder)] == [["Groceries", "Rent"]]
    assert a.check(wait=False)["state"] == "two_computers"
    a.keep_this()
    state = b.check(wait=False)
    assert state["state"] == "saved" and state["took"] and categories(b.db) == ["Groceries", "Rent", "Bills"]


def test_two_computers_taking_one_restore_keep_one_copy_of_what_they_had(tmp_path):
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b, c = computer(tmp_path, "PC-B"), computer(tmp_path, "PC-C")
    b.open_from(folder)
    c.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    assert b.check(wait=False)["took"] and c.check(wait=False)["took"]
    shutil.copyfile(made, folder / FILE_NAME)
    for each in (a, b, c):
        assert each.check(wait=False)["state"] == "saved" and categories(each.db) == ["Groceries"]
    assert [categories_in(p) for p in not_kept(folder)] == [["Groceries", "Rent"]]  # once, by its contents


def test_a_computer_that_missed_the_restore_of_its_very_contents_takes_what_came_after(tmp_path):
    """PC-A doesn't look while a backup with exactly its budget is restored and PC-B saves on top
    of it (random seed 401). Nothing PC-A has is missing from PC-B's save: it's taken, unasked."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    shutil.copyfile(made, folder / FILE_NAME)            # the very contents PC-A and PC-B have
    assert b.check(wait=False)["state"] == "saved"       # PC-B: the restore itself, saved back as itself
    add_category(b.db, "Bills")
    b.check(wait=False)
    state = a.check(wait=False)
    assert state["state"] == "saved" and state["took"] and categories(a.db) == ["Groceries", "Bills"]


def test_a_save_built_on_the_same_change_made_apart_is_taken(tmp_path):
    """PC-A and PC-C add Gifts apart; PC-A joins PC-C's save (the very same contents), then PC-C,
    whose sync is behind, adds Rent on top of its own (random seed 959). PC-A changed nothing since:
    everything it has is in PC-C's save, so it's taken, unasked."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    c = computer(tmp_path, "PC-C")
    c.open_from(folder)
    seen_on_c = _view(c, folder)
    add_category(a.db, "Gifts")
    a.check(wait=False)
    add_category(c.db, "Gifts")                          # the same change, where PC-C's sync is behind
    c.check(wait=False)
    shutil.copyfile(seen_on_c / FILE_NAME, folder / "Budget-PC-C.inab")  # the sync tool keeps PC-A's
    assert a.check(wait=False)["state"] == "saved"       # the same contents: joined
    add_category(c.db, "Rent")                           # on top of its own, still behind
    c.check(wait=False)
    shutil.copyfile(folder / FILE_NAME, folder / "Budget-cloud.inab")    # now the sync tool keeps PC-C's
    shutil.copyfile(seen_on_c / FILE_NAME, folder / FILE_NAME)
    state = a.check(wait=False)
    assert state["state"] == "saved" and state["took"] and categories(a.db) == ["Groceries", "Gifts", "Rent"]


# The sixth review of #196 (reproductions against 03ab640). Made-up data only.

def backups_in(folder):
    return [sorted(categories_in(p)) for p in sorted((folder / "Backups").glob("*.db"))]


def test_a_computer_that_missed_a_restore_and_changed_nothing_takes_what_came_after(tmp_path):
    """PC-B is off while a backup is restored and PC-A adds Gifts on top. PC-B changed nothing."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False)
    b.check(wait=False)                                  # PC-B has Groceries, Rent; then it's off
    shutil.copyfile(made, folder / FILE_NAME)            # the person restores the backup
    assert a.check(wait=False)["took"]
    add_category(a.db, "Gifts")
    a.check(wait=False)                                  # PC-A's change on top of the restore
    state = b.check(wait=False)                          # PC-B comes back, nothing changed on it
    print("B:", state["state"], categories(b.db))
    assert state["state"] == "saved" and "Gifts" in categories(b.db), "asked though only PC-A changed"


def test_a_change_back_to_a_restores_contents_is_asked_about(tmp_path):
    """After a restore, PC-A adds Gifts and PC-B takes it. PC-B (sync behind) deletes Gifts: its
    budget is the restore's very contents again. PC-A adds Pets. Both changed."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent")
    a.check(wait=False); b.check(wait=False)
    shutil.copyfile(made, folder / FILE_NAME)
    a.check(wait=False); b.check(wait=False)
    assert categories(a.db) == categories(b.db) == ["Groceries"]
    add_category(a.db, "Gifts")
    a.check(wait=False)
    assert b.check(wait=False)["took"]
    seen_on_b = _view(b, folder)
    delete_category(b.db, "Gifts")                       # PC-B's change: Gifts goes
    b.check(wait=False)
    add_category(a.db, "Pets")                           # PC-A's change: Pets
    a.check(wait=False)
    shutil.copyfile(folder / FILE_NAME, folder / "Budget-PC-A.inab")    # the sync tool keeps PC-B's
    shutil.copyfile(seen_on_b / FILE_NAME, folder / FILE_NAME)
    b.folder, b._reachable, b._seen = folder, None, {}
    b._point_backups()
    states = []
    for _ in range(2):
        states += [a.check(wait=False)["state"], b.check(wait=False)["state"]]
    print("states:", states, "A:", categories(a.db), "B:", categories(b.db), "backups:", backups_in(folder))
    assert "two_computers" in states, "PC-B's deletion was undone and nobody was asked"


def test_keep_this_answered_late_on_the_second_computer(tmp_path):
    """Both changed; PC-A answers Keep this first. PC-B's question was open before that and it
    answers Keep this without looking again: PC-A's kept change is put away, nobody asked about it."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    seen_on_b = _view(b, folder)
    add_category(a.db, "Rent"); a.check(wait=False)
    add_category(b.db, "Gifts"); b.check(wait=False)
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")
    shutil.copyfile(folder / FILE_NAME, seen_on_b / "Budget-PC-A.inab")
    assert a.check(wait=False)["state"] == "two_computers"
    assert b.check(wait=False)["state"] == "two_computers"
    a.keep_this()                                        # PC-A keeps Rent
    shutil.copyfile(folder / FILE_NAME, seen_on_b / FILE_NAME)  # PC-B's sync catches up...
    b.folder, b._reachable, b._seen = folder, None, {}
    b._point_backups()
    state = b.keep_this()                                # ...as its person answers the open question
    print("B:", state["state"], "A:", categories(a.db), "B:", categories(b.db), "backups:", backups_in(folder))
    # PC-A's kept save has the very contents PC-B's person was shown (Rent): their answer stands, Rent in Backups/
    assert state["state"] == "saved" and ["Groceries", "Rent"] in backups_in(folder)
    assert a.check(wait=False)["took"] and categories(a.db) == categories(b.db) == ["Groceries", "Gifts"]


def test_a_computer_that_missed_a_restore_after_saving_last_takes_what_came_after(tmp_path):
    """PC-B made the last change before the restore (PC-A took it). PC-B is off while the backup is
    restored and PC-A adds Gifts on top. PC-B changed nothing since."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(b.db, "Rent")
    b.check(wait=False)
    assert a.check(wait=False)["took"]                   # PC-B's save, taken; then PC-B is off
    shutil.copyfile(made, folder / FILE_NAME)            # the person restores the backup
    assert a.check(wait=False)["took"]
    add_category(a.db, "Gifts")
    a.check(wait=False)
    state = b.check(wait=False)                          # PC-B comes back, nothing changed on it
    print("B:", state["state"], categories(b.db), b._mine().counts, a._mine().counts)
    assert state["state"] == "saved" and "Gifts" in categories(b.db), "asked though only PC-A changed"


def test_keep_this_on_two_computers_over_the_same_restore(tmp_path):
    """Both have a change not saved yet when a backup is restored; both are asked about the restore
    and both answer Keep this. PC-B's answer comes after PC-A's save landed."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Rent"); a.check(wait=False); b.check(wait=False)
    add_category(a.db, "Bills")                          # PC-A's change, not saved yet
    add_category(b.db, "Gifts")                          # PC-B's change, not saved yet
    shutil.copyfile(made, folder / FILE_NAME)
    assert a.check(wait=False)["state"] == "two_computers"
    assert b.check(wait=False)["state"] == "two_computers"
    a.keep_this()                                        # PC-A: keep Bills over the restore
    b.keep_this()                                        # PC-B: keep Gifts over the restore (its question was about the restore)
    states = []
    for _ in range(2):
        states += [a.check(wait=False)["state"], b.check(wait=False)["state"]]
    print("states:", states, "A:", categories(a.db), "B:", categories(b.db), "backups:", backups_in(folder))
    assert "Bills" in categories(a.db) or "two_computers" in states[2:], \
        "PC-A's kept Bills went to Backups; nobody was asked about Bills against Gifts"


def test_an_older_save_of_a_restore_put_back_is_asked_about(tmp_path):
    """A first restore is taken and saved back as itself (that file stays in the sync tool's version
    history). Later another backup is restored and changed on. The sync tool's "restore previous
    version" puts the first one's save back: an older save put back should ask."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    first = shutil.copyfile(backups.make(a.db), tmp_path / "first.db")
    add_category(a.db, "Rent"); a.check(wait=False)
    second = shutil.copyfile(backups.make(a.db), tmp_path / "second.db")
    add_category(a.db, "Bills"); a.check(wait=False)
    shutil.copyfile(first, folder / FILE_NAME)
    assert a.check(wait=False)["took"]                   # saved back as itself
    history = shutil.copyfile(folder / FILE_NAME, tmp_path / "version history.inab")
    shutil.copyfile(second, folder / FILE_NAME)
    assert a.check(wait=False)["took"]
    add_category(a.db, "Gifts"); a.check(wait=False)
    shutil.copyfile(history, folder / FILE_NAME)         # "restore previous version"
    state = a.check(wait=False)
    print("A:", state["state"], state.get("took"), categories(a.db))
    assert state["state"] == "older", "went back to an older save without asking"


def test_two_computers_with_the_same_name_never_share_a_file_name(tmp_path):
    """Two laptops both called "Laptop" (the sixth review): their backups, open notes and half-made
    copies are named by their ids too, so neither prunes, removes or overwrites the other's."""
    folder = tmp_path / "Budget"
    a = BudgetFolder(Database(tmp_path / "one" / "budget.db"), tmp_path / "one", None, computer_name="Laptop",
                     app_version="1.5.0", machine_id="machine-one", clock=Clock(), now=lambda: NOW, sleep=lambda s: None)
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = BudgetFolder(Database(tmp_path / "two" / "budget.db"), tmp_path / "two", None, computer_name="Laptop",
                     app_version="1.5.0", machine_id="machine-two", clock=Clock(), now=lambda: NOW, sleep=lambda s: None)
    b.open_from(folder)
    assert a._tag() != b._tag() and a._tag().startswith("Laptop ") and a._note_name() != b._note_name()
    assert {a._note_name(), b._note_name()} <= set(names(folder))
    assert b.others_open() == [{"computer": "Laptop", "since": a.others_open()[0]["since"]}]
    when = datetime(2026, 11, 27, 9, 14, 5)
    made = {backups.make(a.db, when).name, backups.make(b.db, when).name}
    assert len(made) == 2 and made <= set(names(folder / "Backups"))
    assert budget_folder.tag_in(tmp_path / "one", "Laptop") == a._tag()


def test_a_backup_restored_into_the_folder_the_budget_moves_out_of_is_kept(tmp_path, monkeypatch):
    """The sixth review: a backup copied into the old folder during Move… (after it looked, before it
    left), or after it, isn't taken (the move stands), and goes into the new folder's Backups/."""
    old, new = tmp_path / "Budget", tmp_path / "Moved"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(old)
    b = computer(tmp_path, "PC-B")
    b.open_from(old)
    made = shutil.move(backups.make(a.db), tmp_path / "the backup.db")  # kept aside, not in Backups/
    add_category(a.db, "Rent")
    a.check(wait=False)
    assert b.check(wait=False)["took"]
    real = BudgetFolder._leave

    def restored_meanwhile(self, folder):
        shutil.copyfile(made, folder / FILE_NAME)        # the person restores it in the old folder just then
        return real(self, folder)

    monkeypatch.setattr(BudgetFolder, "_leave", restored_meanwhile)
    a.move_to(new)
    monkeypatch.setattr(BudgetFolder, "_leave", real)
    assert categories(a.db) == ["Groceries", "Rent"] and [categories_in(p) for p in not_kept(new)] == [["Groceries"]]
    add_category(b.db, "Gifts")                          # another backup restored there after the move
    shutil.move(backups.make(b.db), old / FILE_NAME)
    assert b.check(wait=False)["state"] == "moved"
    b.open_from(new)
    assert sorted(categories_in(p) for p in not_kept(new)) == [["Groceries"], ["Groceries", "Rent", "Gifts"]]


def test_backups_named_by_an_earlier_tag_are_still_this_computers_to_prune(tmp_path):
    """The seventh review: a computer's tag changes with its name, or its id when a budget is started
    again. Its backups under the tag it had before are still its own to prune; another computer's never."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    before = a._tag()
    a.computer = "Office PC"                             # renamed
    a._use(folder)
    assert a.db.backup_tag != before and a.db.backup_earlier_tags == (before,)
    backups_dir = folder / "Backups"
    long_ago, last_week = datetime(2026, 1, 1, 9, 0), datetime(2026, 11, 20, 9, 0)
    old_mine = backups_dir / backups.name(long_ago, before)
    old_theirs = backups_dir / backups.name(long_ago, "PC-B 123abc")
    recent = backups_dir / backups.name(last_week, a.db.backup_tag)  # the newest before today stays
    for path in (old_mine, old_theirs, recent):
        shutil.copyfile(backups.make(a.db, datetime(2026, 11, 27, 9, 0, 1)), path)
    gone = backups.prune(backups_dir, datetime(2026, 11, 27).date(), a.db.backup_tag, a.db.backup_earlier_tags)
    assert old_mine in gone and old_theirs.exists() and recent.exists()
    other = computer(tmp_path / "elsewhere", "PC-A")     # this data folder copied to another machine keeps none
    shutil.copyfile(a.db.path.parent / budget_folder.COMPUTER_FILE, other.db.path.parent / budget_folder.COMPUTER_FILE)
    other.machine_id = lambda: "another machine"
    other._use(folder)
    assert other.db.backup_earlier_tags == ()


# The seventh review of #196 (reproductions against 27d8e89). Made-up data only.

def back_in(c, folder):
    c.folder, c._reachable, c._seen = folder, None, {}
    c._point_backups()


def test_a_change_that_joins_another_save_is_still_a_change_and_asked_about(tmp_path):
    """PC-A deletes Gifts, and its budget then has the very contents of PC-B's save (PC-B added and
    removed Pets): the app joins the two, with no count of PC-A's own for the deletion. PC-C,
    asked about PC-A's earlier save (with Gifts) against PC-B's, keeps PC-A's. That choice counts
    everything the join does, so PC-A takes it unasked and Gifts is back."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b, c = computer(tmp_path, "PC-B"), computer(tmp_path, "PC-C")
    b.open_from(folder); c.open_from(folder)
    seen_on_b = _view(b, folder)                         # PC-B's sync falls behind
    add_category(a.db, "Gifts"); a.check(wait=False)     # PC-A: Gifts
    assert c.check(wait=False)["took"]
    start = backups.digest(b.db)
    add_category(b.db, "Pets"); b.check(wait=False)      # PC-B adds Pets and takes it out again
    delete_category(b.db, "Pets"); b.check(wait=False)
    assert backups.digest(b.db) == start
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")  # PC-B's save arrives as a copy
    back_in(b, folder)
    seen_on_a = _view(a, folder)                         # now PC-A's sync falls behind
    delete_category(a.db, "Gifts")                       # PC-A's change: Gifts goes
    state = a.check(wait=False)
    print("A after deleting Gifts:", state["state"], a._mine().counts)
    assert c.check(wait=False)["state"] == "two_computers"
    c.keep_this()                                        # PC-C's person keeps PC-A's earlier save (Gifts)
    shutil.copyfile(seen_on_a / FILE_NAME, folder / "Budget-PC-A.inab")  # PC-A's join arrives as a copy
    back_in(a, folder)
    states = []
    for _ in range(2):
        states += [a.check(wait=False)["state"], b.check(wait=False)["state"], c.check(wait=False)["state"]]
    print("states:", states, "A:", categories(a.db), "B:", categories(b.db), "C:", categories(c.db))
    assert "Gifts" not in categories(a.db) and "two_computers" in states[:3] and "two_computers" in states[3:], \
        "PC-A's deletion of Gifts was undone and nobody was asked"


def test_a_change_not_saved_yet_that_joins_a_copy_still_asks_about_a_restore(tmp_path):
    """PC-A, its folder unplugged, adds Rent (not saved). PC-B (sync behind) makes the same change.
    A backup is restored; PC-B's save arrives as a copy. PC-A comes back: it joins the copy (the very
    contents of its unsaved change), so it looks unchanged, and takes the restore in the same look."""
    folder = tmp_path / "Budget"
    a = computer(tmp_path, "PC-A")
    add_category(a.db, "Groceries")
    a.move_to(folder)
    b = computer(tmp_path, "PC-B")
    b.open_from(folder)
    made = shutil.copyfile(backups.make(a.db), tmp_path / "the backup.db")
    add_category(a.db, "Bills"); a.check(wait=False)     # both have Bills after the backup
    assert b.check(wait=False)["took"]
    back_in(a, tmp_path / "Unplugged")                   # PC-A's folder can't be reached
    assert a.check(wait=False)["state"] == "unreachable"
    add_category(a.db, "Rent")                           # PC-A's change, not saved
    seen_on_b = _view(b, folder)
    add_category(b.db, "Rent"); b.check(wait=False)      # the same change on PC-B, saved in its copy
    assert backups.digest(a.db) == backups.digest(b.db)
    shutil.copyfile(made, folder / FILE_NAME)            # a backup restored
    shutil.copyfile(seen_on_b / FILE_NAME, folder / "Budget-PC-B.inab")  # PC-B's save arrives as a copy
    back_in(b, folder)
    back_in(a, folder)                                   # PC-A's folder is back
    state = a.check(wait=False)
    print("A:", state["state"], state.get("took"), categories(a.db), "backups:", backups_in(folder), names(folder))
    assert state["state"] == "two_computers", "PC-A had a change not saved yet and the restore replaced it unasked"
    assert categories(a.db) == ["Groceries", "Bills", "Rent"] and a.check(wait=False)["state"] == "two_computers"
