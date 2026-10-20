"""The envelope rules, including credit cards. See budget.py's docstring."""

import random

from budget import average_spending, complete_months_before, compute, usual_spending
from helpers import CARD_FILE, bank_csv, call, card_csv, categories, txns, write
from util import months_between

FOOD, BILLS, INCOME, CARD_PAY = 1, 2, 3, 4
CARD, LOC = 10, 11   # debt accounts
CATS = [{"id": FOOD, "role": "normal", "debt_account_id": None},
        {"id": BILLS, "role": "normal", "debt_account_id": None},
        {"id": INCOME, "role": "income", "debt_account_id": None},
        {"id": CARD_PAY, "role": "payment", "debt_account_id": CARD}]


def run(lines, payments=(), assigned=None, opening=0, end="2026-08", overspending="this_month"):
    return compute("2026-07", end, CATS, lines, list(payments), assigned or {}, opening, overspending=overspending)


def test_cash_spending_and_overspending():
    r = run([("2026-07", 1, False, FOOD, -150)], assigned={("2026-07", FOOD): 100}, opening=1000)
    jul, aug = r["2026-07"], r["2026-08"]
    assert jul["categories"][FOOD]["available"] == -50
    # The 50 has left the bank, so it comes out of Ready to assign straight away: it's
    # what's really there (1,000 in the bank − 100 in Food − the 50 overspent = 850).
    assert jul["overspent"] == 50 and jul["ready_to_assign"] == 850
    # Overspending doesn't carry: Food starts August at zero, and nothing more comes out.
    assert aug["categories"][FOOD]["carried"] == 0
    assert aug["overspent"] == 0 and aug["ready_to_assign"] == 850


def test_overspending_can_come_out_of_next_month_instead():
    """The YNAB way: the 50 is still counted in July and comes out of August's."""
    r = run([("2026-07", 1, False, FOOD, -150)], assigned={("2026-07", FOOD): 100}, opening=1000,
            overspending="next_month")
    jul, aug = r["2026-07"], r["2026-08"]
    assert jul["overspent"] == 50 and jul["overspent_pending"] == 50 and jul["ready_to_assign"] == 900
    assert aug["categories"][FOOD]["carried"] == 0
    assert aug["overspent_last_month"] == 50 and aug["ready_to_assign"] == 850
    # Covering it in July uses July's Ready to assign, and then nothing comes out of August's.
    r = run([("2026-07", 1, False, FOOD, -150)], assigned={("2026-07", FOOD): 150}, opening=1000,
            overspending="next_month")
    assert r["2026-07"]["ready_to_assign"] == 850 and r["2026-08"]["overspent_last_month"] == 0


def test_covering_overspending_doesnt_change_ready_to_assign():
    """The money's already gone: assigning to cover it (or Catch up) leaves Ready to
    assign alone, and covering it from another category gives that much back."""
    spent = [("2026-07", 1, False, FOOD, -150)]
    before = run(spent, assigned={("2026-07", BILLS): 200}, opening=1000)["2026-07"]
    assert before["ready_to_assign"] == 1000 - 200 - 150
    covered = run(spent, assigned={("2026-07", BILLS): 200, ("2026-07", FOOD): 150}, opening=1000)["2026-07"]
    assert covered["categories"][FOOD]["available"] == 0
    assert covered["ready_to_assign"] == before["ready_to_assign"]
    moved = run(spent, assigned={("2026-07", BILLS): 50, ("2026-07", FOOD): 150}, opening=1000)["2026-07"]
    assert moved["ready_to_assign"] == before["ready_to_assign"] + 150


