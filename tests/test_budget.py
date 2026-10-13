"""The envelope rules, including credit cards. See budget.py's docstring."""

import random

from budget import average_spending, complete_months_before, compute
from helpers import CARD_FILE, bank_csv, call, card_csv, categories, write

FOOD, BILLS, INCOME, CARD_PAY = 1, 2, 3, 4
CARD, LOC = 10, 11   # debt accounts
CATS = [{"id": FOOD, "role": "normal", "debt_account_id": None},
        {"id": BILLS, "role": "normal", "debt_account_id": None},
        {"id": INCOME, "role": "income", "debt_account_id": None},
        {"id": CARD_PAY, "role": "payment", "debt_account_id": CARD}]


def run(lines, payments=(), assigned=None, opening=0, end="2026-08", carry=False):
    return compute("2026-07", end, CATS, lines, list(payments), assigned or {}, opening, carry_overspending=carry)


def test_cash_spending_and_overspending():
    r = run([("2026-07", 1, False, FOOD, -150)], assigned={("2026-07", FOOD): 100}, opening=1000)
    jul, aug = r["2026-07"], r["2026-08"]
    assert jul["categories"][FOOD]["available"] == -50
    assert jul["ready_to_assign"] == 900
    # Overspending doesn't carry: Food starts August at zero, and the 50 comes out of Ready to assign.
    assert aug["categories"][FOOD]["carried"] == 0
    assert aug["overspent_last_month"] == 50 and aug["ready_to_assign"] == 850


def test_overspending_can_carry_over_instead():
    """The setting: an overspent category starts next month below zero, and Ready to
    assign is left alone. Next month's money covers it first."""
    r = run([("2026-07", 1, False, FOOD, -150)], assigned={("2026-07", FOOD): 100}, opening=1000, carry=True)
    jul, aug = r["2026-07"], r["2026-08"]
    assert jul["ready_to_assign"] == 900
    assert aug["categories"][FOOD]["carried"] == -50 and aug["categories"][FOOD]["available"] == -50
    assert aug["overspent_last_month"] == 0 and aug["ready_to_assign"] == 900
    r = run([("2026-07", 1, False, FOOD, -150), ("2026-08", CARD, True, FOOD, -30)],
            assigned={("2026-07", FOOD): 100, ("2026-08", FOOD): 50}, opening=1000, carry=True)
    aug = r["2026-08"]
    # The 50 assigned covers July's 50 first, so August's card spending wasn't covered: new debt.
    assert aug["categories"][FOOD]["available"] == -30 and aug["new_debt"] == 30
    assert r["2026-08"]["categories"][CARD_PAY]["available"] == 0
    # Uncovered card spending is debt on the card either way, not carried in the category.
    r = run([("2026-07", CARD, True, FOOD, -80)], assigned={("2026-07", FOOD): 30}, opening=1000, carry=True)
    assert r["2026-08"]["categories"][FOOD]["carried"] == 0 and r["2026-07"]["new_debt"] == 50


def test_card_spending_moves_money_to_the_payment_envelope():
    r = run([("2026-07", CARD, True, FOOD, -80)], assigned={("2026-07", FOOD): 100}, opening=1000)
    jul = r["2026-07"]["categories"]
    assert jul[FOOD]["available"] == 20
    assert jul[CARD_PAY]["available"] == 80  # set aside to pay the card


def test_uncovered_card_spending_is_new_debt_not_next_months_problem():
    r = run([("2026-07", CARD, True, FOOD, -80)], assigned={("2026-07", FOOD): 30}, opening=1000)
    jul, aug = r["2026-07"], r["2026-08"]
    assert jul["categories"][CARD_PAY]["available"] == 30  # only what Food had
    assert jul["new_debt"] == 50 and jul["overspent_this_month"] == 0
    assert aug["overspent_last_month"] == 0


def test_paying_the_card_from_chequing_spends_the_envelope():
    r = run([("2026-07", CARD, True, FOOD, -80)], payments=[("2026-07", CARD, -80)],
            assigned={("2026-07", FOOD): 80}, opening=1000)
    assert r["2026-07"]["categories"][CARD_PAY]["available"] == 0
    # Paying old debt without assigning to the envelope is overspending it.
    r = run([], payments=[("2026-07", CARD, -500)], opening=1000)
    assert r["2026-08"]["overspent_last_month"] == 500


