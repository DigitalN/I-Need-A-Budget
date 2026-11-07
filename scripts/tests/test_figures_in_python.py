"""Figures the page used to work out itself now come from Python (figures.py / subscriptions.py /
inbox.py): Assign money's Left over, a subscription's cost a year, the Dashboard's difference and
above-usual amounts, and the Inbox's group totals. Made-up data only."""

from pathlib import Path

import pytest

import subscriptions
from helpers import call, categories
from test_inbox import Budget

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def _cash_budget(app):
    """$100.00 of cash, a Food budget of $60.00 and Fun Money of $40.00, $150.00 assigned: Ready to assign is -$50.00."""
    ids = categories(app, {"Everyday": ["Food", "Fun Money"]})
    wallet = call(app, "add_account", "Wallet", "cash", 10000, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    call(app, "set_monthly_budget", ids["Food"], 6000)
    call(app, "set_monthly_budget", ids["Fun Money"], 4000)
    call(app, "set_assigned", "2026-10", ids["Food"], 15000)
    return ids, wallet


def test_assign_moneys_left_over_starts_where_python_starts_even_below_zero(app):
    """Issue #71: with Ready to assign at -$50.00 the window opened at $0.00 and showed -$50.00 after one keystroke."""
    ids, _ = _cash_budget(app)
    plan = call(app, "assign_plan", "2026-10")
    assert plan["ready_to_assign"] == -5000 and plan["left_over"] == 0
    assert call(app, "assign_left", "2026-10", plan["amount"], [])["left_over"] == 0
    typed = [{"category_id": ids["Fun Money"], "amount": 2000}]
    assert call(app, "assign_left", "2026-10", plan["amount"], typed)["left_over"] == -2000
    assert call(app, "assign_left", "2026-10", 10000, typed)["left_over"] == 8000
    # Taking money from a category adds to what's left, as in the plan.
    assert call(app, "assign_left", "2026-10", 10000, [{"category_id": ids["Fun Money"], "amount": -500}])["left_over"] == 10500
    assert not app.dispatch("assign_left", ["2026-10", "abc", []])["ok"]


def test_assign_left_agrees_with_the_plan_for_what_the_plan_gives(app):
    ids, _ = _cash_budget(app)
    call(app, "set_assigned", "2026-10", ids["Food"], 1000)  # now there is money to share out
    plan = call(app, "assign_plan", "2026-10")
    allocations = [{"category_id": p["category_id"], "amount": p["amount"]} for p in plan["plan"]]
    assert call(app, "assign_left", "2026-10", plan["amount"], allocations)["left_over"] == plan["left_over"]


def test_a_subscriptions_cost_a_year_isnt_a_rounded_month_times_twelve(tmp_path):
    b = Budget(tmp_path / "budget.db")
    for text, name, price, cadence in (("SAMPLE ANNUAL TORONTO ON", "Annual plan", 9999, "yearly"),
                                       ("SAMPLE WEEKLY TORONTO ON", "Weekly box", 500, "weekly"),
                                       ("SAMPLE MONTHLY TORONTO ON", "Monthly app", 1000, "monthly")):
        t = b.arrive(b.card, "2026-09-04", -price, text)
        sub = subscriptions.create(b.c, b.get(t)["payee_id"], name, price, cadence, category_id=b.cat["Subscriptions"])
        b.c.execute("UPDATE transactions SET subscription_id = ?, status = 'done', category_id = ? WHERE id = ?",
                    (sub, b.cat["Subscriptions"], t))
    listing = subscriptions.listing(b.c, "2026-10-05")
    by_name = {s["name"]: s for s in listing["subscriptions"]}
    assert by_name["Annual plan"]["yearly"] == 9999 and by_name["Weekly box"]["yearly"] == 26000
    assert by_name["Monthly app"]["yearly"] == 12000
    assert listing["yearly_subscriptions"] == 9999 + 26000 + 12000  # not 12 × the rounded monthly figures
    assert listing["yearly_bills"] == 0
    assert "monthly_subscriptions * 12" not in PAGE and "s.yearly_subscriptions" in PAGE


def test_the_dashboard_gives_the_difference_and_the_above_usual_amount(app):
    ids = categories(app, {"Everyday": ["Food"], "Income group": []})
    wallet = call(app, "add_account", "Wallet", "cash", 100000, "2026-02-28")
    call(app, "finish_onboarding", "2026-03")
    for month in ("2026-03", "2026-04", "2026-05", "2026-06"):
        call(app, "add_transaction", wallet, f"{month}-10", "Sample Shop", -10000, ids["Food"])
    call(app, "add_transaction", wallet, "2026-07-10", "Sample Shop", -20000, ids["Food"])  # twice its usual
    call(app, "add_transaction", wallet, "2026-07-12", "Sample Boss", 50000, None)
    dash = call(app, "dashboard")
    spending = dash["spending"]
    assert spending["above_usual"] == {"cents": 2500, "percent": 25}
    assert spending["difference"] == [i - t for i, t in zip(spending["income"], spending["total"])]
    food = next(c for g in spending["groups"] for c in g["categories"] if c["id"] == ids["Food"])
    july = spending["months"].index(next(m for m in spending["months"] if m["month"] == "2026-07"))
    assert food["above"][july] and food["above_by"][july] == food["spent"][july] - food["usual"][july] == 10000
    assert all(x is None for i, x in enumerate(food["above_by"]) if not food["above"][i])
    assert "sp.income[i] - sp.total[i]" not in PAGE and "at least $25 and 25%" not in PAGE


def test_the_inbox_gives_each_group_its_total(tmp_path):
    b = Budget(tmp_path / "budget.db")
    one = b.arrive(b.card, "2026-09-02", -1250, "SAMPLE BAKERY CALGARY AB")
    two = b.arrive(b.card, "2026-09-09", -980, "SAMPLE BAKERY CALGARY AB")
    other = b.arrive(b.card, "2026-09-10", -5000, "SAMPLE HARDWARE CALGARY AB")
    assert b.item(one)["group"] == b.item(two)["group"] != ""
    assert b.item(one)["group_total"] == b.item(two)["group_total"] == -2230
    assert b.item(other)["group_total"] == b.item(other)["amount"] == -5000
    assert "b.items.reduce" not in PAGE.split("function groupCard")[1].split("}\n")[0]


def test_categorize_returns_what_the_items_came_to(app):
    ids = categories(app, {"Everyday": ["Food"]})
    wallet = call(app, "add_account", "Wallet", "cash", 10000, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    a = call(app, "add_transaction", wallet, "2026-10-02", "Sample Shop", -1250)
    b = call(app, "add_transaction", wallet, "2026-10-03", "Sample Shop", -980)
    ids_ = [x["id"] if isinstance(x, dict) else x for x in (a, b)]
    out = call(app, "categorize", txn_ids=ids_, category_id=ids["Food"])
    assert out["total"] == -2230
