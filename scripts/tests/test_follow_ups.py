# SPDX-License-Identifier: GPL-3.0-only
"""Small follow-ups from reviewing the review fixes (issue #121). Made-up data only."""

import re
from pathlib import Path

import figures
from helpers import call, write
from test_account_matching import ofx

APP = Path(__file__).resolve().parents[2] / "Application Files"
PAGE = (APP / "index.html").read_text(encoding="utf-8")


def test_use_is_offered_only_for_an_amount_above_zero():
    """A category whose refunds outweigh its spending averages below zero: Use would save a negative Budgeted."""
    row = PAGE[PAGE.index("const useRow = "):PAGE.index("const rec = d.recurring")]
    assert re.search(r"\$\{amount > 0 \? `<button[^`]*data-act=\"use-amount\"[^`]*>Use</button>` : ''\}", row)


def test_a_savings_account_in_a_file_with_several_statements_isnt_named_chequing(app):
    """Issue #103: with no file name to go by, Tangerine's accounts (all labelled SAVINGS) were made as Chequing."""
    statements = [{"acct_id": "000111222333", "acct": "SAVINGS", "rows": [("20261001", "-5.00", "SAMPLE CAFE", "A1")]},
                  {"acct_id": "000999888777", "acct": "SAVINGS", "rows": [("20261002", "50.00", "SAMPLE DEPOSIT", "B1")]}]
    path = write(app.test_downloads, "Statements.qfx", ofx("Tangerine", statements))
    call(app, "import_files", [str(path)])
    names = sorted(a["name"] for a in call(app, "accounts"))
    assert len(names) == 2 and not any("Chequing" in n or "Savings" in n for n in names), names


def test_such_a_file_asks_which_account_it_is_like_an_unseen_account(app):
    chequing = write(app.test_downloads, "Chequing.QFX", ofx("Tangerine", [
        {"acct_id": "000111222333", "acct": "SAVINGS", "rows": [("20260901", "-5.00", "SAMPLE CAFE", "A1")]}]))
    call(app, "import_files", [str(chequing)])
    app.db.set_meta("onboarded", "1")
    both = write(app.test_downloads, "Statements.qfx", ofx("Tangerine", [
        {"acct_id": "000555666777", "acct": "SAVINGS", "rows": [("20261001", "-5.00", "SAMPLE CAFE", "B1")]},
        {"acct_id": "000999888444", "acct": "SAVINGS", "rows": [("20261002", "50.00", "SAMPLE DEPOSIT", "C1")]}]))
    out = call(app, "import_files", [str(both)])
    assert out["results"] == [] and [f["needs"] for f in out["needs_setup"]] == ["new_account"]


def test_a_splits_amount_in_one_category_is_figures_job():
    splits = [{"category_id": 1, "amount": -3000}, {"category_id": 2, "amount": -7000}, {"category_id": 1, "amount": -500}]
    assert figures.category_amount(splits, 1) == -3500
    assert figures.category_amount(splits, "2") == -7000
    assert figures.category_amount(splits, 3) == 0


def test_what_a_list_of_transactions_adds_up_to_is_figures_job(app):
    wallet = call(app, "add_account", "Wallet", "cash", 10000, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    ids = [call(app, "add_transaction", wallet, f"2026-10-0{n}", "Sample Shop", -1000 * n)["id"] for n in (1, 2, 3)]
    with app.db.read() as c:
        assert figures.transactions_total(c, ids) == -6000
        assert figures.transactions_total(c, ids[:2]) == -3000
        assert figures.transactions_total(c, []) == 0


def test_service_adds_no_money_up_itself():
    """CLAUDE.md: every money figure comes from figures.py / budget.py."""
    source = (APP / "service.py").read_text(encoding="utf-8")
    assert "SUM(amount)" not in source and 'x["amount"] for x in d["splits"]' not in source
