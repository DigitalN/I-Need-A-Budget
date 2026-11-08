# SPDX-License-Identifier: GPL-3.0-only
"""The Transactions page's total and count cover every matching row, and a split counts only
its line in the category being looked at. Made-up data only."""

from pathlib import Path

from helpers import call

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def _warehouse(app):
    wallet = call(app, "add_account", "Wallet", "cash", 100000, "2026-08-31")
    call(app, "finish_onboarding", "2026-09")
    gid = call(app, "add_group", "Everyday")
    call(app, "add_category", gid, "Food")
    call(app, "add_category", gid, "Household")
    cats = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    t = call(app, "add_transaction", wallet, "2026-10-05", "SAMPLE WAREHOUSE", -10000)
    t = t["id"]
    call(app, "split_transaction", t, [{"category_id": cats["Food"], "amount": -3000},
                                       {"category_id": cats["Household"], "amount": -7000}])
    return wallet, cats, t


def test_the_transactions_total_counts_a_splits_line_only(app):
    """Issue #55: Food showed -$100.00 here while the Budget page's Spent for Food was -$30.00."""
    wallet, cats, t = _warehouse(app)
    food = call(app, "transactions", {"category_id": cats["Food"], "month": "2026-10"})
    assert food["total"] == -3000 and food["count"] == 1
    assert [(i["amount"], i["category_amount"]) for i in food["items"]] == [(-10000, -3000)]  # "−$30.00 of −$100.00"
    household = call(app, "transactions", {"category_id": cats["Household"]})
    assert household["total"] == -7000
    # Without a category, the whole transaction is what it is.
    everything = call(app, "transactions", {})
    assert everything["total"] == -10000 and "category_amount" not in everything["items"][0]
    # The Budget page agrees with what Food's list adds up to.
    spent = call(app, "category_detail", cats["Food"], "2026-10")["spent"]["total"]
    assert spent == food["total"]


def test_a_split_with_two_lines_in_one_category_counts_both(app):
    wallet, cats, t = _warehouse(app)
    call(app, "split_transaction", t, [{"category_id": cats["Food"], "amount": -3000},
                                       {"category_id": cats["Food"], "amount": -2000},
                                       {"category_id": cats["Household"], "amount": -5000}])
    assert call(app, "transactions", {"category_id": cats["Food"]})["total"] == -5000


def test_the_total_and_count_cover_every_row_not_just_those_shown(app):
    """Issue #55: with 1,101 rows the page showed "1,000 transactions" and a total missing the oldest."""
    wallet, cats, t = _warehouse(app)
    with app.db.write() as c:
        for i in range(1100):
            c.execute("INSERT INTO transactions (account_id, date, amount, raw_name, status, reason, by_you) "
                      "VALUES (?, '2026-09-01', -100, 'SAMPLE ROW', 'done', 'Added by you', 1)", (wallet,))
    data = call(app, "transactions", {"limit": 1000})
    assert len(data["items"]) == 1000
    assert data["count"] == 1101 and data["total"] == -10000 - 1100 * 100
    assert call(app, "transactions", {})["count"] == 1101 and len(call(app, "transactions", {})["items"]) == 500
    assert call(app, "transactions", {"limit": 2000})["items"].__len__() == 1101
    assert call(app, "transactions", {"month": "2026-10"})["count"] == 1  # the filter narrows the count too


def test_the_page_says_when_it_shows_only_the_newest_and_offers_the_rest():
    shows = PAGE[PAGE.index("async function renderTransactions()"):]
    shows = shows[:shows.index("async function editTransaction")]
    assert "Showing the newest" in shows and "data-act=\"tx-more\"" in shows and "data.count" in shows
    assert "limit: state.txLimit" in shows and "category_amount" in shows
    assert "'tx-more'" in PAGE[PAGE.index("act === 'add-tx'"):][:3000] or "act === 'tx-more'" in PAGE
