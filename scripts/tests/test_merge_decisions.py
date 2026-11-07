"""Merging a duplicate account keeps what you decided (docs/adr/0005): a copy dropped in favour
of its twin never costs a decision of yours, whichever of the two it was on. Driven at the
ledger/Inbox level, like test_inbox.py. Made-up data only."""

import pytest

import ledger
from test_inbox import Budget


@pytest.fixture
def b(tmp_path):
    budget = Budget(tmp_path / "budget.db")
    budget.copy = ledger.create_account(budget.c, "Chequing copy", "chequing")
    budget.wallet = ledger.create_account(budget.c, "Wallet", "cash")
    return budget


def _both(b, date, amount, text, copy_first=False):
    """The same bank row in Chequing and in its duplicate: (the original's id, the copy's id).
    Neither has arrived in the inbox yet; the one added first is found first by a transfer's
    other half."""
    if copy_first:
        copy = b.add(b.copy, date, amount, text)
        original = b.add(b.chequing, date, amount, text)
    else:
        original = b.add(b.chequing, date, amount, text)
        copy = b.add(b.copy, date, amount, text)
    b.c.execute("UPDATE transactions SET fingerprint = ? WHERE id = ?", (b.get(original)["fingerprint"], copy))
    return original, copy


def _merge(b):
    """What the service's Merge into… does."""
    moved, dropped, unlinked, asked = ledger.merge_accounts(b.c, b.copy, b.chequing)
    b.inbox.ask_again(unlinked, "No longer a transfer: both halves are in one account")
    b.inbox.ask_again(asked, "Its other half is a duplicate of one you decided about")
    return moved, dropped


def test_the_apps_pairing_of_the_copy_never_replaces_your_choice_on_the_original(b):
    """You put the original in Food; the app had paired its copy with the card's half."""
    original, copy = _both(b, "2026-08-10", -30000, "Tangerine Credit Card Payment")
    b.inbox.arrive([original])
    b.inbox.categorize([original], b.cat["Food"])
    card = b.arrive(b.card, "2026-08-11", 30000, "PAYMENT - THANK YOU")
    assert b.get(card)["transfer_id"] == copy and b.get(original)["transfer_id"] is None  # the app paired the copy
    assert _merge(b) == (0, 1)
    kept = b.get(original)
    assert (kept["category_id"], kept["by_you"], kept["transfer_id"], kept["transfer_account_id"]) == \
        (b.cat["Food"], 1, None, None)
    half = b.get(card)
    assert (half["status"], half["transfer_id"], half["transfer_account_id"]) == ("review", None, None)
    assert b.item(card)["reason"].startswith("Looks like the other half of")  # of the original, which you put in Food


def test_a_transfer_you_marked_on_the_copy_is_carried_to_the_original(b):
    original, copy = _both(b, "2026-08-10", -4000, "SAMPLE MACHINE WITHDRAWAL")
    b.inbox.arrive([original, copy])
    assert b.get(original)["status"] == "review"
    b.inbox.mark_transfer(copy, b.wallet)
    assert b.get(copy)["by_you"] and b.get(copy)["transfer_account_id"] == b.wallet
    _merge(b)
    kept = b.get(original)
    assert (kept["by_you"], kept["status"], kept["transfer_account_id"]) == (1, "done", b.wallet)
    assert b.waiting() == []


def test_not_a_transfer_and_food_on_the_copy_beat_the_originals_transfer(b):
    original, copy = _both(b, "2026-08-10", -30000, "Tangerine Credit Card Payment")
    b.inbox.arrive([original])
    card = b.arrive(b.card, "2026-08-11", 30000, "PAYMENT - THANK YOU")
    assert b.get(original)["transfer_id"] == card  # the app's: a transfer
    b.inbox.arrive([copy])
    b.inbox.not_a_transfer(copy)
    b.inbox.categorize([copy], b.cat["Food"])
    assert b.get(copy)["by_you"] and b.get(copy)["transfer_account_id"] is None
    _merge(b)
    kept = b.get(original)
    assert (kept["category_id"], kept["by_you"], kept["transfer_id"], kept["transfer_account_id"]) == \
        (b.cat["Food"], 1, None, None)
    half = b.get(card)  # its other half waits for you, and isn't paired with yours
    assert (half["status"], half["transfer_id"], half["transfer_account_id"]) == ("review", None, None)


def test_when_nothing_is_yours_the_original_is_paired_with_the_copys_other_half(b):
    """Neither is yours: the copy's pairing moves to the original (as before)."""
    original, copy = _both(b, "2026-08-10", -30000, "Tangerine Credit Card Payment", copy_first=True)
    b.inbox.arrive([copy])
    card = b.arrive(b.card, "2026-08-11", 30000, "PAYMENT - THANK YOU")
    assert b.get(card)["transfer_id"] == copy
    b.inbox.arrive([original])
    assert b.get(original)["transfer_id"] is None  # its guess only: the card's half has a partner already
    _merge(b)
    assert b.get(original)["transfer_id"] == card and b.get(card)["transfer_id"] == original


def test_an_original_the_app_paired_keeps_its_own_pairing(b):
    original, copy = _both(b, "2026-08-10", -30000, "Tangerine Credit Card Payment")
    b.inbox.arrive([original, copy])
    card = b.arrive(b.card, "2026-08-11", 30000, "PAYMENT - THANK YOU")
    assert b.get(card)["transfer_id"] == original
    _merge(b)
    assert b.get(original)["transfer_id"] == card and b.get(card)["transfer_id"] == original
