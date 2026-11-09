# SPDX-License-Identifier: GPL-3.0-only
"""A new account doesn't hide Usual (issue #83): one added after the budget was set up, whose
first transaction is within a month of when it was added, has no older history to miss, so it
doesn't move the first month every account has data for (Figures.coverage). Accounts from
onboarding keep that rule: there, a card with one month downloaded really is missing history.
Made-up data only."""

import ledger
from db import Database
from figures import Figures
from helpers import call

TODAY = "2026-10-12"  # conftest's INAB_TODAY
FOOD = {"2026-06": -40000, "2026-07": -42000, "2026-08": -38000, "2026-09": -40000}


class Budget:
    """Chequing from onboarding, with Food spending from June; the budget starts in July."""

    def __init__(self, app, onboarding=()):
        self.app = app
        gid = call(app, "add_group", "Everyday")
        self.food = call(app, "add_category", gid, "Food")
        self.chequing = call(app, "add_account", "Chequing", "chequing", 500000, "2026-05-31")
        for month, cents in FOOD.items():
            self.spend(self.chequing, f"{month}-10", cents)
        for step in onboarding:  # more of setting up, before it's finished
            step(self)
        call(app, "finish_onboarding", "2026-07")

    def spend(self, account, day, cents):
        call(self.app, "add_transaction", account, day, "Sample Grocer", cents, self.food)

    def card(self, added_on=None, monkeypatch=None):
        """A card added on `added_on` (default today)."""
        if added_on:
            monkeypatch.setenv("INAB_TODAY", added_on)
        card = call(self.app, "add_account", "New Card", "credit_card", 0, added_on or TODAY)
        if added_on:
            monkeypatch.setenv("INAB_TODAY", TODAY)
        return card

    def usual(self, month="2026-10"):
        return next(c for g in call(self.app, "month", month)["groups"] for c in g["categories"]
                    if c["id"] == self.food)["usual"]

    def whole_months(self):
        """Which of the Dashboard's months (July to October) count as whole months with data for every account."""
        return [m["complete"] for m in call(self.app, "dashboard", 3)["spending"]["months"]]


def test_a_new_card_this_month_doesnt_hide_usual(app):
    """Issue #83: Food's Usual went from $400.00 to nothing when a card opened this month."""
    b = Budget(app)
    assert b.usual() == 40000 and b.whole_months() == [True] * 4
    card = b.card()
    b.spend(card, "2026-10-05", -2500)
    assert b.usual() == 40000
    assert b.whole_months() == [True] * 4  # the Dashboard's whole months, and so its "above usual" marks, stay
    history = call(app, "category_detail", b.food, "2026-10")
    assert [h["complete"] for h in history["history"] if "2026-07" <= h["month"] <= "2026-09"] == [True] * 3
    assert history["averages"][3] == {"months": 3, "amount": 40000}  # the pop-over's Last 3 months


def test_a_card_added_months_ago_with_no_older_history_counts_as_new_too(app, monkeypatch):
    """Added in August with August's charges: July is a whole month all the same (the card didn't
    exist). Today's rule would have averaged August and September only."""
    b = Budget(app)
    card = b.card("2026-08-10", monkeypatch)
    b.spend(card, "2026-08-03", -5000)
    b.spend(card, "2026-09-03", -5000)
    assert b.usual() == round((42000 + 43000 + 45000) / 3)
    assert b.whole_months() == [True] * 4


def test_a_card_from_onboarding_keeps_todays_rule(app):
    """Set up with a card whose download only goes back to October: its older history is missing,
    so no month before October is a whole month."""
    b = Budget(app, onboarding=[lambda b: b.spend(b.card(), "2026-10-02", -2500)])
    assert b.usual() is None
    assert b.whole_months() == [False, False, False, True]


def test_an_old_account_added_late_with_its_history_still_counts(app):
    """Added today with charges from August (more than a month before): it has older history, and
    the months before its first are missing it, as today."""
    b = Budget(app)
    card = b.card()
    b.spend(card, "2026-08-01", -5000)
    assert b.usual() == round((43000 + 40000) / 2)
    assert b.whole_months() == [False, True, True, True]


def test_finishing_onboarding_remembers_its_accounts(app):
    b = Budget(app)
    assert app.db.get_meta("last_onboarding_account") == str(b.chequing)


def test_a_budget_set_up_before_this_counts_every_account_it_has_as_from_onboarding(tmp_path):
    """It doesn't know which accounts onboarding made: all of those it has when it's opened."""
    db = Database(tmp_path / "b.db")
    with db.write() as c:
        old = ledger.create_account(c, "Card", "credit_card")
        c.execute("INSERT INTO meta (key, value) VALUES ('onboarded', '1')")
        c.execute("INSERT INTO transactions (account_id, date, amount) VALUES (?, '2026-10-02', -100)", (old,))
    db.close()
    db = Database(tmp_path / "b.db")
    assert db.get_meta("last_onboarding_account") == str(old)
    with db.write() as c:
        new = ledger.create_account(c, "Chequing", "chequing")
        c.execute("INSERT INTO transactions (account_id, date, amount) VALUES (?, '2026-09-20', -100)", (new,))
        assert Figures(c, TODAY).coverage == "2026-10"  # the old card's, as before; the new account doesn't move it
    db.close()
    assert Database(tmp_path / "b.db").get_meta("last_onboarding_account") == str(old)  # written once


def test_an_account_is_added_on_the_apps_today(tmp_path):
    """INAB_TODAY pins today for the dev preview and the tests: an account's created_at follows it,
    so "within a month of when it was added" compares like with like."""
    db = Database(tmp_path / "b.db")
    with db.write() as c:
        account = ledger.create_account(c, "Wallet", "cash")
        assert c.execute("SELECT created_at FROM accounts WHERE id = ?", (account,)).fetchone()[0][:10] == TODAY
    db.close()
