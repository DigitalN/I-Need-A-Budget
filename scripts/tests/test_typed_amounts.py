# SPDX-License-Identifier: GPL-3.0-only
"""Amounts and rates that aren't numbers are refused, not cleared or misread. Made-up data only."""

from pathlib import Path

import pytest

from helpers import call

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def _card(app):
    card = call(app, "add_account", "Sample Visa", "credit_card", 50000, "2026-09-30")
    call(app, "update_account", card, {"apr": 20.99, "planned_payment": 15000})
    return card


def _debt(app, card):
    with app.db.read() as c:
        return tuple(c.execute("SELECT apr, planned_payment FROM accounts WHERE id = ?", (card,)).fetchone())


def test_update_account_refuses_a_rate_or_payment_that_isnt_a_number_instead_of_clearing_it(app):
    """Issue #69: Number('20,99') is NaN, which arrived as null and cleared the APR, and the page said "Saved"."""
    card = _card(app)
    for bad in ("20,99", "abc", "1e999", float("inf"), float("nan"), -5, 1000):
        result = app.dispatch("update_account", [card, {"apr": bad}])
        assert not result["ok"] and "rate" in result["error"], bad
    for bad in ("150,00", "abc", float("nan"), 10 ** 15):
        result = app.dispatch("update_account", [card, {"planned_payment": bad}])
        assert not result["ok"] and ("amount" in result["error"] or "more than" in result["error"]), bad
    assert _debt(app, card) == (20.99, 15000)  # nothing was cleared
    call(app, "update_account", card, {"apr": "19.5", "planned_payment": 20000})  # numbers as text are fine
    assert _debt(app, card) == (19.5, 20000)
    call(app, "update_account", card, {"apr": None, "planned_payment": ""})  # an empty box clears
    assert _debt(app, card) == (None, None)


def test_money_has_a_ceiling_and_must_be_a_whole_number_of_cents(app):
    card = call(app, "add_account", "Sample Visa", "credit_card")
    ceiling = 100_000_000_000  # $1,000,000,000.00
    for request, args in (("set_balance", [card, ceiling + 1]), ("set_balance", [card, float("nan")]),
                          ("reconcile", [card, 10 ** 17]), ("set_assigned", ["2026-10", 1, ceiling + 1]),
                          ("add_account", ["Other", "chequing", 10 ** 18]),
                          ("add_transaction", [card, "2026-10-05", "Sample", -(10 ** 16)])):
        result = app.dispatch(request, args)
        assert not result["ok"] and ("isn't an amount" in result["error"] or "more than" in result["error"]), (request, result)
    assert call(app, "set_balance", card, ceiling)  # the ceiling itself is allowed


def test_the_page_refuses_what_isnt_an_amount_before_asking_python():
    save = PAGE[PAGE.index("act === 'save-debt'"):]
    save = save[:save.index("act === 'add-account'")]
    assert "isn\\'t a rate" in save and "isn\\'t an amount" in save and "return" in save  # (an escaped quote in the page)
    run = PAGE[PAGE.index("const run = async a =>"):]
    run = run[:run.index("finish(a.value)")]
    assert "AMOUNT_BOXES" in run and "isn\\'t an amount" in run
