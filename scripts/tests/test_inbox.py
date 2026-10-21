"""The inbox, driven directly (inbox.Inbox): an inbox item from arrival until it's
categorized, split or a transfer, and what you decided staying decided (docs/adr/0005).
Made-up data only."""

import pytest

import ledger
import subscriptions
from db import Database
from inbox import Inbox
from payees import describe, transaction_hint

TODAY = "2026-10-12"


class Budget:
    """A throwaway database with a chequing account, a card and a few categories."""

    def __init__(self, path):
        self.db = Database(path)
        self.c = self.db.conn
        self.chequing = ledger.create_account(self.c, "Chequing", "chequing")
        self.card = ledger.create_account(self.c, "Mastercard", "credit_card")
        group = self.c.execute("INSERT INTO category_groups (name, sort) VALUES ('Everyday', 1)").lastrowid
        self.cat = {name: self.c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, name)).lastrowid
                    for name in ("Food", "Household", "Fun Money", "Subscriptions")}
        ledger.income_category(self.c)
        self.inbox = Inbox(self.c, TODAY)
        self.rows = 0

    def add(self, account, date, amount, text):
        """A bank row added as an import adds it (not yet arrived). Returns its id."""
        kind = self.c.execute("SELECT kind FROM accounts WHERE id = ?", (account,)).fetchone()["kind"]
        desc = describe(text, "", kind)
        payee_id, similar = ledger.resolve_payee(self.c, desc)
        self.rows += 1
        return self.c.execute(
            "INSERT INTO transactions (account_id, date, amount, payee_id, raw_name, hint, fingerprint, "
            "similar_payee_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account, date, amount, payee_id, text, transaction_hint(desc), f"test-{self.rows}", similar)).lastrowid

    def arrive(self, account, date, amount, text):
        txn_id = self.add(account, date, amount, text)
        self.inbox.arrive([txn_id])
        return txn_id

    def get(self, txn_id):
        return self.c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()

    def waiting(self):
        return [r["id"] for r in self.c.execute("SELECT id FROM transactions WHERE status = 'review' ORDER BY id")]

    def item(self, txn_id):
        return next(i for i in self.inbox.view()["items"] if i["id"] == txn_id)

    def launch(self):
        """What the app does at every launch: the re-check, on a fresh Inbox."""
        return Inbox(self.c, TODAY).recheck()


@pytest.fixture
def b(tmp_path):
    return Budget(tmp_path / "budget.db")


def card_payment(b):
    """A $300 card payment from chequing and the card's half of it, paired as a transfer."""
    cheq = b.arrive(b.chequing, "2026-08-10", -30000, "Tangerine Credit Card Payment")
    card = b.arrive(b.card, "2026-08-11", 30000, "PAYMENT - THANK YOU")
    assert b.get(cheq)["transfer_id"] == card and b.get(card)["transfer_id"] == cheq
    return cheq, card


# ── the two bugs this module was built for ──

def test_not_a_transfer_is_still_not_a_transfer_after_the_next_launch(b):
    cheq, card = card_payment(b)
    b.inbox.not_a_transfer(cheq)
    assert set(b.waiting()) == {cheq, card}  # both halves wait for you
    b.inbox.categorize([cheq], b.cat["Food"])
    b.launch()
    cheq_row, card_row = b.get(cheq), b.get(card)
    assert (cheq_row["category_id"], cheq_row["status"], cheq_row["transfer_id"]) == (b.cat["Food"], "done", None)
    assert (card_row["status"], card_row["transfer_id"], card_row["transfer_account_id"]) == ("review", None, None)
    assert card_row["by_you"] and b.item(card)["guess"] is None


def test_a_transaction_you_sent_back_stays_in_the_inbox(b):
    for day in ("07-01", "07-08", "07-15"):
        b.inbox.categorize([b.arrive(b.chequing, f"2026-{day}", -650, "SAMPLE CAFE CALGARY AB")], b.cat["Food"])
    fourth = b.arrive(b.chequing, "2026-07-22", -650, "SAMPLE CAFE CALGARY AB")
    assert (b.get(fourth)["status"], b.get(fourth)["by_you"]) == ("done", 0)  # the app was sure
    b.inbox.send_back(fourth)
    b.launch()
    assert b.waiting() == [fourth]
    item = b.item(fourth)
    assert (item["group"], item["guess"], item["yours"]) == ("", None, True)  # its own item, outside Accept all
    assert item["suggestions"][0]["name"] == "Food"  # still suggested
    assert item["reason"].startswith("You sent this back")


# ── yours and the app's ──

def test_what_you_decide_is_yours_and_what_the_app_decides_is_its_own(b):
    cafe = b.arrive(b.chequing, "2026-07-01", -650, "SAMPLE CAFE CALGARY AB")
    b.inbox.categorize([cafe], b.cat["Food"])
    again = b.arrive(b.chequing, "2026-07-08", -700, "SAMPLE CAFE CALGARY AB")
    shop = b.arrive(b.chequing, "2026-07-09", -2500, "SAMPLE HARDWARE CALGARY AB")
    b.inbox.split(shop, [{"category_id": b.cat["Household"], "amount": -2000},
                         {"category_id": b.cat["Fun Money"], "amount": -500}])
    assert [b.get(i)["by_you"] for i in (cafe, again, shop)] == [1, 0, 1]
    assert b.inbox.view()["categorized"] == []  # only what arrived with an import is listed there


def test_a_category_change_outside_the_inbox_changes_only_that_transaction(b):
    first = b.arrive(b.chequing, "2026-07-01", -1250, "SAMPLE BAKERY CALGARY AB")
    second = b.arrive(b.chequing, "2026-07-08", -1100, "SAMPLE BAKERY CALGARY AB")
    out = b.inbox.categorize([first], b.cat["Food"])  # the Transactions page's way
    assert (out.also, b.waiting()) == (0, [second])
    out = b.inbox.categorize([first], b.cat["Food"], recheck_payee=True)  # the inbox's way
    assert (out.also, b.waiting()) == (1, [])
    assert b.get(second)["by_you"] == 0  # also categorized: the app's


def test_categorizing_a_transfer_half_sends_its_other_half_back(b):
    cheq, card = card_payment(b)
    b.inbox.categorize([cheq], b.cat["Household"])
    assert b.get(cheq)["transfer_account_id"] is None
    assert b.waiting() == [card] and b.get(card)["by_you"]
    b.launch()
    assert b.waiting() == [card]  # never paired again


def test_a_transfer_half_arriving_for_one_you_categorized_waits_and_transfer_pairs_them(b):
    cheq = b.arrive(b.chequing, "2026-08-10", -30000, "Tangerine Credit Card Payment")
    b.inbox.categorize([cheq], b.cat["Food"])
    card = b.arrive(b.card, "2026-08-11", 30000, "PAYMENT - THANK YOU")
    row = b.get(card)
    assert (row["status"], row["transfer_account_id"], row["by_you"]) == ("review", None, 0)
    item = b.item(card)
    assert item["reason"] == ("Looks like the other half of the -$300.00 Chequing transaction on 2026-08-10, "
                              "which you put in Food")
    assert (item["group"], item["guess"]) == ("", None)
    b.launch()
    assert b.get(cheq)["category_id"] == b.cat["Food"] and b.waiting() == [card]
    b.inbox.mark_transfer(card, b.chequing)
    assert b.get(card)["transfer_id"] == cheq and b.get(cheq)["transfer_id"] == card
    assert b.get(cheq)["category_id"] is None and b.waiting() == []


def test_a_transfer_you_marked_still_pairs_when_its_other_half_arrives(b):
    cheq = b.arrive(b.chequing, "2026-08-10", -5000, "SAMPLE ONLINE TRANSFER")
    b.inbox.mark_transfer(cheq, b.card)
    card = b.arrive(b.card, "2026-08-11", 5000, "SAMPLE ONLINE TRANSFER")
    assert b.get(card)["transfer_id"] == cheq and b.get(cheq)["transfer_id"] == card


def test_removing_an_import_leaves_its_other_halves_to_the_app(b):
    cheq, card = card_payment(b)
    b.inbox.not_a_transfer(cheq)
    b.inbox.mark_transfer(cheq, b.card)  # yours, paired again
    assert b.get(cheq)["by_you"] and b.get(card)["by_you"]
    b.c.execute("DELETE FROM transactions WHERE id = ?", (card,))  # its import was removed
    b.inbox.ask_again([cheq], "Its other half was removed with its import")
    row = b.get(cheq)
    assert row["by_you"] == 0
    # The app decides again: a card payment whose other half isn't imported (any more).
    assert (row["status"], row["transfer_account_id"]) == ("done", b.card)


# ── worked out, not read back ──

def test_charged_you_again_is_worked_out_and_clears_when_its_reactivated(b):
    first = b.arrive(b.card, "2026-08-10", -1699, "SAMPLE STREAMING TORONTO ON")
    b.inbox.categorize([first], b.cat["Fun Money"])
    sub = subscriptions.create(b.c, b.get(first)["payee_id"], "Streaming", 1699, category_id=b.cat["Fun Money"])
    b.c.execute("UPDATE subscriptions SET status = 'cancelled', cancelled_on = '2026-09-20' WHERE id = ?", (sub,))
    late = b.arrive(b.card, "2026-10-10", -1699, "SAMPLE STREAMING TORONTO ON")
    b.c.execute("UPDATE transactions SET reason = '' WHERE id = ?", (late,))  # the reason is only for showing
    item = b.item(late)
    assert "charged you again" in item["reason"] and (item["group"], item["guess"]) == ("", None)
    b.launch()
    assert b.waiting() == [late]  # the re-check leaves it for you
    b.c.execute("UPDATE subscriptions SET status = 'active', cancelled_on = NULL WHERE id = ?", (sub,))
    assert "charged you again" not in b.item(late)["reason"]
    b.inbox.recheck([b.get(late)["payee_id"]])
    assert (b.get(late)["category_id"], b.get(late)["status"]) == (b.cat["Fun Money"], "done")


def test_a_similar_name_match_is_worked_out_not_read_back(b):
    first = b.arrive(b.card, "2026-07-01", -5000, "SAFEWAY #6432 CALGARY AB")
    b.inbox.categorize([first], b.cat["Food"])
    b.c.execute("UPDATE payees SET always_ask = 1 WHERE id = ?", (b.get(first)["payee_id"],))
    typo = b.arrive(b.card, "2026-07-05", -4200, "SAVEWAY 1532 CALGARY AB")
    assert b.get(typo)["payee_id"] == b.get(first)["payee_id"]
    b.c.execute("UPDATE transactions SET reason = '' WHERE id = ?", (typo,))
    assert b.item(typo)["reason"].startswith("Matched to Safeway (similar name). ")


# ── recurring charges ──

def test_the_recheck_never_changes_a_price(b):
    first = b.add(b.card, "2026-08-13", -1149, "NEWSGROUP SAMPLE WINTER PARK FL U")
    sub = subscriptions.create(b.c, b.get(first)["payee_id"], "Newsgroup", 1149, pricing="currency")
    charge = b.arrive(b.card, "2026-09-13", -1180, "NEWSGROUP SAMPLE WINTER PARK FL U")
    price = lambda: b.c.execute("SELECT amount FROM subscriptions WHERE id = ?", (sub,)).fetchone()[0]
    assert price() == 1180 and b.waiting() == [first, charge]  # arriving, its price followed: no category yet
    b.c.execute("UPDATE subscriptions SET amount = 1149, category_id = ? WHERE id = ?", (b.cat["Subscriptions"], sub))
    assert b.inbox.recheck() == 2
    assert price() == 1149


def test_new_price_follows_the_charge_and_categorizes_it_as_yours(b):
    old = b.add(b.card, "2026-08-04", -3699, "APPLE.COM/BILL TORONTO ON")
    sub = subscriptions.create(b.c, b.get(old)["payee_id"], "Apple One", 3699, category_id=b.cat["Subscriptions"])
    rise = b.arrive(b.card, "2026-09-04", -3899, "APPLE.COM/BILL TORONTO ON")
    item = b.item(rise)
    assert item["subscription_choices"][0]["id"] == sub and (item["group"], item["guess"]) == ("", None)
    b.inbox.new_price(rise, sub)
    row = b.get(rise)
    assert (row["category_id"], row["status"], row["by_you"], row["subscription_id"]) == (
        b.cat["Subscriptions"], "done", 1, sub)
    assert row["reason"] == "Apple One: new price $36.99 → $38.99"
    assert b.c.execute("SELECT amount FROM subscriptions WHERE id = ?", (sub,)).fetchone()[0] == 3899


def test_a_recurring_charge_learns_its_category_and_its_waiting_charges_follow(b):
    charges = [b.add(b.card, f"2026-0{m}-24", -3850, "SAMPLE PAYPLAN TORONTO ON") for m in (7, 8, 9)]
    subscriptions.create(b.c, b.get(charges[0])["payee_id"], "Couch", 3850)
    b.inbox.arrive(charges)
    assert b.waiting() == charges  # no category to go to yet
    out = b.inbox.categorize([charges[0]], b.cat["Household"])  # outside the inbox: still learnt
    assert out.also == 2 and b.waiting() == []
    assert [b.get(i)["by_you"] for i in charges] == [1, 0, 0]


def test_whats_this_for_is_asked_when_a_charge_goes_to_subscriptions(b):
    charge = b.arrive(b.card, "2026-09-05", -2999, "SAMPLE SOFTWARE TORONTO ON")
    question = b.inbox.categorize([charge], b.cat["Subscriptions"]).question
    assert (question["txn_id"], question["amount"], question["category"]) == (charge, 2999, "Subscriptions")


# ── the Inbox page ──

def test_accept_all_takes_each_items_own_guess_and_leaves_yours_and_questions(b):
    small = b.arrive(b.card, "2026-07-10", -399, "COSTCO WHOLESALE W156 CALGARY A")
    big = b.arrive(b.card, "2026-07-10", -98000, "COSTCO WHOLESALE W156 CALGARY A")
    b.inbox.categorize([small], b.cat["Food"])
    b.inbox.categorize([big], b.cat["Household"])  # a mixed payee now: always asks
    snack = b.arrive(b.card, "2026-08-17", -499, "COSTCO WHOLESALE W156 CALGARY A")
    sofa = b.arrive(b.card, "2026-08-18", -91000, "COSTCO WHOLESALE W156 CALGARY A")
    back = b.arrive(b.card, "2026-08-19", -450, "COSTCO WHOLESALE W156 CALGARY A")
    b.inbox.send_back(back)
    assert {b.item(i)["guess"] for i in (snack, sofa)} == {b.cat["Food"], b.cat["Household"]}
    out = b.inbox.accept_all([snack, sofa, back])
    assert out.categorized == 2 and b.waiting() == [back]
    assert (b.get(snack)["category_id"], b.get(sofa)["category_id"]) == (b.cat["Food"], b.cat["Household"])
    assert b.get(snack)["by_you"] and b.get(sofa)["by_you"]


def test_a_refund_categorized_anywhere_is_linked_to_its_purchase(b):
    purchase = b.arrive(b.card, "2026-07-08", -6420, "SAMPLE OUTDOORS CALGARY AB")
    b.inbox.categorize([purchase], b.cat["Household"])
    refund = b.arrive(b.card, "2026-07-10", 3000, "SAMPLE OUTDOORS CALGARY AB")  # partial: asked about
    assert b.waiting() == [refund]
    b.inbox.categorize([refund], b.cat["Household"])
    assert b.get(refund)["refund_of"] == purchase
    b.inbox.categorize([refund], b.cat["Fun Money"])  # not a refund after all
    assert b.get(refund)["refund_of"] is None