def test_refunds_and_rewards_on_the_card():
    r = run([("2026-07", CARD, True, FOOD, -80), ("2026-07", CARD, True, FOOD, 30)],
            assigned={("2026-07", FOOD): 80}, opening=1000)
    cats = r["2026-07"]["categories"]
    assert cats[FOOD]["available"] == 30 and cats[CARD_PAY]["available"] == 50
    r = run([("2026-07", CARD, True, FOOD, -80), ("2026-07", CARD, True, INCOME, 20)],
            assigned={("2026-07", FOOD): 80}, opening=1000)
    assert r["2026-07"]["ready_to_assign"] == 1000 - 80 + 20
    assert r["2026-07"]["categories"][CARD_PAY]["available"] == 60  # you owe 20 less


def test_balance_transfer_between_debts_moves_no_money():
    r = run([], payments=[], opening=0)
    assert r["2026-07"]["categories"][CARD_PAY]["available"] == 0


def test_money_always_adds_up_on_random_histories():
    """Ready to assign − overspent + Σ carried == cash in cash accounts, every month."""
    rng = random.Random(7)
    months = ["2026-07", "2026-08", "2026-09", "2026-10"]
    for _ in range(60):
        opening = rng.randint(0, 500_000)
        lines, payments, assigned = [], [], {}
        cash = {m: 0 for m in months}
        for m in months:
            for _ in range(rng.randint(0, 25)):
                on_card = rng.random() < 0.5
                cat = rng.choice([FOOD, BILLS, INCOME, None])
                amount = rng.randint(-60_000, 40_000 if cat in (INCOME, None) else 5_000)
                lines.append((m, CARD if on_card else 1, on_card, cat, amount))
                if not on_card:
                    cash[m] += amount
            for _ in range(rng.randint(0, 2)):
                paid = -rng.randint(0, 200_000)
                payments.append((m, CARD, paid))
                cash[m] += paid
            for cat in (FOOD, BILLS, CARD_PAY):
                if rng.random() < 0.7:
                    assigned[(m, cat)] = rng.randint(-5_000, 150_000)
        for carry in (False, True):
            result = compute(months[0], months[-1], CATS, lines, payments, assigned, opening, carry_overspending=carry)
            running = opening
            for m in months:
                running += cash[m]
                f = result[m]
                carried = sum(max(c["available"], 0) for c in f["categories"].values())
                assert f["ready_to_assign"] - f["overspent_this_month"] + carried == running, (m, carry)


def test_averages_use_whole_months_only():
    assert complete_months_before("2026-10", "2026-07", 3) == ["2026-09", "2026-08", "2026-07"]
    assert complete_months_before("2026-10", "2026-09", 6) == ["2026-09"]
    assert average_spending({"2026-07": -600, "2026-08": -300, "2026-09": 0}, ["2026-07", "2026-08", "2026-09"]) == 300


