"""Budget figures, through figures.Figures on a real (temporary) database.

budget.compute's own tests check the envelope rules on hand-built lines. These check the
whole module: the queries that gather what counts in a category, card payments and
balances, as well as the rules, on random histories written into SQLite.
"""

import random

import ledger
from db import Database
from figures import Figures
from util import months_between

TODAY = "2026-10-19"
START = "2026-07"
MONTHS = months_between("2026-06", "2026-10")      # June is before the budget starts
RULES = ("this_month", "next_month", "carry_over")


def add(c, account, date, amount, category=None, transfer=None, split=None, status=None):
    if status is None:
        status = "done" if category or transfer or split else "review"
    tid = c.execute(
        "INSERT INTO transactions (account_id, date, amount, category_id, status, transfer_account_id, is_split) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (account, date, amount, category, status, transfer, 1 if split else 0)).lastrowid
    for cat, part in split or ():
        c.execute("INSERT INTO splits (transaction_id, category_id, amount) VALUES (?, ?, ?)", (tid, cat, part))
    return tid


def transfer(c, from_account, to_account, date, amount):
    """Money moving between two of your accounts: both halves."""
    add(c, from_account, date, -amount, transfer=to_account)
    add(c, to_account, date, amount, transfer=from_account)


def random_budget(path, seed):
    """A made-up budget: chequing, savings and two cards; Food, Bills and Income; a few
    months of spending (some split, some still in the inbox), card payments, a balance
    transfer between the cards, savings moved to chequing, and money assigned."""
    rng = random.Random(seed)
    db = Database(path)
    with db.write() as c:
        chequing = ledger.create_account(c, "Chequing", "chequing", anchor_date="2026-06-30",
                                         anchor_balance=rng.randint(0, 500_000))
        savings = ledger.create_account(c, "Savings", "savings", anchor_date="2026-06-30",
                                        anchor_balance=rng.randint(0, 300_000))
        card_a = ledger.create_account(c, "Card A", "credit_card", anchor_date="2026-06-30",
                                       anchor_balance=-rng.randint(0, 200_000))
        card_b = ledger.create_account(c, "Card B", "credit_card", anchor_date="2026-06-30",
                                       anchor_balance=-rng.randint(0, 200_000))
        group = c.execute("INSERT INTO category_groups (name, sort) VALUES ('Everyday', 1)").lastrowid
        food = c.execute("INSERT INTO categories (group_id, name, sort) VALUES (?, 'Food', 1)", (group,)).lastrowid
        bills = c.execute("INSERT INTO categories (group_id, name, sort) VALUES (?, 'Bills', 2)", (group,)).lastrowid
        income = ledger.income_category(c)
        payment = ledger.payment_categories(c)
        c.execute("INSERT INTO meta (key, value) VALUES ('budget_start', ?)", (START,))
        for m in MONTHS:
            day = lambda: f"{m}-{rng.randint(1, 28):02d}"  # noqa: E731
            for _ in range(rng.randint(0, 20)):
                account = rng.choice([chequing, chequing, savings, card_a, card_b])
                category = rng.choice([food, bills, income, None])
                amount = rng.randint(-60_000, 40_000 if category in (income, None) else 5_000)
                add(c, account, day(), amount, category)
            for _ in range(rng.randint(0, 3)):
                amount = -rng.randint(1_000, 30_000)
                first = rng.randint(amount, 0)
                # A split back in the inbox (one of its categories was deleted) counts nowhere yet.
                add(c, rng.choice([chequing, card_a]), day(), amount, split=[(food, first), (bills, amount - first)],
                    status=rng.choice(["done", "done", "review"]))
            for card in (card_a, card_b):
                if rng.random() < 0.7:
                    transfer(c, chequing, card, day(), rng.randint(0, 150_000))
            if rng.random() < 0.3:
                transfer(c, card_a, card_b, day(), rng.randint(0, 50_000))
            if rng.random() < 0.5:
                transfer(c, savings, chequing, day(), rng.randint(0, 50_000))
            if m >= START:
                for cat in (food, bills, *payment.values()):
                    if rng.random() < 0.7:
                        c.execute("INSERT INTO budget (month, category_id, assigned) VALUES (?, ?, ?)",
                                  (m, cat, rng.randint(-5_000, 150_000)))
    return db


def test_money_adds_up_on_random_databases(tmp_path):
    """Ready to assign + money in categories − overspending not taken out yet + money in
    waiting in the inbox == Cash, from the accounts' own balances, every month."""
    for seed in range(25):
        db = random_budget(tmp_path / f"{seed}.db", seed)
        with db.read() as c:
            for rule in RULES:
                f = Figures(c, TODAY, rule)
                for m in months_between(START, "2026-10"):
                    s = f.month(m)["summary"]
                    assert s["ready_to_assign"] + s["in_categories"] - s["overspent_pending"] + s["waiting"] \
                        == s["cash"]["total"], (seed, rule, m)
                    assert s["off"] == 0
        db.close()


def simple_budget(path, balance=100_000):
    """Chequing with a balance, Food and Bills, the budget starting in July."""
    db = Database(path)
    with db.write() as c:
        chequing = ledger.create_account(c, "Chequing", "chequing", anchor_date="2026-06-30", anchor_balance=balance)
        group = c.execute("INSERT INTO category_groups (name, sort) VALUES ('Everyday', 1)").lastrowid
        food = c.execute("INSERT INTO categories (group_id, name) VALUES (?, 'Food')", (group,)).lastrowid
        bills = c.execute("INSERT INTO categories (group_id, name) VALUES (?, 'Bills')", (group,)).lastrowid
        c.execute("INSERT INTO meta (key, value) VALUES ('budget_start', ?)", (START,))
    return db, chequing, food, bills


def test_the_check_line_says_how_far_off_it_is(tmp_path):
    """Half of a transfer missing (the savings side wasn't imported): Cash is short of
    what Ready to assign and the categories account for, by that much."""
    db, chequing, food, bills = simple_budget(tmp_path / "b.db")
    with db.write() as c:
        savings = ledger.create_account(c, "Savings", "savings", anchor_date="2026-06-30", anchor_balance=0)
        add(c, chequing, "2026-09-10", -10_000, transfer=savings)
    with db.read() as c:
        assert Figures(c, TODAY).month("2026-09")["summary"]["off"] == 10_000


def test_an_inbox_item_counts_in_no_category(tmp_path):
    """docs/adr/0002, here for a split that went back to the inbox: its lines still name
    categories, but none of them counts until it's categorized again."""
    db = Database(tmp_path / "b.db")
    with db.write() as c:
        chequing = ledger.create_account(c, "Chequing", "chequing", anchor_date="2026-06-30", anchor_balance=100_000)
        group = c.execute("INSERT INTO category_groups (name, sort) VALUES ('Everyday', 1)").lastrowid
        food = c.execute("INSERT INTO categories (group_id, name) VALUES (?, 'Food')", (group,)).lastrowid
        bills = c.execute("INSERT INTO categories (group_id, name) VALUES (?, 'Bills')", (group,)).lastrowid
        c.execute("INSERT INTO meta (key, value) VALUES ('budget_start', ?)", (START,))
        add(c, chequing, "2026-09-03", -5000, food)
        add(c, chequing, "2026-09-04", -2000, split=[(food, -1500), (bills, -500)], status="review")
    with db.read() as c:
        f = Figures(c, TODAY)
        assert f.month("2026-09")["categories"][food]["activity"] == -5000
        assert f.month("2026-09")["categories"][bills]["activity"] == 0
        assert f.spent([food, bills], "2026-09")["total"] == -5000
        assert [h["spent"] for h in f.history(food)["history"] if h["month"] == "2026-09"] == [5000]
        assert f.spending(["2026-09"])["total"] == [5000]
        # Both have left the bank all the same: Food's overspending and the inbox item.
        assert f.month("2026-09")["summary"]["ready_to_assign"] == 100_000 - 5000 - 2000


def test_spent_lines_add_up_to_spent(tmp_path):
    """The lines behind a category's Spent add up to it, payment categories included
    (payments to the card, and card spending set aside in it), before the budget starts too."""
    for seed in range(10):
        db = random_budget(tmp_path / f"{seed}.db", seed)
        with db.read() as c:
            for rule in RULES:
                f = Figures(c, TODAY, rule)
                for m in MONTHS:
                    for cid, cat in f.month(m)["categories"].items():
                        assert f.spent([cid], m)["total"] == cat["activity"], (seed, rule, m, cid)
        db.close()


def test_catch_up_leaves_no_category_overspent(tmp_path):
    for seed in range(10):
        for rule in RULES:
            db = random_budget(tmp_path / f"{seed}-{rule}.db", seed)
            past = months_between(START, "2026-09")
            with db.write() as c:
                for (m, cid), cents in Figures(c, TODAY, rule).catch_up(past).items():
                    c.execute("INSERT INTO budget (month, category_id, assigned) VALUES (?, ?, ?) "
                              "ON CONFLICT(month, category_id) DO UPDATE SET assigned = excluded.assigned", (m, cid, cents))
            with db.read() as c:
                f = Figures(c, TODAY, rule)
                for m in past:
                    assert all(cat["available"] >= 0 for cat in f.month(m)["categories"].values()), (seed, rule, m)
            db.close()


def test_catch_up_before_the_budget_starts(tmp_path):
    db = random_budget(tmp_path / "b.db", 1)
    with db.read() as c:
        assert Figures(c, TODAY).catch_up(["2026-06"]) is None
        assert Figures(c, TODAY).catch_up([]) == {}


def test_assign_plan_shares_out_exactly_the_amount(tmp_path):
    """What's handed out plus what's left over is the amount, apart from covering this
    month's overspending, which is already paid for."""
    for seed in range(10):
        db = random_budget(tmp_path / f"{seed}.db", seed)
        with db.write() as c:
            c.execute("UPDATE categories SET monthly_budget = 40000 WHERE debt_account_id IS NULL")
        with db.read() as c:
            for rule in RULES:
                for amount in (0, 25_000, 1_000_000):
                    plan = Figures(c, TODAY, rule).assign_plan("2026-10", amount)
                    used = sum(max(0, p["amount"] - p["covered"]) for p in plan["plan"])
                    assert used + plan["left_over"] == amount, (seed, rule, amount)
                    assert all(p["amount"] <= p["need"] for p in plan["plan"])
        db.close()


def test_last_months_count_from_a_categorys_first_spending(tmp_path):
    """A $50 phone bill that started in August: in October its Usual is $50, and the
    pop-over's "Last 3 months" agrees (two months, not July's $0 as well)."""
    db, chequing, food, bills = simple_budget(tmp_path / "b.db")
    with db.write() as c:
        add(c, chequing, "2026-06-12", -2000, food)
        for m in ("2026-08", "2026-09", "2026-10"):
            add(c, chequing, f"{m}-05", -5000, bills)
    with db.read() as c:
        f = Figures(c, TODAY)
        assert f.history(bills)["averages"] == {3: {"months": 2, "amount": 5000}}
        assert f.month("2026-10")["categories"][bills]["usual"] == 5000


def test_a_past_months_usual_comes_from_the_months_before_it(tmp_path):
    db, chequing, food, bills = simple_budget(tmp_path / "b.db")
    with db.write() as c:
        for i, m in enumerate(months_between("2026-03", "2026-09"), start=1):
            add(c, chequing, f"{m}-15", -10_000 * i, food)
    with db.read() as c:
        f = Figures(c, TODAY)
        assert f.month("2026-06")["categories"][food]["usual"] == 20_000   # March to May
        assert f.month("2026-10")["categories"][food]["usual"] == 60_000   # July to September
        assert f.month("2026-11")["categories"][food]["usual"] == 60_000   # not October: it isn't over


def test_a_balance_transfer_isnt_a_payment(tmp_path):
    """Paying Card B $300 a month from chequing, plus $1,500 moved over from Card A in
    August: the estimated payment is $300."""
    db, chequing, food, bills = simple_budget(tmp_path / "b.db")
    with db.write() as c:
        card_a = ledger.create_account(c, "Card A", "credit_card", anchor_date="2026-06-30", anchor_balance=-300_000)
        card_b = ledger.create_account(c, "Card B", "credit_card", anchor_date="2026-06-30", anchor_balance=-100_000)
        for m in ("2026-07", "2026-08", "2026-09"):
            transfer(c, chequing, card_b, f"{m}-20", 30_000)
        transfer(c, card_a, card_b, "2026-08-02", 150_000)
    with db.read() as c:
        debts = {d["account_id"]: d for d in Figures(c, TODAY).debts()}
        assert debts[card_b]["estimate_payment"] == 30_000
        assert debts[card_a]["estimate_payment"] is None


def test_a_closed_card_is_in_debts_while_it_still_owes(tmp_path):
    db, chequing, food, bills = simple_budget(tmp_path / "b.db")
    with db.write() as c:
        owing = ledger.create_account(c, "Card A", "credit_card", anchor_date="2026-06-30", anchor_balance=-40_000)
        paid = ledger.create_account(c, "Card B", "credit_card", anchor_date="2026-06-30", anchor_balance=0)
        c.execute("UPDATE accounts SET closed = 1 WHERE id IN (?, ?)", (owing, paid))
    with db.read() as c:
        f = Figures(c, TODAY)
        assert [d["account_id"] for d in f.debts()] == [owing]
        assert f.balances()["owed"] == 40_000


def test_cash(tmp_path):
    """Cash counts open cash accounts and closed ones with money left. A closed account
    that never had a balance doesn't matter; an open one without a balance makes Cash
    unknown."""
    db, chequing, food, bills = simple_budget(tmp_path / "b.db")
    with db.write() as c:
        old = ledger.create_account(c, "Old Savings", "savings")
        kept = ledger.create_account(c, "Closed With Money", "savings", anchor_date="2026-06-30", anchor_balance=2500)
        c.execute("UPDATE accounts SET closed = 1 WHERE id IN (?, ?)", (old, kept))
    with db.read() as c:
        f = Figures(c, TODAY)
        assert f.month("2026-10")["summary"]["cash"] == {"total": 102_500, "accounts": ["Chequing", "Closed With Money"]}
        assert f.balances()["cash"] == 102_500
    with db.write() as c:
        ledger.create_account(c, "New Savings", "savings")
    with db.read() as c:
        f = Figures(c, TODAY)
        assert f.month("2026-10")["summary"]["cash"] is None and f.month("2026-10")["summary"]["off"] is None
        assert f.balances()["cash"] is None
        assert f.without_balance == ["New Savings"]