def test_overspending_can_carry_over_instead():
    """The setting: an overspent category starts next month below zero, and Ready to
    assign is left alone. Next month's money covers it first."""
    r = run([("2026-07", 1, False, FOOD, -150)], assigned={("2026-07", FOOD): 100}, opening=1000,
            overspending="carry_over")
    jul, aug = r["2026-07"], r["2026-08"]
    assert jul["overspent"] == 50 and jul["ready_to_assign"] == 900
    assert aug["categories"][FOOD]["carried"] == -50 and aug["categories"][FOOD]["available"] == -50
    assert aug["ready_to_assign"] == 900
    r = run([("2026-07", 1, False, FOOD, -150), ("2026-08", CARD, True, FOOD, -30)],
            assigned={("2026-07", FOOD): 100, ("2026-08", FOOD): 50}, opening=1000, overspending="carry_over")
    aug = r["2026-08"]
    # The 50 assigned covers July's 50 first, so August's card spending wasn't covered: new debt.
    assert aug["categories"][FOOD]["available"] == -30 and aug["new_debt"] == 30
    assert r["2026-08"]["categories"][CARD_PAY]["available"] == 0
    # Uncovered card spending is debt on the card either way, not carried in the category.
    r = run([("2026-07", CARD, True, FOOD, -80)], assigned={("2026-07", FOOD): 30}, opening=1000,
            overspending="carry_over")
    assert r["2026-08"]["categories"][FOOD]["carried"] == 0 and r["2026-07"]["new_debt"] == 50


def test_card_spending_moves_money_to_the_payment_envelope():
    r = run([("2026-07", CARD, True, FOOD, -80)], assigned={("2026-07", FOOD): 100}, opening=1000)
    jul = r["2026-07"]["categories"]
    assert jul[FOOD]["available"] == 20
    assert jul[CARD_PAY]["available"] == 80  # set aside to pay the card


def test_uncovered_card_spending_is_new_debt_not_next_months_problem():
    for overspending in ("this_month", "next_month", "carry_over"):
        r = run([("2026-07", CARD, True, FOOD, -80)], assigned={("2026-07", FOOD): 30}, opening=1000,
                overspending=overspending)
        jul, aug = r["2026-07"], r["2026-08"]
        assert jul["categories"][CARD_PAY]["available"] == 30  # only what Food had
        assert jul["new_debt"] == 50 and jul["overspent"] == 0
        assert jul["ready_to_assign"] == aug["ready_to_assign"] == 1000 - 30
        assert aug["categories"][FOOD]["carried"] == 0


def test_paying_the_card_from_chequing_spends_the_envelope():
    r = run([("2026-07", CARD, True, FOOD, -80)], payments=[("2026-07", CARD, -80)],
            assigned={("2026-07", FOOD): 80}, opening=1000)
    assert r["2026-07"]["categories"][CARD_PAY]["available"] == 0
    # Paying old debt without assigning to the envelope is overspending it.
    r = run([], payments=[("2026-07", CARD, -500)], opening=1000)
    assert r["2026-07"]["overspent"] == 500 and r["2026-07"]["ready_to_assign"] == 500


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
    """Ready to assign + money in categories − overspending not out of it yet + money in
    waiting in the inbox == cash in cash accounts, every month, whichever way overspending
    is paid for."""
    rng = random.Random(7)
    months = ["2026-07", "2026-08", "2026-09", "2026-10"]
    for _ in range(60):
        opening = rng.randint(0, 500_000)
        lines, payments, assigned = [], [], {}
        cash = {m: 0 for m in months}
        waiting = {m: 0 for m in months}
        for m in months:
            for _ in range(rng.randint(0, 25)):
                on_card = rng.random() < 0.5
                cat = rng.choice([FOOD, BILLS, INCOME, None])
                amount = rng.randint(-60_000, 40_000 if cat in (INCOME, None) else 5_000)
                lines.append((m, CARD if on_card else 1, on_card, cat, amount))
                if not on_card:
                    cash[m] += amount
                    if cat is None and amount > 0:
                        waiting[m] += amount
            for _ in range(rng.randint(0, 2)):
                paid = -rng.randint(0, 200_000)
                payments.append((m, CARD, paid))
                cash[m] += paid
            for cat in (FOOD, BILLS, CARD_PAY):
                if rng.random() < 0.7:
                    assigned[(m, cat)] = rng.randint(-5_000, 150_000)
        for overspending in ("this_month", "next_month", "carry_over"):
            result = compute(months[0], months[-1], CATS, lines, payments, assigned, opening, overspending=overspending)
            running, waited = opening, 0
            for m in months:
                running += cash[m]
                waited += waiting[m]
                f = result[m]
                in_categories = sum(max(c["available"], 0) for c in f["categories"].values())
                assert f["in_categories"] == in_categories
                assert f["overspent_pending"] == (0 if overspending == "this_month" else f["overspent"])
                assert f["waiting"] == waited
                assert f["ready_to_assign"] + in_categories - f["overspent_pending"] + waited == running, \
                    (m, overspending)


