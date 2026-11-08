# SPDX-License-Identifier: GPL-3.0-only
"""Always ask is always: an Always-ask payee's item waits, whatever else the app knows about it (an
exact refund, a tracked recurring charge, a share of a bill), and shares group only different people.
Driven at the Inbox level, like test_inbox.py. Made-up data only."""

import pytest

import subscriptions
from test_inbox import Budget


@pytest.fixture
def b(tmp_path):
    return Budget(tmp_path / "budget.db")


def _always_ask(b, txn_id):
    b.c.execute("UPDATE payees SET always_ask = 1 WHERE id = ?", (b.get(txn_id)["payee_id"],))


def test_an_exact_refund_from_an_always_ask_payee_waits(b):
    """Issue #62: $45.99 back after a $45.99 purchase was categorized without asking."""
    purchase = b.arrive(b.card, "2026-09-02", -4599, "SAMPLE HOMEWARES TORONTO ON")
    b.inbox.categorize([purchase], b.cat["Household"])
    _always_ask(b, purchase)
    back = b.arrive(b.card, "2026-09-12", 4599, "SAMPLE HOMEWARES TORONTO ON")
    row = b.get(back)
    assert (row["status"], row["category_id"]) == ("review", None)
    item = b.item(back)
    assert item["suggestions"][0]["name"] == "Household"  # still the top suggestion
    assert item["reason"].startswith("Refund of the") and "Household" in item["reason"]
    assert item["group"] == ""  # its own item
    b.launch()
    assert b.get(back)["status"] == "review"  # the re-check leaves it too
    # Not Always ask: the same refund is still filed on its own.
    b.c.execute("UPDATE payees SET always_ask = 0 WHERE id = ?", (b.get(purchase)["payee_id"],))
    again = b.arrive(b.card, "2026-09-20", 4599, "SAMPLE HOMEWARES TORONTO ON")
    assert b.get(again)["status"] == "done"


def test_a_tracked_recurring_charge_from_an_always_ask_payee_waits(b):
    first = b.arrive(b.card, "2026-08-10", -1699, "SAMPLE STREAMING TORONTO ON")
    b.inbox.categorize([first], b.cat["Fun Money"])
    sub = subscriptions.create(b.c, b.get(first)["payee_id"], "Music", 1699, category_id=b.cat["Fun Money"])
    ordinary = b.arrive(b.card, "2026-09-10", -1699, "SAMPLE STREAMING TORONTO ON")
    assert b.get(ordinary)["status"] == "done"  # tracked and not Always ask: filed
    _always_ask(b, first)
    later = b.arrive(b.card, "2026-10-10", -1699, "SAMPLE STREAMING TORONTO ON")
    row = b.get(later)
    assert (row["status"], row["category_id"], row["subscription_id"]) == ("review", None, sub)
    item = b.item(later)
    assert item["suggestions"][0]["name"] == "Fun Money"
    assert item["reason"].startswith("Music subscription") and item["group"] == ""
    b.launch()
    assert b.get(later)["status"] == "review"


def test_rent_etransferred_to_a_landlord_with_always_ask_on_waits_with_the_bill_as_its_suggestion(b):
    first = b.arrive(b.chequing, "2026-08-01", -150000, "INTERAC e-Transfer To: SAMPLE LANDLORD")
    b.inbox.categorize([first], b.cat["Household"])
    _always_ask(b, first)
    sub = subscriptions.create(b.c, b.get(first)["payee_id"], "Rent", 150000, category_id=b.cat["Household"], kind="bill")
    rent = b.arrive(b.chequing, "2026-09-01", -150000, "INTERAC e-Transfer To: SAMPLE LANDLORD")
    assert (b.get(rent)["status"], b.get(rent)["subscription_id"]) == ("review", sub)
    assert b.item(rent)["suggestions"][0]["name"] == "Household"


def test_two_shares_from_one_payee_arent_grouped_as_one_shared_expense(b):
    """Issue #62: two $50.00 e-transfers from the same payee 9 days apart, near a $100.00 purchase."""
    b.arrive(b.card, "2026-09-10", -10000, "SAMPLE WAREHOUSE CALGARY AB")
    one = b.arrive(b.chequing, "2026-09-12", 5000, "INTERAC e-Transfer From: ALEX SAMPLE")
    two = b.arrive(b.chequing, "2026-09-21", 5000, "INTERAC e-Transfer From: ALEX SAMPLE")
    groups = [b.item(one)["group"], b.item(two)["group"]]
    assert sum(g.startswith("share:") for g in groups) <= 1  # one at most carries the share group: never both together
    assert len({g for g in groups if g}) == len([g for g in groups if g])
    assert groups.count("") >= 1


def test_shares_from_different_people_still_group(b):
    b.arrive(b.card, "2026-09-10", -10000, "SAMPLE WAREHOUSE CALGARY AB")
    one = b.arrive(b.chequing, "2026-09-12", 5000, "INTERAC e-Transfer From: ALEX SAMPLE")
    two = b.arrive(b.chequing, "2026-09-13", 5000, "INTERAC e-Transfer From: JORDAN SAMPLE")
    groups = {b.item(one)["group"], b.item(two)["group"]}
    assert len(groups) == 1 and next(iter(groups)).startswith("share:")
