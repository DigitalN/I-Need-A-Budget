# SPDX-License-Identifier: GPL-3.0-only
"""⌘Z after an import, big steps that ask first, and the page's side of it (issue #42).
Made-up data only."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import CARD_FILE, bank_csv, call, card_csv, categories, txns, write

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")

CAFE = ("SAMPLE CAFE CALGARY AB", "SAMPLE CAFE CALGARY AB")


def _import(app, name, rows):
    return call(app, "import_files", [str(write(app.test_downloads, name, bank_csv(rows)))])


def _nothing_to_undo(app):
    out = app.dispatch("undo", [])
    return not out["ok"] and out["error"] == "Nothing to undo."


# ── an import clears ⌘Z: the five ways it went wrong ─────────────────────────

def test_undo_never_deletes_what_an_import_added(app):
    """Scenario 1: Add account, a file is imported into it, then ⌘Z deleted the account and both rows."""
    app.db.set_meta("onboarded", "1")
    app.db.set_meta("budget_start", "2026-08")
    acct = call(app, "add_account", "SAMPLE Chequing", "chequing", 100000, "2026-08-01")
    path = write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("09/02/2026", *CAFE, "-12.34"), ("09/03/2026", "SAMPLE BOOKS CALGARY AB", "SAMPLE BOOKS CALGARY AB", "-23.45")]))
    call(app, "import_files", [str(path)])
    call(app, "set_file_accounts", str(path), [{"index": 0, "account_id": acct}])
    assert _nothing_to_undo(app)
    assert app.db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    assert [a["name"] for a in call(app, "accounts")] == ["SAMPLE Chequing"]


def test_what_an_import_categorizes_stays_categorized(app):
    """Scenario 2: Add category, an import categorizes two new charges into it, then ⌘Z twice left them
    out of the inbox in no category."""
    _import(app, "Chequing.CSV", [("09/02/2026", *CAFE, "-4.50")])
    ids = categories(app, {"Fun": ["Cafes"]})
    call(app, "categorize", txn_ids=[txns(app)[0]["id"]], category_id=ids["Cafes"], recheck_payee=True)
    _import(app, "Chequing-2.CSV", [("09/09/2026", *CAFE, "-5.25"), ("09/16/2026", *CAFE, "-6.00")])
    assert _nothing_to_undo(app) and _nothing_to_undo(app)
    rows = txns(app)
    assert len(rows) == 3 and {(t["category_name"], t["status"]) for t in rows} == {("Cafes", "done")}


def test_an_import_that_reuses_a_deleted_rows_id_never_sticks_undo(app):
    """Scenario 3: Delete transaction, an import reuses its id, then every ⌘Z failed with IntegrityError."""
    _import(app, "Chequing.CSV", [("09/02/2026", *CAFE, "-4.50"), ("09/03/2026", *CAFE, "-5.50")])
    newest = max(t["id"] for t in txns(app))
    call(app, "delete_transaction", newest)
    _import(app, "Chequing-2.CSV", [("09/10/2026", "SAMPLE BOOKS CALGARY AB", "SAMPLE BOOKS CALGARY AB", "-9.99")])
    assert newest in {t["id"] for t in txns(app)}  # the id came back for the new row
    assert _nothing_to_undo(app)
    assert sorted(t["amount"] for t in txns(app)) == [-999, -450]


def test_remove_import_then_importing_it_again_clears_undo(app):
    """Scenario 4: Remove import, the same file again, ⌘Z: IntegrityError every time."""
    rows = [("09/02/2026", *CAFE, "-4.50"), ("09/03/2026", *CAFE, "-5.50")]
    _import(app, "Chequing.CSV", rows)
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert call(app, "undo_step")["label"] == "Remove import"
    _import(app, "Chequing.CSV", rows)
    assert _nothing_to_undo(app)
    assert len(txns(app)) == 2


def test_a_transfer_the_cards_file_paired_stays_paired(app):
    """Scenario 5: a payment added by hand and marked as a transfer, the card's file pairs it, ⌘Z: the
    payment went back to the inbox still paired."""
    _import(app, "Chequing.CSV", [("09/02/2026", *CAFE, "-4.50")])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("09/01/2026", "SAMPLE ARCADE", "-20.00", "Entertainment")])))])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    added = call(app, "add_transaction", accounts["Chequing"], "2026-09-05", "Card payment", -30000)["id"]
    call(app, "update_transaction", added, {"transfer_account_id": accounts["Tangerine Mastercard"]})
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE.replace(".CSV", "-2.CSV"),
                                         card_csv([("09/06/2026", "PAYMENT - THANK YOU", "300.00", "")])))])
    payment = next(t for t in txns(app) if t["id"] == added)
    assert payment["transfer_id"] and payment["status"] == "done"
    assert _nothing_to_undo(app)
    again = next(t for t in txns(app) if t["id"] == added)
    assert (again["transfer_id"], again["status"]) == (payment["transfer_id"], "done")


# ── only changes that matter clear it ────────────────────────────────────────

def test_folding_a_group_typing_a_note_and_ignoring_a_file_keep_undo(app):
    ids = categories(app, {"Fun": ["Cafes"]})
    group = call(app, "categories")[0]["id"]
    call(app, "set_group_collapsed", group, True)
    call(app, "set_category_note", ids["Cafes"], "the corner one")
    call(app, "ignore_file", str(write(app.test_downloads, "notes.csv", "a,b\n1,2\n")))
    assert call(app, "undo")["undone"] == "Add category Cafes"


def test_a_recheck_clears_undo_only_when_it_changes_something(app):
    _import(app, "Chequing.CSV", [("09/02/2026", *CAFE, "-4.50")])
    ids = categories(app, {"Fun": ["Cafes"]})
    call(app, "categorize", txn_ids=[txns(app)[0]["id"]], category_id=ids["Cafes"], recheck_payee=True)
    call(app, "recheck")  # nothing waiting: nothing changes
    assert call(app, "undo_step")["label"] == "Categorize transaction"
    with app.db.write(keep_undo=True) as c:  # one of the payee's waiting, as if the rules had changed since
        c.execute("INSERT INTO transactions (account_id, date, amount, payee_id, raw_name, status) "
                  "SELECT account_id, '2026-09-20', -650, payee_id, raw_name, 'review' FROM transactions LIMIT 1")
    assert call(app, "recheck") == 1  # it's categorized now
    assert _nothing_to_undo(app)


def test_finishing_onboarding_keeps_the_steps_before_it(app):
    call(app, "add_account", "Wallet", "cash", 5000, "2026-08-31")
    call(app, "finish_onboarding", "2026-09")
    assert call(app, "undo_step")["label"] == "Add account Wallet"


# ── big steps ask first ──────────────────────────────────────────────────────

def _step(app):
    step = call(app, "undo_step")
    return step["label"], step["big"], step["undoes"]


def test_big_steps_say_what_undoing_them_does(app):
    call(app, "add_account", "Savings", "savings", 100000, "2026-08-31")
    assert _step(app) == ("Add account Savings", True, "Savings is deleted.")
    savings = call(app, "accounts")[0]["id"]
    call(app, "delete_account", savings)
    assert _step(app) == ("Delete account", True, "Savings comes back.")
    gid = call(app, "add_group", "Travel", None, True)
    assert _step(app) == ("Add group Travel", True, "Travel and its category are deleted.")
    cid = call(app, "add_category", gid, "Flights")
    assert _step(app) == ("Add category Flights", True, "Flights is deleted.")
    call(app, "delete_category", cid)
    assert _step(app) == ("Delete category", True, "Flights comes back.")
    empty = call(app, "add_group", "Someday")
    call(app, "delete_group", empty)
    assert _step(app) == ("Delete group", True, "Someday comes back.")
    call(app, "finish_onboarding", "2026-09")
    call(app, "set_budget_start", "2026-08")
    assert _step(app) == ("Change budget start", True, "The budget starts in September 2026 again.")


def test_catching_up_past_months_is_big(app):
    wallet = call(app, "add_account", "Wallet", "cash", 50000, "2026-07-31")
    ids = categories(app, {"Fun": ["Cafes"]})
    call(app, "add_transaction", wallet, "2026-08-14", "Sample Cafe", -1250, ids["Cafes"])
    call(app, "finish_onboarding", "2026-08")
    call(app, "catch_up_all")
    assert _step(app) == ("Catch up past months", True,
                          "What's assigned from August 2026 to September 2026 goes back to what it was.")


def test_starter_categories_accept_all_and_remove_import_are_big(app):
    call(app, "use_starter_categories")
    assert _step(app) == ("Add starter categories", True, "The starter categories are deleted.")
    _import(app, "Chequing.CSV", [("09/02/2026", *CAFE, "-4.50"), ("09/03/2026", *CAFE, "-5.50")])
    ids = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    call(app, "categorize", txn_ids=[txns(app)[0]["id"]], category_id=ids["Eating Out"])  # One at a time
    waiting = [i["id"] for i in call(app, "inbox")["items"]]
    call(app, "accept_all", waiting)
    assert _step(app) == ("Accept all", True, "1 transaction goes back to the Inbox.")
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _step(app) == ("Remove import", True, "The 2 transactions from Chequing.CSV come back.")


def test_small_steps_undo_straight_away(app):
    ids = categories(app, {"Fun": ["Cafes", "Games"]})
    call(app, "set_assigned", "2026-10", ids["Cafes"], 5000)
    assert _step(app) == ("Assign money", False, "")
    call(app, "move_money", "2026-10", ids["Cafes"], ids["Games"], 1000)
    assert _step(app)[1] is False
    call(app, "rename_category", ids["Games"], "Board Games")
    assert _step(app)[1] is False
    call(app, "set_category_hidden", ids["Games"], True)
    assert _step(app)[1] is False


def test_a_toast_undoes_its_own_step_and_says_when_it_cant(app):
    """Every answer carries the step ⌘Z would undo after it: the toast's Undo asks for exactly that step."""
    ids = categories(app, {"Fun": ["Cafes"]})
    out = app.dispatch("set_assigned", ["2026-10", ids["Cafes"], 5000])
    assert out["ok"] and out["step"] == call(app, "undo_step")["step"]
    _import(app, "Chequing.CSV", [("09/02/2026", *CAFE, "-4.50")])  # a download arrives before Undo is clicked
    gone = app.dispatch("undo", {"step": out["step"]})
    assert gone == {"ok": False, "error": "That can't be undone any more."}
    assert app.dispatch("month", ["2026-10"])["step"] is None