def test_money_waiting_in_the_inbox():
    """docs/adr/0002: money out without a category has left the bank, so it comes off
    Ready to assign (on a card, it's set aside to pay the card); money in waits."""
    cash = run([("2026-07", 1, False, None, -2000), ("2026-07", 1, False, None, 5000)], opening=10000)
    assert [(m["ready_to_assign"], m["waiting"]) for m in cash.values()] == [(8000, 5000), (8000, 5000)]
    card = run([("2026-07", CARD, True, None, -3000), ("2026-07", CARD, True, None, 1000)])["2026-07"]
    assert card["categories"][CARD_PAY]["available"] == 3000
    assert (card["ready_to_assign"], card["waiting"]) == (-3000, 0)  # a refund on the card isn't cash

def test_averages_use_whole_months_only():
    assert complete_months_before("2026-10", "2026-07", 3) == ["2026-09", "2026-08", "2026-07"]
    assert complete_months_before("2026-10", "2026-09", 6) == ["2026-09"]
    assert average_spending({"2026-07": -600, "2026-08": -300, "2026-09": 0}, ["2026-07", "2026-08", "2026-09"]) == 300


def test_usual_starts_with_a_categorys_first_spending():
    """A bill that started last month isn't averaged with the months before it existed."""
    bill = {"2026-08": -34000, "2026-09": -34000}
    assert usual_spending(bill, "2026-09", "2026-06", 3) == 34000
    assert usual_spending(bill, "2026-10", "2026-06", 3) == 34000
    assert usual_spending(bill, "2026-08", "2026-06", 3) is None     # nothing before it
    assert usual_spending({}, "2026-10", "2026-06", 3) is None
    gifts = {"2026-06": -20000}                                        # months with nothing still count
    assert usual_spending(gifts, "2026-10", "2026-06", 3) == 0
    assert usual_spending(gifts, "2026-09", "2026-06", 3) == round(20000 / 3)
    assert usual_spending({"2026-01": -900}, "2026-10", "2026-07", 3) == 0  # only months every account has


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
        call(app, "categorize", txn_ids=[t["id"]], category_id=cid, recheck_payee=True)
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
    # Rent was paid on 10/01 with nothing assigned: that came out of Ready to assign
    # already, so covering it costs nothing and the 200 shared out all goes to Food.
    before = call(app, "month", "2026-10")["summary"]
    assert before["overspent"] == 100000
    plan = call(app, "assign_plan", month="2026-10", amount=20000)
    assert [(p["name"], p["amount"], p["covered"]) for p in plan["plan"]] == [("Rent", 100000, 100000), ("Food", 20000, 0)]
    assert plan["left_over"] == 0
    view = call(app, "apply_plan", "2026-10", [{"category_id": p["category_id"], "amount": p["amount"]} for p in plan["plan"]])
    cats = {c["name"]: c for g in view["groups"] for c in g["categories"]}
    assert cats["Rent"]["available"] == 0 and cats["Food"]["assigned"] == 20000
    assert view["summary"]["ready_to_assign"] == before["ready_to_assign"] - 20000
    # What's already in a category counts: Food has 200 of its 500, Rent all of its 1000.
    plan = call(app, "assign_plan", month="2026-10")
    assert [(p["name"], p["has"], p["need"]) for p in plan["plan"]] == [("Rent", 100000, 0), ("Food", 20000, 30000)]
    # Money carried over from last month counts too.
    call(app, "set_assigned", "2026-09", ids["Food"], 15000)
    plan = call(app, "assign_plan", month="2026-10")
    assert [(p["name"], p["has"], p["need"]) for p in plan["plan"]][1] == ("Food", 35000, 15000)


