# SPDX-License-Identifier: GPL-3.0-only
"""Every change of yours that changes the budget is one the window can show (issue #189, docs/adr/0008).

A budget changed on two computers is asked about with what each side changed (changes.py); a side whose change
backups.digest sees but changes.py doesn't would read "Nothing you'd see". So each action here, on a fresh copy of
a settled made-up budget (scripts/demo_data.py's), changes the digest exactly when changes.py lists something:
the budget's own changes are shown, and what isn't the budget (this computer's settings, a group folded on the
Budget page: how this computer shows the page) changes neither. Throwaway folders only."""

import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import backups
import changes
from db import Database
from helpers import call
from service import BudgetService
from settings import Settings

REPO = Path(__file__).resolve().parents[2]
TODAY, MONTH = "2026-11-20", "2026-11"


def ofx_file(path, rows, balance, as_of):
    """A made-up Quicken file for the demo budget's chequing account (demo_data.ofx)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("demo_data", REPO / "scripts" / "demo_data.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    path.write_text(demo.ofx(rows, False, "000123456789", balance, as_of), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def start(tmp_path_factory):
    """The made-up budget, settled (the launch re-check run once), with one more thing waiting: a charge whose payee
    has a similar name to one you have ("Is Sample Greengrocer the same as Sample Grocer?"). Returns its file and
    the ids the actions use."""
    folder = tmp_path_factory.mktemp("start")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("INAB_TODAY", TODAY)
        subprocess.run([sys.executable, str(REPO / "scripts" / "demo_data.py"), str(folder / "made"), "--today", TODAY],
                       check=True, capture_output=True)
        service = open_service(folder / "made")
        call(service, "recheck")
        balance = next(a for a in call(service, "accounts") if a["kind"] == "chequing")["balance"]
        call(service, "import_files", [str(ofx_file(folder / "made" / "downloads" / "grocer.qfx",
                                                    [(date(2026, 11, 17), -3000, "SAMPLE GROCER CALGARY AB", "G1")],
                                                    balance - 3000, date(2026, 11, 17)))])
        grocer = next(i["id"] for i in call(service, "inbox")["items"] if "GROCER" in str(i).upper())
        food = next(c["id"] for g in call(service, "categories") for c in g["categories"] if c["name"] == "Food")
        call(service, "categorize", [grocer], food)
        call(service, "import_files", [str(ofx_file(folder / "made" / "downloads" / "greengrocer.qfx",
                                                    [(date(2026, 11, 19), -2200, "SAMPLE GREENGROCER CALGARY AB", "G2")],
                                                    balance - 5200, date(2026, 11, 19)))])
        items = call(service, "inbox")["items"]
        ids = {"similar": next(i["id"] for i in items if i.get("similar_payee")),
               "waiting": [i["id"] for i in items]}
        with service.db.read() as c:
            ids["groups"] = [r[0] for r in c.execute("SELECT id FROM category_groups WHERE role = 'normal' ORDER BY sort")]
            ids["categories"] = [tuple(r) for r in c.execute(
                "SELECT c.id, c.group_id FROM categories c JOIN category_groups g ON g.id = c.group_id "
                "WHERE g.role = 'normal' ORDER BY c.id")]
            ids["payee"] = c.execute("SELECT id FROM payees ORDER BY id LIMIT 1").fetchone()[0]
            ids["accounts"] = [r[0] for r in c.execute("SELECT id FROM accounts ORDER BY id")]
            ids["charge"] = c.execute("SELECT id FROM transactions WHERE status = 'done' AND amount < 0 AND "
                                      "subscription_id IS NULL ORDER BY id DESC LIMIT 1").fetchone()[0]
        service.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        service.db.conn.close()
    shutil.copyfile(folder / "made" / "budget.db", folder / "start.db")
    return folder / "start.db", ids


def open_service(data):
    settings = Settings(data / "settings.json")
    if not settings.get()["downloads_dir"]:  # never the real Downloads
        (data / "downloads").mkdir(parents=True, exist_ok=True)
        settings.update({"downloads_dir": str(data / "downloads")})
    return BudgetService(Database(data / "budget.db"), settings, data)


def balance_of(s, account):
    return next(a for a in call(s, "accounts") if a["id"] == account)


def a_file_not_to_import(data):
    path = data / "downloads" / "notes.csv"
    path.write_text("Date,Description,Amount\n2026-11-01,SAMPLE,-1.00\n", encoding="utf-8")
    return str(path)


# (what you do, how): each on a fresh copy of the settled budget
ACTIONS = {
    "move a group": lambda s, i, d: call(s, "move_group", i["groups"][1], i["groups"][0]),
    "move a category": lambda s, i, d: call(s, "move_category", i["categories"][1][0], i["categories"][1][1],
                                            i["categories"][0][0]),
    "hide a category": lambda s, i, d: call(s, "set_category_hidden", i["categories"][0][0], True),
    "rename a category": lambda s, i, d: call(s, "rename_category", i["categories"][0][0], "Renamed"),
    "a category's note": lambda s, i, d: call(s, "set_category_note", i["categories"][0][0], "made-up note"),
    "Budgeted": lambda s, i, d: call(s, "set_monthly_budget", i["categories"][0][0], 12345),
    "assign": lambda s, i, d: call(s, "set_assigned", MONTH, i["categories"][0][0], 99900),
    "assign, then undo it": lambda s, i, d: (call(s, "set_assigned", MONTH, i["categories"][0][0], 99900),
                                             call(s, "undo")),
    "Always ask": lambda s, i, d: call(s, "set_always_ask", i["payee"], True),
    "rename a payee": lambda s, i, d: call(s, "rename_payee", i["payee"], "Renamed Payee"),
    "No, different (is it the same payee?)": lambda s, i, d: call(s, "same_payee", i["similar"], False),
    "Yes, same": lambda s, i, d: call(s, "same_payee", i["similar"], True),
    "Overspending": lambda s, i, d: call(s, "save_settings", {"overspending": "carry_over"}),
    "Usual's months": lambda s, i, d: call(s, "save_settings", {"average_months": 6}),
    "budget start": lambda s, i, d: call(s, "set_budget_start", "2026-09"),
    "an account's rate and payment": lambda s, i, d: call(s, "update_account", i["accounts"][1],
                                                          {"apr": 19.99, "planned_payment": 5000}),
    "rename an account": lambda s, i, d: call(s, "update_account", i["accounts"][0], {"name": "Renamed account"}),
    "Check balance… with another amount": lambda s, i, d: call(s, "reconcile", i["accounts"][0],
                                                               balance_of(s, i["accounts"][0])["balance"] + 100),
    "Not recurring (a charge)": lambda s, i, d: call(s, "not_a_subscription", i["charge"]),
    "a price type chosen": lambda s, i, d: call(s, "update_subscription", call(s, "subscriptions")["subscriptions"][0]["id"],
                                                {"pricing": "varies"}),
    "Don't import a file": lambda s, i, d: call(s, "ignore_file", a_file_not_to_import(d)),
    "categorize": lambda s, i, d: call(s, "categorize", [i["waiting"][-1]], i["categories"][0][0]),
    "categorize One at a time": lambda s, i, d: call(s, "categorize", [i["waiting"][0]], i["categories"][0][0], False),
    "Accept all": lambda s, i, d: call(s, "accept_all", i["waiting"]),
    "send one back": lambda s, i, d: call(s, "update_transaction", i["charge"], {"category_id": None}),
    "a transaction's note": lambda s, i, d: call(s, "update_transaction", i["charge"], {"note": "made-up"}),
    # not the budget: this computer's own settings (settings.json), and how it shows the Budget page
    "Appearance": lambda s, i, d: call(s, "save_settings", {"theme": "dark"}),
    "the Dashboard's months": lambda s, i, d: call(s, "save_settings", {"dashboard_months": 12}),
    "the balance typed as it is": lambda s, i, d: call(s, "set_balance", i["accounts"][0],
                                                       balance_of(s, i["accounts"][0])["balance"],
                                                       balance_of(s, i["accounts"][0])["balance_date"]),
    "fold a group": lambda s, i, d: call(s, "set_group_collapsed", i["groups"][0], True),
}
NOT_THE_BUDGET = {"assign, then undo it", "Appearance", "the Dashboard's months", "the balance typed as it is",
                  "fold a group"}


@pytest.mark.parametrize("action", ACTIONS)
def test_a_change_of_yours_changes_the_budget_only_when_the_window_can_show_it(action, start, tmp_path, monkeypatch):
    path, ids = start
    monkeypatch.setenv("INAB_TODAY", TODAY)
    shutil.copyfile(path, tmp_path / "budget.db")
    service = open_service(tmp_path)
    before = backups.digest(service.db)
    ACTIONS[action](service, ids, tmp_path)
    side = changes.what_changed(path, service.db)
    changed = backups.digest(service.db) != before
    assert changed == side["changed"], side["summary"]  # changed exactly when the window can show it
    assert changed == (action not in NOT_THE_BUDGET)
    service.db.conn.close()