def test_catch_up_and_assigning_a_paycheque(app):
    ids = categories(app, {"Bills": ["Rent"], "Everyday": ["Food"]})
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("07/01/2026", "EFT Withdrawal to SAMPLE LANDLORD", "EFT Withdrawal to SAMPLE LANDLORD", "-1000.00"),
        ("07/15/2026", "EFT Deposit from SAMPLE EMPLOYER", "EFT Deposit from SAMPLE EMPLOYER", "2000.00"),
        ("10/01/2026", "EFT Withdrawal to SAMPLE LANDLORD", "EFT Withdrawal to SAMPLE LANDLORD", "-1000.00"),
    ])))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE, card_csv([
        ("07/03/2026", "SAMPLE GROCER CALGARY AB", "-120.00", "Groceries")])))])
    ids = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    for t in call(app, "inbox")["items"]:
        cid = ids["Rent"] if "LANDLORD" in t["raw_name"] else ids["Food"] if t["amount"] < 0 else ids["Income"]
        call(app, "approve", txn_id=t["id"], category_id=cid)
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    call(app, "set_balance", accounts["Chequing"], 500000, "2026-10-12")
    call(app, "set_balance", accounts["Tangerine Mastercard"], 12000, "2026-10-12")
    call(app, "finish_onboarding", "2026-07")

    view = call(app, "catch_up", "2026-07")
    cats = {c["name"]: c for g in view["groups"] for c in g["categories"]}
    assert all(c["available"] >= 0 for c in cats.values())
    assert cats["Rent"]["assigned"] == 100000 and cats["Food"]["assigned"] == 12000
    assert cats["Tangerine Mastercard payment"]["available"] == 12000

    call(app, "set_monthly_budget", ids["Rent"], 100000)
    call(app, "set_monthly_budget", ids["Food"], 50000)
    plan = call(app, "assign_plan", month="2026-10", amount=120000)
    assert [(p["name"], p["amount"]) for p in plan["plan"]] == [("Rent", 100000), ("Food", 20000)]
    view = call(app, "apply_plan", "2026-10", [{"category_id": p["category_id"], "amount": p["amount"]} for p in plan["plan"]])
    cats = {c["name"]: c for g in view["groups"] for c in g["categories"]}
    assert cats["Rent"]["available"] == 0 and cats["Food"]["assigned"] == 20000
    # What's already in a category counts: Food has 200 of its 500, Rent all of its 1000.
    plan = call(app, "assign_plan", month="2026-10")
    assert [(p["name"], p["has"], p["need"]) for p in plan["plan"]] == [("Rent", 100000, 0), ("Food", 20000, 30000)]
    # Money carried over from last month counts too.
    call(app, "set_assigned", "2026-09", ids["Food"], 15000)
    plan = call(app, "assign_plan", month="2026-10")
    assert [(p["name"], p["has"], p["need"]) for p in plan["plan"]][1] == ("Food", 35000, 15000)


def test_overspending_carries_over_when_switched_on(app):
    ids = categories(app, {"Everyday": ["Food"]})
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("09/03/2026", "SAMPLE GROCER", "SAMPLE GROCER", "-80.00")])))])
    [t] = call(app, "inbox")["items"]
    call(app, "approve", txn_id=t["id"], category_id=ids["Food"])
    [acct] = call(app, "accounts")
    call(app, "set_balance", acct["id"], 100000, "2026-10-12")
    call(app, "finish_onboarding", "2026-09")
    call(app, "set_monthly_budget", ids["Food"], 50000)

    food = lambda v: next(c for g in v["groups"] for c in g["categories"] if c["id"] == ids["Food"])
    october = call(app, "month", "2026-10")
    assert food(october)["available"] == 0 and october["summary"]["overspent_last_month"] == 8000
    app.settings.update({"carry_overspending": True})
    october = call(app, "month", "2026-10")
    assert october["carry_overspending"]
    assert food(october)["available"] == -8000 and october["summary"]["overspent_last_month"] == 0
    assert october["summary"]["ready_to_assign"] == 108000  # the 1,000 in the bank, plus the 80 Food still owes
    assert call(app, "assign_plan", month="2026-10")["plan"][0]["need"] == 58000  # last month's 80 too


def test_category_details_have_last_month_the_average_and_a_note(app):
    ids = categories(app, {"Everyday": ["Food"]})
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE, card_csv([
        ("07/03/2026", "SAMPLE GROCER CALGARY AB", "-300.00", "Groceries"),
        ("08/03/2026", "SAMPLE GROCER CALGARY AB", "-150.00", "Groceries"),
        ("09/03/2026", "SAMPLE GROCER CALGARY AB", "-450.00", "Groceries")])))])
    for t in call(app, "inbox")["items"]:
        call(app, "approve", txn_id=t["id"], category_id=ids["Food"], refile=False)
    call(app, "set_category_note", ids["Food"], "Costco run every second week")
    detail = call(app, "category_detail", category_id=ids["Food"])
    assert detail["last_month"] == {"month": "2026-09", "amount": 45000}
    assert detail["averages"][3]["amount"] == 30000
    assert detail["note"] == "Costco run every second week"