def test_the_overspending_setting(app):
    ids = categories(app, {"Everyday": ["Food"]})
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("09/03/2026", "SAMPLE GROCER", "SAMPLE GROCER", "-80.00")])))])
    [t] = call(app, "inbox")["items"]
    call(app, "categorize", txn_ids=[t["id"]], category_id=ids["Food"], recheck_payee=True)
    [acct] = call(app, "accounts")
    call(app, "set_balance", acct["id"], 100000, "2026-10-12")
    call(app, "finish_onboarding", "2026-09")
    call(app, "set_monthly_budget", ids["Food"], 50000)

    food = lambda v: next(c for g in v["groups"] for c in g["categories"] if c["id"] == ids["Food"])
    september, october = call(app, "month", "2026-09"), call(app, "month", "2026-10")
    assert october["overspending"] == "this_month"
    assert september["summary"]["overspent"] == 8000 and september["summary"]["ready_to_assign"] == 100000
    assert food(october)["available"] == 0 and october["summary"]["ready_to_assign"] == 100000  # what's in the bank
    # Covering September's overspending costs nothing: it already came out.
    plan = call(app, "assign_plan", month="2026-09", amount=0)["plan"]
    assert (plan[0]["amount"], plan[0]["covered"]) == (8000, 8000)

    app.settings.update({"overspending": "next_month"})
    september, october = call(app, "month", "2026-09"), call(app, "month", "2026-10")
    assert september["summary"]["ready_to_assign"] == 108000 and september["summary"]["overspent_pending"] == 8000
    assert october["summary"]["overspent_last_month"] == 8000 and october["summary"]["ready_to_assign"] == 100000
    assert call(app, "assign_plan", month="2026-09", amount=0)["plan"][0]["amount"] == 0  # covering it costs money

    app.settings.update({"overspending": "carry_over"})
    october = call(app, "month", "2026-10")
    assert october["overspending"] == "carry_over"
    assert food(october)["available"] == -8000 and october["summary"]["overspent_pending"] == 8000  # still owed
    assert october["summary"]["ready_to_assign"] == 108000  # the 1,000 in the bank, plus the 80 Food still owes
    assert call(app, "assign_plan", month="2026-10")["plan"][0]["need"] == 58000  # last month's 80 too


def test_category_details_have_last_month_the_average_and_a_note(app):
    ids = categories(app, {"Everyday": ["Food"]})
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE, card_csv([
        ("07/03/2026", "SAMPLE GROCER CALGARY AB", "-300.00", "Groceries"),
        ("08/03/2026", "SAMPLE GROCER CALGARY AB", "-150.00", "Groceries"),
        ("09/03/2026", "SAMPLE GROCER CALGARY AB", "-450.00", "Groceries")])))])
    for t in call(app, "inbox")["items"]:
        call(app, "categorize", txn_ids=[t["id"]], category_id=ids["Food"])
    call(app, "set_category_note", ids["Food"], "Costco run every second week")
    detail = call(app, "category_detail", category_id=ids["Food"])
    assert detail["last_month"] == {"month": "2026-09", "amount": 45000}
    assert detail["averages"][3]["amount"] == 30000
    assert detail["note"] == "Costco run every second week"


def test_what_makes_up_spent_adds_up_to_it(app):
    """Clicking Spent (a category's or a group's) lists what makes it up: a split counts
    only its line, and a card's payment envelope shows the payment and the card spending
    moved in. The total is always the figure on the Budget page."""
    categories(app, {"Bills": ["Rent"], "Everyday": ["Food", "Fun"]})
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE, card_csv([
        ("10/03/2026", "SAMPLE GROCER CALGARY AB", "-120.00", "Groceries"),
        ("10/04/2026", "SAMPLE CINEMA CALGARY AB", "-30.00", "Entertainment")])))])
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("10/01/2026", "EFT Withdrawal to SAMPLE LANDLORD", "EFT Withdrawal to SAMPLE LANDLORD", "-1000.00"),
        ("10/02/2026", "EFT Deposit from SAMPLE EMPLOYER", "EFT Deposit from SAMPLE EMPLOYER", "2000.00"),
        ("10/06/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-50.00"),
    ])))])
    ids = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    for t in call(app, "inbox")["items"]:
        name = t["raw_name"]
        cid = ids["Rent"] if "LANDLORD" in name else ids["Income"] if t["amount"] > 0 else \
            ids["Fun"] if "CINEMA" in name else ids["Food"]
        call(app, "categorize", txn_ids=[t["id"]], category_id=cid)
    grocer = next(t for t in txns(app) if "GROCER" in t["raw_name"])
    call(app, "split_transaction", grocer["id"], [{"category_id": ids["Food"], "amount": -8000},
                                                  {"category_id": ids["Fun"], "amount": -4000}])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    call(app, "set_balance", accounts["Chequing"], 500000, "2026-10-12")
    call(app, "set_balance", accounts["Tangerine Mastercard"], 10000, "2026-10-12")
    call(app, "finish_onboarding", "2026-10")
    call(app, "set_assigned", "2026-10", ids["Food"], 10000)
    call(app, "set_assigned", "2026-10", ids["Fun"], 10000)

    view = call(app, "month", "2026-10")
    for g in view["groups"]:
        assert call(app, "spent_detail", month="2026-10", group_id=g["id"])["total"] == g["activity"], g["name"]
        for cat in g["categories"]:
            detail = call(app, "spent_detail", month="2026-10", category_id=cat["id"])
            assert detail["total"] == cat["activity"], cat["name"]
    food = call(app, "spent_detail", month="2026-10", category_id=ids["Food"])
    assert [(l["amount"], l["split"], l["full_amount"]) for l in food["lines"]] == [(-8000, 1, -12000)]
    card = call(app, "category_detail", category_id=ids["Tangerine Mastercard payment"], month="2026-10")["spent"]
    assert {l.get("label"): l["amount"] for l in card["lines"]} == {
        "Paid to the card from Chequing": -5000, "Set aside for card spending in Food": 8000,
        "Set aside for card spending in Fun": 7000}
    assert card["total"] == 10000


