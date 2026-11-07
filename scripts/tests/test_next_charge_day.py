"""A monthly charge's next date follows the day of its last charge (issue #125): billing days move,
and a first bill is often part of a month. Made-up data only."""

import pytest

import subscriptions
from test_inbox import Budget


@pytest.fixture
def b(tmp_path):
    return Budget(tmp_path / "budget.db")


def _bill(b, days, text="SAMPLE POWER CO", price=8000):
    first = b.add(b.card, days[0], -price, text)
    sub = subscriptions.create(b.c, b.get(first)["payee_id"], "Power", price, category_id=b.cat["Subscriptions"])
    for d in days:
        t = first if d == days[0] else b.add(b.card, d, -price, text)
        b.c.execute("UPDATE transactions SET subscription_id = ?, status = 'done', category_id = ? WHERE id = ?",
                    (sub, b.cat["Subscriptions"], t))
    return sub


def _next(b, sub, today):
    listing = subscriptions.listing(b.c, today)
    s = next(s for s in listing["subscriptions"] + listing["bills"] if s["id"] == sub)
    return s["next_date"], s["overdue"]


def test_a_partial_first_bill_doesnt_set_the_day(b):
    sub = _bill(b, ["2026-06-09", "2026-07-20", "2026-08-21", "2026-09-20", "2026-10-20"])
    assert _next(b, sub, "2026-11-18") == ("2026-11-20", False)


def test_a_bill_whose_day_moved_is_due_on_its_new_day(b):
    sub = _bill(b, ["2026-05-26", "2026-06-24", "2026-07-21", "2026-08-18", "2026-09-19", "2026-10-19"])
    assert _next(b, sub, "2026-11-07")[0] == "2026-11-19"


def test_the_31st_is_still_the_31st_after_february(b):
    """Issue #65's case, which must keep working."""
    sub = _bill(b, ["2025-12-31", "2026-01-31", "2026-02-28"])
    assert _next(b, sub, "2026-03-05")[0] == "2026-03-31"


def test_the_rule_in_one_place():
    due = subscriptions.next_due
    assert due("2026-02-28", "monthly", ["2025-12-31", "2026-01-31"]) == "2026-03-31"  # a short month moved it
    assert due("2026-02-28", "monthly", ["2026-01-30"]) == "2026-03-30"
    assert due("2026-03-30", "monthly", ["2026-01-30", "2026-02-28"]) == "2026-04-30"  # the 30th is not a month's last day in March
    assert due("2026-04-30", "monthly", ["2026-01-31", "2026-02-28", "2026-03-31"]) == "2026-05-31"
    assert due("2026-04-28", "monthly", ["2026-01-31"]) == "2026-05-28"  # not on a month's last day: its own day
    assert due("2026-01-31", "monthly") == "2026-02-28"  # clamped to the next month's end
    assert due("2026-06-30", "monthly", ["2026-05-31", "2026-04-30"]) == "2026-07-31"
    assert due("2026-02-28", "monthly", ["2026-01-15"]) == "2026-03-28"  # no earlier charge on a later day
    assert due("2026-03-15", "monthly") == "2026-04-15"


def test_the_price_change_question_uses_the_same_date(b):
    sub = _bill(b, ["2026-06-09", "2026-07-20", "2026-08-20", "2026-09-20", "2026-10-20"])
    new = b.arrive(b.card, "2026-11-20", -9500, "SAMPLE POWER CO")
    choices = subscriptions.price_change_choices(b.c, b.get(new))
    assert [(c["id"], c["expected"], c["due_now"]) for c in choices] == [(sub, "2026-11-20", True)]
