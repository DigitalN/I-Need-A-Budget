# SPDX-License-Identifier: GPL-3.0-only
"""A change to something that's gone says so, changes nothing and has no Undo (issue #139).

Undo can take something away while a pop-over still shows it. A change aimed at it then said
"Saved", or its toast's Undo took back the step before it, or it said "Something went wrong".
Made-up data only."""

import pytest

import backups
from helpers import call, categories

GONE = 9999  # an id nothing has


def _group(app, name):
    return next(g["id"] for g in call(app, "categories") if g["name"] == name)


def _assigned(app, month, category_id):
    row = app.db.conn.execute("SELECT assigned FROM budget WHERE month = ? AND category_id = ?",
                              (month, category_id)).fetchone()
    return row["assigned"] if row else 0


def test_a_category_undone_behind_its_pop_over_says_so(app):
    """The issue's case: $50.00 assigned to Food, then Review Drawer added and undone while its
    pop-over was open. Budgeted and Notes said "Saved"; Hide and Delete… said they'd worked, and
    their toast's Undo took back the $50.00; Move money said "Something went wrong (IntegrityError)"."""
    ids = categories(app, {"Fun": ["Food"]})
    call(app, "set_assigned", "2026-10", ids["Food"], 5000)
    review = call(app, "add_category", _group(app, "Fun"), "Review Drawer")
    call(app, "undo")
    assert call(app, "undo_step")["label"] == "Assign money"
    for request, args in (("set_monthly_budget", [review, 2500]), ("set_category_note", [review, "Renews in May"]),
                          ("set_category_hidden", [review, True]), ("delete_category", [review]),
                          ("move_money", ["2026-10", review, ids["Food"], 1000]),
                          ("set_assigned", ["2026-10", review, 1000])):
        assert app.dispatch(request, args) == {"ok": False, "error": "That category no longer exists."}, request
    assert call(app, "undo_step")["label"] == "Assign money"
    assert _assigned(app, "2026-10", ids["Food"]) == 5000


@pytest.fixture
def budget(app):
    """A made-up budget with one of everything a change can be aimed at."""
    ids = categories(app, {"Fun": ["Food", "Gifts"]})
    acct = call(app, "add_account", "SAMPLE Chequing", "chequing", 100000, "2026-10-01")
    txn = call(app, "add_transaction", acct, "2026-10-05", "SAMPLE GYM CALGARY AB", -2500)["id"]
    payee = app.db.conn.execute("SELECT payee_id FROM transactions WHERE id = ?", (txn,)).fetchone()[0]
    sub = call(app, "add_subscription", "SAMPLE GYM CALGARY AB", "Gym", 2500)
    candidate = {"name": "SAMPLE GYM CALGARY AB", "payee_id": payee, "amount": 2500, "cadence": "monthly",
                 "account_id": acct, "transaction_ids": [txn]}
    return {**ids, "acct": acct, "txn": txn, "payee": payee, "sub": sub, "candidate": candidate}


CHANGES = [  # (request, its arguments given the made-up budget `b`, what's gone)
    ("set_monthly_budget", lambda b: [GONE, 2500], "category"),
    ("set_category_note", lambda b: [GONE, "Renews in May"], "category"),
    ("set_category_hidden", lambda b: [GONE, True], "category"),
    ("set_category_hidden", lambda b: [GONE, False], "category"),
    ("delete_category", lambda b: [GONE], "category"),
    ("rename_category", lambda b: [GONE, "Presents"], "category"),
    ("move_money", lambda b: ["2026-10", GONE, b["Food"], 1000], "category"),
    ("move_money", lambda b: ["2026-10", b["Food"], GONE, 1000], "category"),
    ("set_assigned", lambda b: ["2026-10", GONE, 1000], "category"),
    ("apply_plan", lambda b: ["2026-10", [{"category_id": b["Food"], "amount": 1000},
                                          {"category_id": GONE, "amount": 1000}]], "category"),
    ("categorize", lambda b: [[b["txn"]], GONE], "category"),
    ("track_subscription", lambda b: [b["candidate"], None, None, GONE], "category"),
    ("rename_group", lambda b: [GONE, "Fun stuff"], "group"),
    ("delete_group", lambda b: [GONE], "group"),
    ("set_group_collapsed", lambda b: [GONE, True], "group"),
    ("add_category", lambda b: [GONE, "Presents"], "group"),
    ("update_account", lambda b: [GONE, {"name": "SAMPLE Savings"}], "account"),
    ("update_account", lambda b: [GONE, {"closed": True}], "account"),
    ("delete_account", lambda b: [GONE], "account"),
    ("merge_accounts", lambda b: [GONE, b["acct"]], "account"),
    ("merge_accounts", lambda b: [b["acct"], GONE], "account"),
    ("set_balance", lambda b: [GONE, 100000], "account"),
    ("delete_transaction", lambda b: [GONE], "transaction"),
    ("update_transaction", lambda b: [GONE, {"note": "Lunch"}], "transaction"),
    ("not_a_subscription", lambda b: [GONE], "transaction"),
    ("same_payee", lambda b: [GONE, True], "transaction"),
    ("subscription_from_transaction", lambda b: [GONE, "Gym"], "transaction"),
    ("link_subscription", lambda b: [GONE, b["sub"]], "transaction"),
    ("link_subscription", lambda b: [b["txn"], GONE], "recurring charge"),
    ("update_subscription", lambda b: [GONE, {"name": "Gym"}], "recurring charge"),
    ("cancel_subscription", lambda b: [GONE], "recurring charge"),
    ("reactivate_subscription", lambda b: [GONE], "recurring charge"),
    ("delete_subscription", lambda b: [GONE], "recurring charge"),
    ("set_always_ask", lambda b: [GONE, True], "payee"),
    ("rename_payee", lambda b: [GONE, "SAMPLE GYM"], "payee"),
    ("add_subscription", lambda b: [GONE, "Gym", 2500], "payee"),
    ("track_subscription", lambda b: [{**b["candidate"], "payee_id": GONE}], "payee"),
    ("dismiss_subscription", lambda b: [{**b["candidate"], "payee_id": GONE}], "payee"),
    ("undo_import", lambda b: [GONE], "import"),
]


@pytest.mark.parametrize("request_name, args, what", CHANGES, ids=[f"{r}-{w}" for r, _, w in CHANGES])
def test_a_change_to_something_gone_says_so_and_changes_nothing(app, budget, request_name, args, what):
    before, step = backups.digest(app.db), call(app, "undo_step")
    assert app.dispatch(request_name, args(budget)) == {"ok": False, "error": f"That {what} no longer exists."}
    assert backups.digest(app.db) == before and call(app, "undo_step") == step