def test_ready_to_assign_adds_up_to_whats_in_the_bank(app):
    """The Budget page's check: Ready to assign + what's in categories == the cash
    accounts' own balances at the end of the month (none while one has no balance)."""
    ids = categories(app, {"Bills": ["Rent"], "Everyday": ["Food"]})
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("09/01/2026", "EFT Withdrawal to SAMPLE LANDLORD", "EFT Withdrawal to SAMPLE LANDLORD", "-1000.00"),
        ("09/15/2026", "EFT Deposit from SAMPLE EMPLOYER", "EFT Deposit from SAMPLE EMPLOYER", "2000.00"),
        ("10/01/2026", "EFT Withdrawal to SAMPLE LANDLORD", "EFT Withdrawal to SAMPLE LANDLORD", "-1000.00"),
        ("10/06/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-50.00"),
    ])))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE, card_csv([
        ("10/03/2026", "SAMPLE GROCER CALGARY AB", "-120.00", "Groceries")])))])
    ids = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    for t in call(app, "inbox")["items"]:
        cid = ids["Rent"] if "LANDLORD" in t["raw_name"] else ids["Income"] if t["amount"] > 0 else ids["Food"]
        call(app, "categorize", txn_ids=[t["id"]], category_id=cid)
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    assert call(app, "month", "2026-10")["summary"] is None  # no budget yet
    call(app, "finish_onboarding", "2026-09")
    assert call(app, "month", "2026-10")["summary"]["cash"] is None  # Chequing has no balance yet
    call(app, "set_balance", accounts["Chequing"], 300000, "2026-10-12")
    call(app, "set_balance", accounts["Tangerine Mastercard"], 7000, "2026-10-12")
    call(app, "set_assigned", "2026-10", ids["Food"], 20000)
    for month, cash in (("2026-09", 405000), ("2026-10", 300000)):
        s = call(app, "month", month)["summary"]
        assert s["cash"] == {"total": cash, "accounts": ["Chequing"]}
        assert s["ready_to_assign"] + s["in_categories"] == cash, month
    s = call(app, "month", "2026-10")["summary"]
    assert s["overspent"] == 100000  # Rent, paid with nothing assigned
    assert s["in_categories"] == 8000 + 7000  # Food's 80 left; its 120 moved to the card, less the 50 paid
    assert s["ready_to_assign"] == 300000 - 15000


def test_payment_estimate_averages_the_last_three_whole_months(app):
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        (date, "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", amount)
        for date, amount in (("06/20/2026", "-900.00"), ("07/20/2026", "-300.00"), ("08/20/2026", "-300.00"),
                             ("09/20/2026", "-300.00"), ("10/02/2026", "-600.00"))])))])
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE, card_csv([
        (date, "PAYMENT - THANK YOU", amount, "")
        for date, amount in (("06/20/2026", "900.00"), ("07/20/2026", "300.00"), ("08/20/2026", "300.00"),
                             ("09/20/2026", "300.00"), ("10/02/2026", "600.00"))])))])
    [card] = call(app, "debt")["accounts"]
    assert card["estimate_payment"] == 30000  # July to September; not June, nor October so far


