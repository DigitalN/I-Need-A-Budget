"""Recurring charges: a cancelled one at a new price, prices you chose, the 6% wobble, and the day
the next charge is due. Made-up data only."""

import pytest

import subscriptions
from test_inbox import Budget


@pytest.fixture
def b(tmp_path):
    return Budget(tmp_path / "budget.db")


def _sub(b, text, name, price, day="2026-08-04", **kw):
    old = b.add(b.card, day, -price, text)
    sub = subscriptions.create(b.c, b.get(old)["payee_id"], name, price, category_id=b.cat["Subscriptions"], **kw)
    b.c.execute("UPDATE transactions SET subscription_id = ?, status = 'done', category_id = ? WHERE id = ?",
                (sub, b.cat["Subscriptions"], old))
    return sub, old


def test_a_cancelled_subscription_charging_a_new_price_is_flagged(b):
    """Issue #65: $18.99 after a cancelled $16.99 was categorized from history, with no warning."""
    sub, _ = _sub(b, "SAMPLE STREAMING TORONTO ON", "Streaming", 1699)
    b.c.execute("UPDATE subscriptions SET status = 'cancelled', cancelled_on = '2026-08-20' WHERE id = ?", (sub,))
    again = b.arrive(b.card, "2026-09-04", -1899, "SAMPLE STREAMING TORONTO ON")
    row = b.get(again)
    assert (row["status"], row["category_id"]) == ("review", None)
    assert "charged you again" in b.item(again)["reason"] and b.item(again)["group"] == ""
    b.launch()
    assert b.get(again)["status"] == "review"
    # Reactivated, the same charge is a question about a new price instead.
    b.c.execute("UPDATE subscriptions SET status = 'active', cancelled_on = NULL WHERE id = ?", (sub,))
    assert "charged you again" not in b.item(again)["reason"]


def test_a_subscription_past_its_end_date_charging_a_new_price_is_flagged_too(b):
    sub, _ = _sub(b, "SAMPLE GYM TORONTO ON", "Gym", 4000)
    b.c.execute("UPDATE subscriptions SET ends_on = '2026-08-31' WHERE id = ?", (sub,))
    again = b.arrive(b.card, "2026-09-04", -4500, "SAMPLE GYM TORONTO ON")
    assert "charged you again" in b.item(again)["reason"] and b.get(again)["status"] == "review"


def test_an_active_subscription_beside_a_cancelled_one_isnt_a_charged_again_warning(b):
    cancelled, _ = _sub(b, "APPLE.COM/BILL TORONTO ON", "Old plan", 1099)
    _sub(b, "APPLE.COM/BILL TORONTO ON", "Music", 1699, day="2026-08-05")
    b.c.execute("UPDATE subscriptions SET status = 'cancelled', cancelled_on = '2026-08-20' WHERE id = ?", (cancelled,))
    other = b.arrive(b.card, "2026-09-04", -3899, "APPLE.COM/BILL TORONTO ON")
    assert "charged you again" not in b.item(other)["reason"]


def test_prices_you_chose_dont_turn_same_price_into_currency(b):
    """Issue #65: $16.49 → $18.99 → $20.99, each chosen as "New price for…", counted as currency wobble."""
    sub, first = _sub(b, "SAMPLE STREAMING TORONTO ON", "Streaming", 1649, day="2026-06-04")
    for day, price in (("2026-07-04", 1899), ("2026-08-04", 2099)):
        charge = b.arrive(b.card, day, -price, "SAMPLE STREAMING TORONTO ON")
        b.inbox.new_price(charge, sub)
    with b.c:
        row = b.c.execute("SELECT * FROM subscriptions WHERE id = ?", (sub,)).fetchone()
        assert subscriptions.effective_pricing(b.c, row) == "fixed"
    asked = b.arrive(b.card, "2026-09-04", -2199, "SAMPLE STREAMING TORONTO ON")
    assert b.get(asked)["status"] == "review" and b.get(asked)["subscription_id"] is None
    assert b.c.execute("SELECT amount FROM subscriptions WHERE id = ?", (sub,)).fetchone()[0] == 2099  # not followed


def test_small_drifts_are_still_currency(b):
    charges = [{"amt": a, "bank_category": ""} for a in (1650, 1672, 1641, 1688)]
    assert subscriptions.looks_foreign(charges)  # more than one move, each a few percent
    assert not subscriptions.looks_foreign([{"amt": a, "bank_category": ""} for a in (1649, 1899, 2099)])


def test_the_wobble_is_measured_against_the_old_price(b):
    assert subscriptions.same_price_for("currency", 1000, 1060)
    assert not subscriptions.same_price_for("currency", 1000, 1063)  # +6.3%: it passed against the larger amount
    assert subscriptions.same_price_for("currency", 1000, 940) and not subscriptions.same_price_for("currency", 1000, 930)
    assert not subscriptions.close(1000, 1063) and subscriptions.close(1000, 1060)


def test_the_next_charge_follows_the_last_charges_day_unless_a_short_month_moved_it():
    assert subscriptions.next_due("2026-02-28", "monthly", ["2026-01-31"]) == "2026-03-31"
    assert subscriptions.next_due("2026-01-31", "monthly") == "2026-02-28"
    assert subscriptions.next_due("2026-03-15", "monthly") == "2026-04-15"  # no earlier charges: the last one's day
    assert subscriptions.next_due("2026-04-30", "monthly", ["2026-01-31", "2026-03-31"]) == "2026-05-31"
    assert subscriptions.next_due("2026-04-28", "monthly", ["2026-01-31"]) == "2026-05-28"  # not on a month's last day


def test_a_subscription_charged_on_the_31st_is_due_on_the_31st_after_february(b):
    sub, first = _sub(b, "SAMPLE HOSTING TORONTO ON", "Hosting", 1500, day="2026-01-31")
    feb = b.add(b.card, "2026-02-28", -1500, "SAMPLE HOSTING TORONTO ON")
    b.c.execute("UPDATE transactions SET subscription_id = ?, status = 'done' WHERE id = ?", (sub, feb))
    tracked = next(s for s in subscriptions.listing(b.c, "2026-03-05")["subscriptions"] if s["id"] == sub)
    assert tracked["next_date"] == "2026-03-31"
