# SPDX-License-Identifier: GPL-3.0-only
"""What the app writes on its own after a launch, as the budget folder sees it (issue #189, ADR 0008's rule 2):
another computer's save is taken without asking only when nothing changed here (backups.digest), so a launch, the
page's reads and the watchers' looks must leave the budget as it was. The everyday case it protects: computer B is
opened before A's last save has synced, and A's save lands a minute later.

Each computer is launched the way main.py does it (the database with its backups' tag, the service, the look at
launch, the once-only reads, the re-check), then the page's start and reads, then the watchers' looks. The budget is
scripts/demo_data.py's made-up one; every computer has its own empty Downloads folder. Throwaway folders only.

What the launch's re-check does change, found on 2026-11-27: a stored inbox reason gone stale during a session
(only text: Inbox.view works it out again, and backups.digest leaves transactions.reason out, so it's no change), and
what "One at a time" leaves for the next re-check to categorize (the maintainer's design, 2026-11-09: an accepted
cost in ADR 0008, it asks once). A group folded is no change either (category_groups.collapsed)."""

import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import backups
import budget_folder
from budget_folder import FILE_NAME, read_save
from db import Database
from helpers import call
from service import BudgetService
from settings import Settings

REPO = Path(__file__).resolve().parents[2]
TODAY = "2026-11-20"
# The page at its start and as it's clicked through: every read it makes, none of which may write.
READS = ("bootstrap", "budget_folder", "categories", "dashboard", "counts", "inbox", "transactions", "accounts",
         "subscriptions", "debt", "get_settings", "app_info", "onboarding_state", "imports", "payees", "scan_downloads",
         "bank_names")


def made_up_budget(data):
    """scripts/demo_data.py's budget in `data`, as that script builds it (one of its inbox reasons stale)."""
    subprocess.run([sys.executable, str(REPO / "scripts" / "demo_data.py"), str(data), "--today", TODAY],
                   check=True, capture_output=True)


def launch(data, name):
    """main.py's launch: the database (named for this computer's backups once there's a budget folder), the service,
    the look at launch, the once-only reads, then the re-check the window's start runs."""
    settings = Settings(data / "settings.json")
    if not settings.get()["downloads_dir"]:  # never the real Downloads: the watcher would import from it
        (data / "downloads").mkdir(parents=True, exist_ok=True)
        settings.update({"downloads_dir": str(data / "downloads")})
    folder = settings.get()["budget_folder"]
    db = Database(data / "budget.db", backup_tag=budget_folder.tag_in(data, name) if folder else None)
    service = BudgetService(db, settings, data, computer_name=name)
    service.budget_folder_look(at_launch=True)
    service.read_imported_files()
    service.read_currency()
    call(service, "recheck")
    return service


def the_page_and_the_watchers(service, looks=3):
    """The page's start and reads (a month, a category's pop-over, its Spent list, Assign money…), then the budget
    folder's and the Downloads watcher's looks. Returns the looks' states."""
    month = call(service, "bootstrap")["current_month"]
    category = next(c["id"] for g in call(service, "categories") if g["role"] == "normal" for c in g["categories"])
    for request in READS:
        call(service, request)
    call(service, "month", month)
    call(service, "category_detail", category)
    call(service, "spent_detail", month, category)
    call(service, "assign_plan", month)
    states = []
    for _ in range(looks):
        states.append(service.budget_folder_look())
        service.watch_downloads()
    return states


def quit_app(service):
    service.budget_folder_at_quit()
    service.db.conn.close()


def as_saved(service, folder):
    """Whether this computer's budget is the one in the folder, as saved (nothing changed here since)."""
    return backups.digest(service.db) == read_save(folder / FILE_NAME).digest


def settled(tmp_path, monkeypatch):
    """PC-A's made-up budget moved into a budget folder, launched once (its re-check rewrites the one stale reason
    the demo build leaves, and that's saved at quit), so what follows starts from a budget the app has settled."""
    monkeypatch.setenv("INAB_TODAY", TODAY)
    a, folder = tmp_path / "PC-A", tmp_path / "Budget"
    made_up_budget(a)
    service = launch(a, "PC-A")
    call(service, "move_budget_folder", str(folder))
    quit_app(service)
    quit_app(launch(a, "PC-A"))
    return a, folder