def test_an_inbox_item_counts_in_no_category_until_its_categorized(app):
    """docs/adr/0002: an inbox item's suggestion is only offered. Its money out comes off
    Ready to assign, its money in waits, and neither is in a category's Spent or Usual."""
    ids = categories(app, {"Everyday": ["Food", "Household"]})
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv([
        ("09/02/2026", "SAMPLE MART CALGARY AB", "SAMPLE MART CALGARY AB", "-3.99"),
        ("09/03/2026", "SAMPLE MART CALGARY AB", "SAMPLE MART CALGARY AB", "-980.00"),
        ("10/05/2026", "SAMPLE MART CALGARY AB", "SAMPLE MART CALGARY AB", "-4.99"),
        ("10/06/2026", "INTERAC e-Transfer From: RILEY", "INTERAC e-Transfer From: RILEY EXAMPLE", "50.00"),
    ])))])
    for t in txns(app, month="2026-09"):
        call(app, "categorize", txn_ids=[t["id"]], category_id=ids["Food" if t["amount"] == -399 else "Household"])
    items = {i["amount"]: i for i in call(app, "inbox")["items"]}
    assert set(items) == {-499, 5000}
    assert (items[-499]["category_id"], items[-499]["guess"]) == (None, ids["Food"])  # Accept all would use it
    assert (items[5000]["category_id"], items[5000]["guess"]) == (None, None)
    call(app, "finish_onboarding", "2026-09")
    call(app, "set_balance", {a["name"]: a["id"] for a in call(app, "accounts")}["Chequing"], 100000, "2026-10-12")

    def october():
        view = call(app, "month", "2026-10")
        food = next(c for g in view["groups"] for c in g["categories"] if c["id"] == ids["Food"])
        s = view["summary"]
        assert s["ready_to_assign"] + s["in_categories"] - s["overspent_pending"] + s["waiting"] == s["cash"]["total"]
        return s, food

    s, food = october()
    assert (s["uncategorized"], s["waiting"], food["activity"]) == (-499, 5000, 0)
    assert call(app, "spent_detail", "2026-10", ids["Food"])["total"] == 0
    assert [h["spent"] for h in call(app, "category_detail", ids["Food"])["history"] if h["month"] == "2026-10"] == [0]
    rta = s["ready_to_assign"]
    call(app, "categorize", txn_ids=[items[-499]["id"]], category_id=ids["Food"])
    call(app, "categorize", txn_ids=[items[5000]["id"]], category_id=ids["Food"])  # Riley paid half back
    s, food = october()
    assert (s["uncategorized"], s["waiting"], food["activity"]) == (0, 0, 5000 - 499)
    assert s["ready_to_assign"] == rta + 499  # the 4.99 is Food's now, and Riley's 50 covers it