# ── the page: ⌘Z asks for a big step, a toast's Undo doesn't ─────────────────

def _undo_script(body):
    parts = [re.search(rf"^{pattern}", PAGE, re.S | re.M).group(0) for pattern in (
        r"const undoAction = [^\n]*$", r"const undoStep = [^\n]*$", r"let undoing = false;$",
        r"async function doUndo\(.*?^}$")]
    fakes = """
const out = { calls: [], asked: [], toasts: [] };
let next = null, answer = false;
async function call(name, args) {
  out.calls.push(args === undefined ? [name] : [name, args]);
  if (name === 'undo_step') return next;
  if (name === 'undo') return { undone: 'It' };
}
async function confirmBox(title, text, label) { out.asked.push([title, text, label]); return answer; }
function toast(message) { out.toasts.push(message); }
async function refresh() {}
"""
    return fakes + "\n".join(parts) + "\n(async () => {\n" + body + "\nconsole.log(JSON.stringify(out));\n})();"


def _run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node")
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)


def test_command_z_on_a_big_step_asks_and_cancel_changes_nothing():
    got = _run(_undo_script("""
next = { step: 5, label: 'Add account Savings', big: true, undoes: 'Savings is deleted.' };
answer = false;
await doUndo();
"""))
    assert got["asked"] == [["Undo Add account Savings?", "Savings is deleted.", "Undo"]]
    assert got["calls"] == [["undo_step"]]  # Cancel: nothing undone


