# SPDX-License-Identifier: GPL-3.0-only
"""Changing the amount of a transaction added by hand. Made-up data only."""

from helpers import CARD_FILE, call, card_csv, write


def _add(app, *args):
    """add_transaction's id (it returns {'id', 'question'})."""
    return call(app, "add_transaction", *args)["id"]


def _budget(app):
    gid = call(app, "add_group", "Everyday")
    food, house = call(app, "add_category", gid, "Food"), call(app, "add_category", gid, "Household")
    cheq = call(app, "add_account", "Chequing", "chequing", 100000, "2026-09-30")
    call(app, "finish_onboarding", "2026-10")
    return food, house, cheq


def _split(app):
    food, house, cheq = _budget(app)
    t = _add(app, cheq, "2026-10-05", "SAMPLE HARDWARE", -5000, food)
    call(app, "split_transaction", t, [{"category_id": food, "amount": -2000}, {"category_id": house, "amount": -3000}])
    return t, food, house, cheq


def _amount(app, txn_id):
    return next(x for x in call(app, "transactions", {})["items"] if x["id"] == txn_id)["amount"]


def test_a_split_keeps_adding_up_when_its_amount_changes(app):
    """Issue #52: the lines stayed at -$50.00 while the account counted -$80.00, so the check
    line under Ready to assign went $30.00 off."""
    t, *_ = _split(app)
    refused = app.dispatch("update_transaction", [t, {"amount": -8000}])
    assert not refused["ok"] and "Unsplit it first" in refused["error"]
    assert _amount(app, t) == -5000
    assert call(app, "month", "2026-10")["summary"]["off"] == 0


def test_a_paired_transfer_keeps_matching_when_its_amount_changes(app):
    _, _, cheq = _budget(app)
    card = call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    payment = _add(app, cheq, "2026-10-06", "Card payment", -30000)
    other = _add(app, card, "2026-10-06", "Payment thank you", 30000)
    call(app, "update_transaction", payment, {"transfer_account_id": card})
    paired = {x["id"]: x for x in call(app, "transactions", {})["items"]}
    assert paired[payment]["transfer_id"] == other
    refused = app.dispatch("update_transaction", [payment, {"amount": -45000}])
    assert not refused["ok"] and "Not a transfer" in refused["error"]
    assert (_amount(app, payment), _amount(app, other)) == (-30000, 30000)


def test_the_amount_can_change_again_once_it_isnt_a_split_or_a_transfer(app):
    t, food, house, cheq = _split(app)
    call(app, "unsplit_transaction", t)
    call(app, "update_transaction", t, {"amount": -8000, "date": "2026-10-07"})
    assert _amount(app, t) == -8000
    # A split's date can change: only its amount has to stay with its lines.
    call(app, "update_transaction", t, {"category_id": food})
    call(app, "split_transaction", t, [{"category_id": food, "amount": -5000}, {"category_id": house, "amount": -3000}])
    call(app, "update_transaction", t, {"date": "2026-10-08"})
    assert call(app, "month", "2026-10")["summary"]["off"] == 0
    # A transfer that was only marked (its other half isn't imported yet: the card gets files, so it
    # comes with the next one) has nothing to match. A card kept by hand would get its half (issue #75).
    card = call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    path = write(app.test_downloads, CARD_FILE, card_csv([("10/02/2026", "SAMPLE STORE", "-5.00", "")]))
    call(app, "import_files", [str(path)])
    call(app, "set_file_accounts", str(path), [{"index": 0, "account_id": card}])
    lone = _add(app, cheq, "2026-10-09", "Card payment", -10000)
    call(app, "update_transaction", lone, {"transfer_account_id": card})
    call(app, "update_transaction", lone, {"amount": -12000})
    assert _amount(app, lone) == -12000
