# SPDX-License-Identifier: GPL-3.0-only
"""The budget folder as the window sees it (issue #189): budget_folder.py's one question, "The budget in
the folder is older than this one" or "This budget changed on two computers", with what each side changed
(service.budget_folder_question, from changes.py), answered Use it or Keep this. The window decides
nothing itself: a side with nothing to show is still asked about (ADR 0008's rules are the only ones).
Two data folders sharing one budget folder stand in for two computers. Made-up data, throwaway folders."""

import json
import shutil

import backups
from budget_folder import FILE_NAME, SAVES
from db import Database
from helpers import call
from service import BudgetService
from settings import Settings


def computer(tmp_path, name):
    data = tmp_path / name
    return BudgetService(Database(data / "budget.db"), Settings(data / "settings.json"), data, computer_name=name)


def add_category(service, name):
    with service.db.write("Add category") as c:
        found = c.execute("SELECT id FROM category_groups WHERE name = 'Everyday'").fetchone()
        group = found[0] if found else c.execute("INSERT INTO category_groups (name) VALUES ('Everyday')").lastrowid
        c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, name))


def fold_the_group(service):
    """A change the window has nothing to show for (changes.py never lists a group folded)."""
    with service.db.read() as c:
        group = c.execute("SELECT id FROM category_groups WHERE name = 'Everyday'").fetchone()[0]
    call(service, "set_group_collapsed", group, True)


def categories(service):
    with service.db.read() as c:
        return [r[0] for r in c.execute("SELECT name FROM categories ORDER BY id")]


def two_computers(tmp_path):
    """PC-A's budget in the folder, opened on PC-B; both with Groceries."""
    folder = tmp_path / "Budget"
    a, b = computer(tmp_path, "PC-A"), computer(tmp_path, "PC-B")
    call(a, "move_budget_folder", str(folder))
    add_category(a, "Groceries")
    a.budget_folder_look(now=True)
    call(b, "open_budget_folder", str(folder))
    assert categories(b) == ["Groceries"]
    return folder, a, b


def test_changed_on_two_computers_asks_with_what_each_side_changed(tmp_path):
    folder, a, b = two_computers(tmp_path)
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    add_category(b, "Fuel")
    state = b.budget_folder_look()
    assert state["state"] == "two_computers" and "took" not in state
    question = call(b, "budget_folder_question")
    assert question["state"] == "two_computers" and question["other"]["computer"] == "PC-A"
    assert question["can_show_changes"]
    assert question["this"]["changed"] and "Fuel" in json.dumps(question["this"]) and "Rent" not in json.dumps(question["this"])
    assert question["theirs"]["changed"] and "Rent" in json.dumps(question["theirs"])
    assert question["this"]["summary"] and question["this"]["sections"]  # the summary, and Show every change

    b.budget_folder_look()  # the next round asks the same question: worked out once
    assert call(b, "budget_folder_question") == question
    call(b, "keep_other_budget")  # Use it
    assert categories(b) == ["Groceries", "Rent"]
    assert call(b, "budget_folder_question") is None
    assert a.budget_folder_look()["state"] == "saved"  # PC-A takes the answer (rule 2), unasked


def test_keep_this_saves_this_side_and_the_other_takes_it(tmp_path):
    folder, a, b = two_computers(tmp_path)
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    add_category(b, "Fuel")
    assert b.budget_folder_look()["state"] == "two_computers"
    call(b, "keep_this_budget")
    assert call(b, "budget_folder_question") is None
    assert a.budget_folder_look()["took"]["computer"] == "PC-B"
    assert categories(a) == ["Groceries", "Fuel"]


def test_a_side_with_nothing_to_show_is_still_asked_about(tmp_path):
    """The window adds no rule of its own: a change changes.py doesn't list (a group folded) on one side
    still makes it changed on two computers, shown as nothing to see on that side."""
    folder, a, b = two_computers(tmp_path)
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    fold_the_group(b)
    assert b.budget_folder_look()["state"] == "two_computers"
    question = call(b, "budget_folder_question")
    assert question["can_show_changes"] and not question["this"]["changed"] and question["theirs"]["changed"]
    assert categories(b) == ["Groceries"]  # nothing taken until you choose
    call(b, "keep_other_budget")  # Use it
    assert categories(b) == ["Groceries", "Rent"]


def test_when_what_both_started_from_is_gone_it_asks_without_the_changes(tmp_path):
    folder, a, b = two_computers(tmp_path)
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    add_category(b, "Fuel")
    shutil.rmtree(tmp_path / "PC-B" / SAVES)
    assert b.budget_folder_look()["state"] == "two_computers"
    question = call(b, "budget_folder_question")
    assert question["can_show_changes"] is False and question["this"] is None and question["theirs"] is None


def test_a_backup_copied_in_asks_older_once_and_the_others_take_the_answer(tmp_path):
    """Restoring: quit everywhere, copy a backup in as Budget.inab. It's an old save: the first computer
    to look asks "older" (Use it), with what this computer changed since; the other takes the answer."""
    folder, a, b = two_computers(tmp_path)
    backup = backups.at_quit(a.db)  # PC-A quits with Groceries
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    assert b.budget_folder_look()["state"] == "saved" and categories(b) == ["Groceries", "Rent"]
    shutil.copyfile(backup, folder / FILE_NAME)  # the backup put back

    assert a.budget_folder_look()["state"] == "older"
    question = call(a, "budget_folder_question")
    assert question["state"] == "older" and question["other"]["computer"] is None  # nothing kept about saves
    assert question["can_show_changes"] and "Rent" in json.dumps(question["this"])
    call(a, "keep_other_budget")  # Use it
    assert categories(a) == ["Groceries"]
    assert b.budget_folder_look()["took"] and categories(b) == ["Groceries"]


def test_the_question_is_none_until_it_is_worked_out(tmp_path):
    """The window asks only once the look that found it has worked out what each side changed."""
    folder, a, b = two_computers(tmp_path)
    assert call(b, "budget_folder_question") is None
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    add_category(b, "Fuel")
    assert b.folder.check()["state"] == "two_computers"  # a look that isn't the window's
    assert call(b, "budget_folder_question") is None
    b.budget_folder_look()
    assert call(b, "budget_folder_question")["state"] == "two_computers"
