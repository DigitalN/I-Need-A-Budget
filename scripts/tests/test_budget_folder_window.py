# SPDX-License-Identifier: GPL-3.0-only
"""The budget folder as the window sees it (issue #189): what each computer changed, worked out for the
window's question (service.budget_folder_question, from changes.py), and the cases that need no question.
Two data folders sharing one budget folder stand in for two computers. Made-up data, throwaway folders."""

import json
import shutil

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


def test_both_changed_asks_with_what_each_changed(tmp_path):
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
    call(b, "keep_other_budget")  # Keep PC-A's
    assert categories(b) == ["Groceries", "Rent"]
    assert call(b, "budget_folder_question") is None


def test_nothing_to_see_changed_here_takes_the_other_computers_without_asking(tmp_path):
    folder, a, b = two_computers(tmp_path)
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    fold_the_group(b)  # B changed only what the window wouldn't show
    state = b.budget_folder_look()
    assert state["state"] == "saved" and state["took"]["computer"] == "PC-A"
    assert categories(b) == ["Groceries", "Rent"]
    assert call(b, "budget_folder_question") is None
    backups = [p.name for p in (folder / "Backups").iterdir()]
    assert any(n.startswith("budget-not-kept-") for n in backups)  # B's is still kept as a backup


def test_nothing_to_see_changed_on_the_other_keeps_this_computers_without_asking(tmp_path):
    folder, a, b = two_computers(tmp_path)
    fold_the_group(a)
    a.budget_folder_look(now=True)  # A saved, having changed nothing the window would show
    add_category(b, "Fuel")
    state = b.budget_folder_look()
    assert state["state"] in ("saved", "unsaved") and "took" not in state
    assert categories(b) == ["Groceries", "Fuel"]
    assert a.budget_folder_look()["took"]["computer"] == "PC-B"  # and A takes it
    assert categories(a) == ["Groceries", "Fuel"]


def test_when_the_save_both_started_from_is_gone_it_asks_without_the_changes(tmp_path):
    folder, a, b = two_computers(tmp_path)
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    add_category(b, "Fuel")
    shutil.rmtree(tmp_path / "PC-B" / SAVES)
    assert b.budget_folder_look()["state"] == "two_computers"
    question = call(b, "budget_folder_question")
    assert question["can_show_changes"] is False and question["this"] is None and question["theirs"] is None


def test_an_older_budget_put_back_always_asks(tmp_path):
    """Use it, or Keep the newer one: going back is never decided without asking, even with nothing to see."""
    folder, a, b = two_computers(tmp_path)
    older = tmp_path / "older.inab"
    shutil.copyfile(folder / FILE_NAME, older)
    fold_the_group(b)
    b.budget_folder_look(now=True)  # B's newer save, different only in what the window wouldn't show
    shutil.copyfile(older, folder / FILE_NAME)  # a sync tool's "restore previous version"
    assert b.budget_folder_look()["state"] == "older"
    question = call(b, "budget_folder_question")
    assert question["state"] == "older" and question["other"]["computer"] == "PC-A"
    assert question["can_show_changes"] and not question["this"]["changed"]
    call(b, "keep_this_budget")  # Keep the newer one
    assert call(b, "budget_folder_question") is None and b.budget_folder_look()["state"] == "saved"


def test_the_question_is_none_until_it_is_worked_out(tmp_path):
    """The window asks only once the look that found it has worked out what each changed."""
    folder, a, b = two_computers(tmp_path)
    assert call(b, "budget_folder_question") is None
    add_category(a, "Rent")
    a.budget_folder_look(now=True)
    add_category(b, "Fuel")
    assert b.folder.check()["state"] == "two_computers"  # a look that isn't the window's
    assert call(b, "budget_folder_question") is None
    b.budget_folder_look()
    assert call(b, "budget_folder_question")["state"] == "two_computers"
