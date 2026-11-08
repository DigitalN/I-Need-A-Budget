# SPDX-License-Identifier: GPL-3.0-only
"""The reasons a category can't be used reach the page as they are written, not as "Something went
wrong" (issue #118). Made-up data only."""

import re
from pathlib import Path

from helpers import call, categories

APP = Path(__file__).resolve().parents[2] / "Application Files"


def _setup(app):
    ids = categories(app, {"Everyday": ["Food", "Fun Money"]})
    card = call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    call(app, "add_account", "Chequing", "chequing", 100000, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    charge = call(app, "add_transaction", card, "2026-10-05", "SAMPLE ARCADE", -4000)["id"]
    return ids, card, charge


def test_categorizing_into_a_hidden_category_says_it_is_hidden(app):
    ids, _, charge = _setup(app)
    call(app, "set_category_hidden", ids["Fun Money"], True)
    result = app.dispatch("categorize", [[charge], ids["Fun Money"]])
    assert result == {"ok": False, "error": "Fun Money is hidden: show it again, or pick another category."}


def test_a_payment_category_or_one_that_is_gone_says_so_too(app):
    import ledger
    ids, card, charge = _setup(app)
    payment = ledger.payment_categories(app.db.conn)[card]
    result = app.dispatch("categorize", [[charge], payment])
    assert result["error"] == ("Sample Visa payment is where the card's payments are set aside: "
                               "pick a category for what was bought.")
    assert app.dispatch("categorize", [[charge], 99999]) == {"ok": False, "error": "That category no longer exists."}
    parts = [{"category_id": ids["Food"], "amount": -1000}, {"category_id": payment, "amount": -3000}]
    assert "card's payments" in app.dispatch("split_transaction", [charge, parts])["error"]
    for result in (app.dispatch("categorize", [[charge], None]), app.dispatch("split_transaction", [charge, [
            {"category_id": ids["Food"], "amount": -1000}, {"category_id": None, "amount": -3000}]])):
        assert not result["ok"] and "Something went wrong" not in result["error"]


def test_no_message_for_the_person_is_raised_as_a_value_error():
    """dispatch() shows only UserError (and the import errors) as written; any other exception is a
    bug, shown as "Something went wrong". So a message written for the person is never a ValueError."""
    found = []
    for name in ("service.py", "ledger.py", "inbox.py", "subscriptions.py", "transfers.py", "learn.py"):
        for number, line in enumerate((APP / name).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"raise ValueError\(", line) and "Unknown account type" not in line:  # a bug, not a message
                found.append(f"{name}:{number}: {line.strip()}")
    assert found == []