def opened_on(tmp_path, folder, name):
    """A new computer opening the budget from its folder (onboarding's Open my budget), then quitting."""
    data = tmp_path / name
    (data / "downloads").mkdir(parents=True)
    settings = Settings(data / "settings.json")
    settings.update({"downloads_dir": str(data / "downloads")})
    service = BudgetService(Database(data / "budget.db"), settings, data, computer_name=name)
    call(service, "open_budget_folder", str(folder))
    quit_app(service)
    return data


def assign(service, cents):
    month = call(service, "bootstrap")["current_month"]
    category = next(c["id"] for g in call(service, "categories") if g["role"] == "normal" for c in g["categories"])
    call(service, "set_assigned", month, category, cents)
    return month, category


def assigned(service, month, category):
    return next(c["assigned"] for g in call(service, "month", month)["groups"] for c in g["categories"]
                if c["id"] == category)


@pytest.mark.parametrize("today", [TODAY, "2026-11-21", "2026-12-01"])
def test_a_launch_the_page_and_the_watchers_change_nothing(today, tmp_path, monkeypatch):
    """The same day, a new day, a new month: the launch (re-check, once-only reads, settings moved into the budget),
    the page's reads (Inbox, Dashboard, a month, a pop-over, Assign money…) and the watchers' looks write nothing."""
    a, folder = settled(tmp_path, monkeypatch)
    monkeypatch.setenv("INAB_TODAY", today)
    service = launch(a, "PC-A")
    states = the_page_and_the_watchers(service)
    assert [(s["state"], s["unsaved"], "took" in s) for s in states] == [("saved", False, False)] * 3
    assert as_saved(service, folder)
    quit_app(service)


def test_a_save_delivered_after_another_computer_launched_is_taken_without_asking(tmp_path, monkeypatch):
    """B is opened on A's save; A, still open, saves a change a minute later: B's next look takes it, unasked."""
    a, folder = settled(tmp_path, monkeypatch)
    b = opened_on(tmp_path, folder, "PC-B")
    on_a = launch(a, "PC-A")
    on_b = launch(b, "PC-B")
    assert [s["state"] for s in the_page_and_the_watchers(on_b)] == ["saved"] * 3 and as_saved(on_b, folder)

    month, category = assign(on_a, 54321)
    assert on_a.budget_folder_look(now=True)["state"] == "saved"
    state = on_b.budget_folder_look()
    assert state["state"] == "saved" and state["took"]["computer"] == "PC-A"
    assert assigned(on_b, month, category) == 54321 and call(on_b, "budget_folder_question") is None
    assert on_a.budget_folder_look()["state"] == "saved"  # and A has nothing to ask either
    quit_app(on_a)
    quit_app(on_b)


def still_open(data, name):
    """A computer whose app has been open a while: its session goes on (no launch, so no launch re-check)."""
    settings = Settings(data / "settings.json")
    return BudgetService(Database(data / "budget.db"), settings, data, computer_name=name)


def test_a_group_folded_here_doesnt_stop_another_computers_save_being_taken(tmp_path, monkeypatch):
    """Folding a group is how this computer shows the Budget page, not a change to the budget (backups.digest leaves
    category_groups.collapsed out): B folds one, nothing is saved for it, and A's change saved a minute later is
    taken unasked."""
    a, folder = settled(tmp_path, monkeypatch)
    on_a = launch(a, "PC-A")
    on_b = launch(opened_on(tmp_path, folder, "PC-B"), "PC-B")
    the_page_and_the_watchers(on_b)
    group = next(g["id"] for g in call(on_b, "categories") if g["role"] == "normal")
    call(on_b, "set_group_collapsed", group, True)
    state = on_b.budget_folder_look()
    assert state["state"] == "saved" and not state["unsaved"] and as_saved(on_b, folder)  # nothing to save
    month, category = assign(on_a, 54321)
    assert on_a.budget_folder_look(now=True)["state"] == "saved"
    state = on_b.budget_folder_look()
    assert state["state"] == "saved" and state["took"]["computer"] == "PC-A"
    assert assigned(on_b, month, category) == 54321 and call(on_b, "budget_folder_question") is None
    quit_app(on_a)
    quit_app(on_b)


