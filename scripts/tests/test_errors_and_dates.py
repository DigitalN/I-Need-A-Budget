# SPDX-License-Identifier: GPL-3.0-only
"""What reaches the page as an error: messages for the person, and nothing raw from Python; dates and
months from the page are checked. Made-up data only."""

import logging

import pytest

import errors
import service
from helpers import call, categories
from test_inbox import Budget


def _setup(app):
    ids = categories(app, {"Everyday": ["Food"]})
    wallet = call(app, "add_account", "Wallet", "cash", 10000, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    return ids, wallet


def test_user_error_lives_in_a_shared_module():
    assert service.UserError is errors.UserError


def test_a_bad_month_is_a_plain_message(app, caplog):
    """Issue #72: category_detail(id, "2026-9x") showed "invalid literal for int() with base 10: '9x'"."""
    ids, _ = _setup(app)
    for request, args in (("category_detail", [ids["Food"], "2026-9x"]), ("month", ["2026-13"]),
                          ("assign_plan", ["soon"]), ("set_assigned", ["2026-00", ids["Food"], 100]),
                          ("move_money", ["2026-1", ids["Food"], None, 100]), ("catch_up", ["last month"]),
                          ("spent_detail", ["2026-9x", ids["Food"]]), ("set_budget_start", ["2026-13"])):
        result = app.dispatch(request, args)
        assert not result["ok"] and "month" in result["error"] and "invalid literal" not in result["error"], request
    assert app.dispatch("month", ["2026-10"])["ok"]


def test_a_bad_date_is_refused_before_it_is_stored(app):
    _, wallet = _setup(app)
    for date in ("2026-13-01", "2026-02-30", "yesterday", "10/05/2026", ""):
        result = app.dispatch("add_transaction", [wallet, date, "Sample", -500])
        assert not result["ok"] and ("date" in result["error"]), date
    assert not app.dispatch("set_balance", [wallet, 5000, "2026-13-45"])["ok"]
    txn = call(app, "add_transaction", wallet, "2026-10-05", "Sample", -500)
    txn = txn["id"]
    assert not app.dispatch("update_transaction", [txn, {"date": "2026-99-99"}])["ok"]
    assert call(app, "update_transaction", txn, {"date": "2026-10-06"}) is not None
    with app.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM transactions WHERE date NOT LIKE '____-__-__'").fetchone()[0] == 0
        assert c.execute("SELECT date FROM transactions WHERE id = ?", (txn,)).fetchone()[0] == "2026-10-06"


def test_something_unexpected_is_logged_and_shown_without_raw_python(app, caplog, monkeypatch):
    _setup(app)

    def boom(self, *args, **kwargs):
        raise ValueError("invalid literal for int() with base 10: '9x'")

    monkeypatch.setitem(service.EXPOSED, "payees", boom)  # put back after the test
    with caplog.at_level(logging.ERROR, logger="inab"):
        result = app.dispatch("payees", [])
    assert not result["ok"] and "invalid literal" not in result["error"]
    assert result["error"].startswith("Something went wrong (ValueError)") and "log" in result["error"]
    assert any("payees failed" in r.getMessage() for r in caplog.records)


def test_messages_for_the_person_still_reach_the_page_unchanged(app):
    ids, wallet = _setup(app)
    txn = call(app, "add_transaction", wallet, "2026-10-05", "Sample", -500)
    txn = txn["id"]
    for request, args, message in (
            ("split_transaction", [txn, [{"category_id": ids["Food"], "amount": -500}]], "A split needs at least two parts."),
            ("split_transaction", [txn, [{"category_id": ids["Food"], "amount": -100}, {"category_id": ids["Food"], "amount": -100}]],
             "The parts have to add up to the transaction's amount."),
            ("categorize", [[txn], None], "Pick a category first."),
            ("merge_accounts", [wallet, wallet], "Pick another account to merge into.")):
        result = app.dispatch(request, args)
        assert result == {"ok": False, "error": message}, (request, result)
    card = call(app, "add_account", "Sample Visa", "credit_card")
    assert app.dispatch("merge_accounts", [card, wallet])["error"] == "A card or line of credit can only be merged with another one."
    assert app.dispatch("update_transaction", [99999, {"note": "x"}])["error"] == "That transaction no longer exists."


def test_user_messages_are_raised_as_user_errors_in_the_inbox_and_ledger(tmp_path):
    b = Budget(tmp_path / "budget.db")
    with pytest.raises(errors.UserError, match="Pick a category first"):
        b.inbox.categorize([1], None)
    with pytest.raises(errors.UserError, match="no longer exists"):
        b.inbox.split(9999, [])
    with pytest.raises(errors.UserError, match="Pick another account"):
        import ledger
        ledger.merge_accounts(b.c, b.chequing, b.chequing)
