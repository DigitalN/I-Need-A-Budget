"""A charge more than 3× the payee's biggest past charge always asks (above a small floor). Driven at
the Inbox level, like test_inbox.py. Made-up data only."""

import pytest

import learn
import subscriptions
from test_inbox import Budget


@pytest.fixture
def b(tmp_path):
    return Budget(tmp_path / "budget.db")


def _history(b, account, text, amounts, category, day=1):
    ids = []
    for i, amount in enumerate(amounts):
        t = b.arrive(account, f"2026-05-{day + i:02d}", amount, text)
        b.inbox.categorize([t], category)
        ids.append(t)
    return ids


def test_a_charge_over_three_times_the_biggest_asks_even_when_small(b):
    """Issue #64: there was a $50 floor that isn't in the spec: -$45.00 after at most -$9.00 was filed."""
    _history(b, b.card, "SAMPLE DELI CALGARY AB", [-800, -900, -700], b.cat["Food"])
    big = b.arrive(b.card, "2026-06-10", -4500, "SAMPLE DELI CALGARY AB")
    assert (b.get(big)["status"], b.get(big)["category_id"]) == ("review", None)
    item = b.item(big)
    assert "Much bigger than usual" in item["reason"] and item["group"] == "" and item["guess"] is None
    _history(b, b.card, "SAMPLE COFFEE CALGARY AB", [-400, -400, -400], b.cat["Food"], day=11)
    assert b.get(b.arrive(b.card, "2026-06-11", -4900, "SAMPLE COFFEE CALGARY AB"))["status"] == "review"


def test_the_small_floor_keeps_pocket_change_from_asking(b):
    """Documented: a charge has to be over $10.00 as well (learn.UNUSUAL_FLOOR) to count as unusual."""
    assert learn.UNUSUAL_FLOOR == 1000
    _history(b, b.card, "SAMPLE KIOSK CALGARY AB", [-200, -200, -200], b.cat["Food"])
    assert b.get(b.arrive(b.card, "2026-06-10", -950, "SAMPLE KIOSK CALGARY AB"))["status"] == "done"


def test_money_in_over_three_times_the_biggest_asks_too(b):
    _history(b, b.chequing, "SAMPLE REFUNDS DESK CALGARY AB", [900, 700, 800], b.cat["Food"])
    assert b.get(b.arrive(b.chequing, "2026-06-10", 4500, "SAMPLE REFUNDS DESK CALGARY AB"))["status"] == "review"


def test_a_fluctuates_every_time_bill_still_asks_about_a_huge_one(b):
    """Issue #64: the recurring-charge path ran first, so a -$900.00 power bill was filed as "Power subscription"."""
    first, *_ = _history(b, b.chequing, "SAMPLE POWER CO", [-6500, -8000, -7500], b.cat["Household"])
    subscriptions.create(b.c, b.get(first)["payee_id"], "Power", 7500, category_id=b.cat["Household"], pricing="varies")
    normal = b.arrive(b.chequing, "2026-06-03", -8200, "SAMPLE POWER CO")
    assert (b.get(normal)["status"], b.get(normal)["reason"]) == ("done", "Power subscription")
    huge = b.arrive(b.chequing, "2026-07-03", -90000, "SAMPLE POWER CO")
    assert (b.get(huge)["status"], b.get(huge)["category_id"]) == ("review", None)
    assert "Much bigger than usual" in b.item(huge)["reason"] and b.item(huge)["guess"] is None
    b.launch()
    assert b.get(huge)["status"] == "review"


def test_a_mixed_payees_huge_charge_is_its_own_item_and_not_in_accept_all(b):
    _history(b, b.card, "SAMPLE WAREHOUSE CALGARY AB", [-5000, -6000, -5500], b.cat["Food"])
    _history(b, b.card, "SAMPLE WAREHOUSE CALGARY AB", [-4500, -7000, -6500], b.cat["Household"], day=11)
    other = b.arrive(b.card, "2026-06-02", -5200, "SAMPLE WAREHOUSE CALGARY AB")  # an ordinary one of this mixed payee
    huge = b.arrive(b.card, "2026-06-03", -225000, "SAMPLE WAREHOUSE CALGARY AB")
    item = b.item(huge)
    assert item["group"] == "" and item["guess"] is None and "Much bigger than usual" in item["reason"]
    assert b.item(other)["group"] != ""  # the payee's other items stay grouped
    b.inbox.accept_all([huge, other])
    assert b.get(huge)["status"] == "review"