def test_the_dashboard(app):
    """Four figures, then what each category spent month by month: a whole month at least
    $25 and 25% above its Usual (the whole months before it) is marked; this month so far,
    a category with nothing before, and small changes aren't. Inbox items aren't counted."""
    ids = categories(app, {"Everyday": ["Food", "Fun", "Books", "Phone", "Old"]})
    rows = [(f"{m:02d}/05/2026", "SAMPLE GROCER CALGARY AB", "SAMPLE GROCER CALGARY AB", amount)
            for m, amount in ((6, "-100.00"), (7, "-100.00"), (8, "-100.00"), (9, "-200.00"), (10, "-300.00"))]
    rows += [(f"{m:02d}/06/2026", "SAMPLE ARCADE CALGARY AB", "SAMPLE ARCADE CALGARY AB", amount)
             for m, amount in ((6, "-4.00"), (7, "-4.00"), (8, "-4.00"), (9, "-6.00"))]
    rows += [(f"{m:02d}/08/2026", "SAMPLE PHONE CO", "SAMPLE PHONE CO", "-60.00") for m in (8, 9)]
    rows += [("09/07/2026", "SAMPLE BOOKSHOP CALGARY AB", "SAMPLE BOOKSHOP CALGARY AB", "-100.00"),
             ("09/15/2026", "EFT Deposit from SAMPLE EMPLOYER", "EFT Deposit from SAMPLE EMPLOYER", "2000.00"),
             ("09/20/2026", "SAMPLE MYSTERY SHOP CALGARY AB", "SAMPLE MYSTERY SHOP CALGARY AB", "-50.00")]
    call(app, "import_files", [str(write(app.test_downloads, "Chequing.CSV", bank_csv(rows)))])
    ids = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}  # Income too now
    where = {"GROCER": "Food", "ARCADE": "Fun", "BOOKSHOP": "Books", "PHONE": "Phone", "EMPLOYER": "Income"}
    for t in call(app, "inbox")["items"]:
        name = next((cat for word, cat in where.items() if word in t["raw_name"]), None)
        if name:
            call(app, "categorize", txn_ids=[t["id"]], category_id=ids[name])
    call(app, "set_category_hidden", ids["Old"], True)
    call(app, "finish_onboarding", "2026-09")
    call(app, "set_balance", {a["name"]: a["id"] for a in call(app, "accounts")}["Chequing"], 100000, "2026-10-12")

    d = call(app, "dashboard", 6)
    sp = d["spending"]
    assert [m["month"] for m in sp["months"]] == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert [m["so_far"] for m in sp["months"]] == [False] * 4 + [True]
    [everyday] = sp["groups"]
    rows = {c["name"]: c for c in everyday["categories"]}
    assert "Old" not in rows  # hidden, and nothing spent
    assert rows["Food"]["spent"] == [10000, 10000, 10000, 20000, 30000]
    assert rows["Food"]["usual"][3] == 10000
    assert rows["Food"]["above"] == [False, False, False, True, False]  # October is only so far
    assert rows["Fun"]["above"] == [False] * 5    # 50% more, but only $2
    assert rows["Books"]["above"] == [False] * 5  # nothing before September to compare with
    assert rows["Phone"]["usual"][3] == 6000 and rows["Phone"]["above"] == [False] * 5  # started in August
    assert everyday["above"] == [False, False, False, True, False]
    assert sp["total"] == [10400, 10400, 16400, 36600, 30000]  # the mystery shop is still in the inbox
    assert sp["income"][3] == 200000 and sp["income_group"]
    assert (d["inbox"], d["cash"], d["has_debt"]) == (1, 100000, False)
    assert [o["name"] for o in d["overspent"]] == ["Food"]  # nothing assigned in October
    assert d["summary"]["ready_to_assign"] + d["summary"]["in_categories"] == d["cash"]
    nw = d["net_worth"]  # Chequing's balance at the end of each month (October: so far)
    assert nw["months"] == ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert nw["cash"] == [-1600, -12000, -28400, 130000, 100000] and nw["owed"] == [0] * 5
    assert nw["net"] == nw["cash"] and nw["complete"] == [True] * 5
    assert [m["month"] for m in call(app, "dashboard", 3)["spending"]["months"]][0] == "2026-07"
    assert call(app, "dashboard", "all")["spending"]["months"][0]["month"] == "2026-06"
    assert not app.dispatch("dashboard", [5])["ok"]
    call(app, "save_settings", {"dashboard_months": 3})  # the months you chose come back
    assert [m["month"] for m in call(app, "dashboard")["spending"]["months"]][0] == "2026-07"

    # A card that only has October: Net worth still follows the months chosen, and the
    # months before it are marked as not every account having data.
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE, card_csv([
        ("10/03/2026", "SAMPLE GROCER CALGARY AB", "-40.00", "Groceries")])))])
    card = next(a for a in call(app, "accounts") if a["kind"] == "credit_card")
    call(app, "set_balance", card["id"], -4000, "2026-10-12")
    for months, first in ((3, "2026-07"), (6, "2026-06"), ("all", "2026-06")):
        nw = call(app, "dashboard", months)["net_worth"]
        assert nw["months"] == months_between(first, "2026-10")
        assert nw["complete"] == [False] * (len(nw["months"]) - 1) + [True]
        assert nw["owed"] == [0] * (len(nw["months"]) - 1) + [4000]  # nothing owed before it
