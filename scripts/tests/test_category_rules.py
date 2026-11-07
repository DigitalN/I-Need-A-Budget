"""Which categories a transaction can go in, checked in Python and not only by what the page
offers (a card's payment category, a hidden category, one that isn't there). Made-up data only."""

from pathlib import Path

import pytest

import ledger
import subscriptions
from errors import UserError
from helpers import call, categories
from test_inbox import Budget


@pytest.fixture
def b(tmp_path):
    return Budget(tmp_path / "budget.db")


def _hide(b, name):
    b.c.execute("UPDATE categories SET hidden = 1 WHERE id = ?", (b.cat[name],))


def test_a_transaction_cant_go_in_a_payment_category_a_hidden_one_or_one_that_isnt_there(b):
    """Issue #66: a -$40.00 card charge filed in the card's own payment category threw the check line off."""
    charge = b.arrive(b.card, "2026-08-10", -4000, "SAMPLE ARCADE")
    payment = ledger.payment_categories(b.c)[b.card]
    with pytest.raises(UserError, match="card's payments"):
        b.inbox.categorize([charge], payment)
    with pytest.raises(UserError, match="no longer exists"):
        b.inbox.categorize([charge], 99999)
    _hide(b, "Fun Money")
    with pytest.raises(UserError, match="Fun Money is hidden"):
        b.inbox.categorize([charge], b.cat["Fun Money"])
    row = b.get(charge)
    assert (row["status"], row["category_id"], row["by_you"]) == ("review", None, 0)  # refused: nothing changed
    b.inbox.categorize([charge], b.cat["Food"])
    assert b.get(charge)["category_id"] == b.cat["Food"]


def test_a_split_has_the_same_rule_for_every_part(b):
    charge = b.arrive(b.card, "2026-08-10", -4000, "SAMPLE ARCADE")
    payment = ledger.payment_categories(b.c)[b.card]
    parts = [{"category_id": b.cat["Food"], "amount": -1000}, {"category_id": payment, "amount": -3000}]
    with pytest.raises(UserError, match="card's payments"):
        b.inbox.split(charge, parts)
    parts[1]["category_id"] = 99999
    with pytest.raises(UserError, match="no longer exists"):
        b.inbox.split(charge, parts)
    row = b.get(charge)
    assert (row["is_split"], row["status"]) == (0, "review")
    assert b.c.execute("SELECT COUNT(*) FROM splits").fetchone()[0] == 0


def test_a_recurring_charge_whose_category_is_hidden_waits_in_the_inbox(b):
    first = b.arrive(b.card, "2026-08-10", -1699, "SAMPLE STREAMING TORONTO ON")
    b.inbox.categorize([first], b.cat["Fun Money"])
    subscriptions.create(b.c, b.get(first)["payee_id"], "Music", 1699, category_id=b.cat["Fun Money"])
    second = b.arrive(b.card, "2026-09-10", -1699, "SAMPLE STREAMING TORONTO ON")
    assert (b.get(second)["status"], b.get(second)["category_id"]) == ("done", b.cat["Fun Money"])
    _hide(b, "Fun Money")
    third = b.arrive(b.card, "2026-10-10", -1699, "SAMPLE STREAMING TORONTO ON")
    row = b.get(third)
    assert (row["status"], row["category_id"]) == ("review", None)
    assert row["reason"] == "Music's category is hidden"
    assert b.item(third)["reason"].startswith("Music's category is hidden")
    b.launch()  # the re-check leaves it waiting
    assert b.get(third)["status"] == "review"


def test_a_charge_added_by_hand_goes_through_the_inboxs_rules(b):
    added = b.inbox.add_by_hand(b.chequing, "2026-10-05", "Sample Streaming", -2999, b.cat["Subscriptions"])
    row = b.get(added.id)
    assert (row["status"], row["category_id"], row["by_you"], row["reason"]) == \
        ("done", b.cat["Subscriptions"], 1, "Added by you")
    assert (added.question["txn_id"], added.question["amount"], added.question["category"]) == \
        (added.id, 2999, "Subscriptions")
    plain = b.inbox.add_by_hand(b.chequing, "2026-10-06", "Sample Cafe", -450, b.cat["Food"])
    assert plain.question is None and b.get(plain.id)["category_id"] == b.cat["Food"]
    waiting = b.inbox.add_by_hand(b.chequing, "2026-10-07", "Sample Shop", -1000, None, "a note")
    row = b.get(waiting.id)
    assert (row["status"], row["by_you"], row["note"], row["category_id"]) == ("review", 1, "a note", None)
    payment = ledger.payment_categories(b.c)[b.card]
    before = b.c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    with pytest.raises(UserError, match="card's payments"):
        b.inbox.add_by_hand(b.card, "2026-10-08", "Sample Arcade", -4000, payment)
    assert b.c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before  # nothing was added


def test_the_service_checks_categories_on_every_path(app):
    ids = categories(app, {"Everyday": ["Food", "Subscriptions"]})
    cheq = call(app, "add_account", "Chequing", "chequing", 100000, "2026-09-30")
    card = call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    payment = ledger.payment_categories(app.db.conn)[card]
    charge = call(app, "add_transaction", card, "2026-10-05", "SAMPLE ARCADE", -4000)["id"]

    def refused(request, *args):
        result = app.dispatch(request, list(args))
        assert not result["ok"], request
        return result["error"]

    assert "card's payments" in refused("add_transaction", card, "2026-10-06", "SAMPLE ARCADE", -4000, payment)
    assert "card's payments" in refused("update_transaction", charge, {"category_id": payment})
    assert "no longer exists" in refused("update_transaction", charge, {"category_id": 99999})
    assert "card's payments" in refused("subscription_from_transaction", charge, "Arcade", payment)
    assert "card's payments" in refused("add_subscription", "Sample Arcade", "Arcade", 1000, "monthly", "subscription", payment)
    sub = call(app, "add_subscription", "Sample Arcade", "Arcade", 1000, "monthly", "subscription", ids["Food"])
    assert "card's payments" in refused("update_subscription", sub, {"category_id": payment})
    call(app, "set_category_hidden", ids["Food"], True)
    assert "hidden" in refused("update_subscription", sub, {"category_id": ids["Food"]})
    assert "hidden" in refused("add_transaction", cheq, "2026-10-06", "SAMPLE CAFE", -450, ids["Food"])


def test_a_hand_added_charge_in_a_subscriptions_category_asks_whats_it_for(app):
    ids = categories(app, {"Everyday": ["Food", "Subscriptions"]})
    cheq = call(app, "add_account", "Chequing", "chequing", 100000, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    added = call(app, "add_transaction", cheq, "2026-10-05", "Sample Streaming", -2999, ids["Subscriptions"])
    assert added["question"]["amount"] == 2999 and added["question"]["txn_id"] == added["id"]
    plain = call(app, "add_transaction", cheq, "2026-10-06", "Sample Cafe", -450, ids["Food"])
    assert plain["question"] is None


def test_the_add_a_transaction_pop_up_shows_the_question():
    page = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")
    adding = page[page.index("async function addTransaction()"):]
    adding = adding[:adding.index("// ── accounts")]
    assert "const r = await call('add_transaction'" in adding and "if (r.question) { await subscriptionPrompt(r.question)" in adding