def test_command_z_on_a_big_step_undoes_exactly_that_step_once_asked():
    got = _run(_undo_script("""
next = { step: 5, label: 'Add account Savings', big: true, undoes: 'Savings is deleted.' };
answer = true;
await doUndo();
"""))
    assert got["calls"] == [["undo_step"], ["undo", {"step": 5}]]
    assert got["toasts"] == ["Undone: It"]


def test_command_z_on_a_small_step_undoes_without_asking():
    got = _run(_undo_script("""
next = { step: 6, label: 'Assign money', big: false, undoes: '' };
await doUndo();
"""))
    assert got["asked"] == [] and got["calls"] == [["undo_step"], ["undo", {"step": 6}]]


def test_a_toasts_undo_doesnt_ask():
    got = _run(_undo_script("""
next = { step: 7, label: 'Accept all', big: true, undoes: '3 transactions go back to the Inbox.' };
await undoStep(7)();
"""))
    assert got["asked"] == [] and got["calls"] == [["undo", {"step": 7}]]


def test_the_page_binds_undo_to_its_step_and_command_z_to_asking():
    assert "const run = a === undoAction ? undoStep(lastStep) : a.run;" in PAGE  # a toast's Undo: its step
    assert PAGE.count("if (r && r.ok && 'step' in r) lastStep = r.step;") == 2  # call() and tryCall()
    keys = PAGE[PAGE.index("document.addEventListener('keydown'"):]
    keys = keys[:keys.index("inboxKeys(e);")]
    assert "if (!$('#dlg').open) doUndo();" in keys and "e.preventDefault();" in keys
