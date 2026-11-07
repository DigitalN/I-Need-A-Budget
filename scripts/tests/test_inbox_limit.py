"""The Inbox page shows every item waiting, however many. Made-up data only."""

import time

from helpers import call


def _waiting(app, n):
    wallet = call(app, "add_account", "Wallet", "cash", 100000, "2026-08-31")
    with app.db.write() as c:
        for i in range(n):
            c.execute("INSERT INTO transactions (account_id, date, amount, status) VALUES (?, ?, -100, 'review')",
                      (wallet, f"2026-10-{1 + i % 28:02d}"))
    return wallet


def test_the_inbox_shows_every_item(app):
    """Issue #56: the count said 1,100 and the page showed 1,000: the oldest 100 couldn't be seen,
    found with the finder, or reached by Accept all."""
    _waiting(app, 1100)
    inbox = call(app, "inbox")
    assert len(inbox["items"]) == inbox["counts"]["inbox"] == 1100
    assert len({i["id"] for i in inbox["items"]}) == 1100


def test_the_oldest_items_are_there_to_be_accepted(app):
    _waiting(app, 1100)
    oldest = call(app, "transactions", {"status": "review", "limit": -1})["items"][-1]["id"]
    assert oldest in {i["id"] for i in call(app, "inbox")["items"]}


def test_a_big_inbox_still_loads_in_reasonable_time(app):
    _waiting(app, 1100)
    start = time.time()
    call(app, "inbox")
    assert time.time() - start < 20