def b_opened_then_a_saves_a_minute_later(tmp_path, folder, on_a):
    """A has just saved (its session goes on); B is opened on that save and launched; a minute later A saves a
    change. Returns what B's next look finds, whether B has A's change, and B."""
    assert on_a.budget_folder_look(now=True)["state"] == "saved"
    on_b = launch(opened_on(tmp_path, folder, "PC-B"), "PC-B")
    the_page_and_the_watchers(on_b)
    month, category = assign(on_a, 54321)
    on_a.budget_folder_look(now=True)
    state = on_b.budget_folder_look()
    return state, assigned(on_b, month, category) == 54321, on_b


def test_a_reason_gone_stale_in_a_session_is_no_change_at_the_next_launch(tmp_path, monkeypatch):
    """The demo build is such a session: an e-transfer in waits as "a 1/2 share of The Keg Steakhouse… Put it in that
    one's category", written before The Keg was categorized; B's launch re-check rewrites it ("Back to Food?"). Only
    text (backups.NOT_A_CHANGE_COLUMNS), so A's save is still taken."""
    monkeypatch.setenv("INAB_TODAY", TODAY)
    a, folder = tmp_path / "PC-A", tmp_path / "Budget"
    made_up_budget(a)
    on_a = still_open(a, "PC-A")
    call(on_a, "move_budget_folder", str(folder))
    state, has_it, _ = b_opened_then_a_saves_a_minute_later(tmp_path, folder, on_a)
    assert state["state"] == "saved" and has_it  # taken, unasked


def test_what_one_at_a_time_left_asks_once_when_the_other_computers_save_arrives_later(tmp_path, monkeypatch):
    """ADR 0008's accepted cost: a computer opened before the other's last save has synced, whose launch re-check
    categorizes what One at a time left (the maintainer's design, 2026-11-09: they're still the app's), asks once.
    In A's session two charges from a new payee arrive and one is categorized One at a time; the other waits with
    that category as its guess. B's launch re-check categorizes it: a change of B's own, which B's side of the
    question lists as the app's."""
    a, folder = settled(tmp_path, monkeypatch)
    spec = importlib.util.spec_from_file_location("demo_data", REPO / "scripts" / "demo_data.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    on_a = launch(a, "PC-A")
    chequing = next(x for x in call(on_a, "accounts") if x["kind"] == "chequing")
    qfx = a / "downloads" / "Chequing more.qfx"
    qfx.write_text(demo.ofx([(date(2026, 11, 19), -4000, "SAMPLE HARDWARE CALGARY AB", "X1"),
                             (date(2026, 11, 20), -2500, "SAMPLE HARDWARE CALGARY AB", "X2")], False, "000123456789",
                            chequing["balance"] - 6500, date(2026, 11, 20)), encoding="utf-8")
    call(on_a, "import_files", [str(qfx)])
    waiting = [i["id"] for i in call(on_a, "inbox")["items"] if "HARDWARE" in str(i).upper()]
    assert len(waiting) == 2
    household = next(c["id"] for g in call(on_a, "categories") for c in g["categories"] if c["name"] == "Household")
    call(on_a, "categorize", [waiting[0]], household, False)  # One at a time
    state, has_it, on_b = b_opened_then_a_saves_a_minute_later(tmp_path, folder, on_a)
    assert state["state"] == "two_computers" and not has_it  # asked, nothing taken yet
    question = call(on_b, "budget_folder_question")
    assert question["can_show_changes"] and question["this"]["summary"] == "1 categorized"
    [item] = [i for section in question["this"]["sections"] for i in section["items"]]
    assert item["app"] and "SAMPLE HARDWARE" in item["text"].upper()  # the app's, and it says so
    assert "Assigned" in question["theirs"]["summary"]  # A's change
