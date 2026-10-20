"""Guessing a transaction's category, and deciding when it's sure enough not to ask.

Everything is learnt from what you've already filed (plus examples taken from an
Actual Budget export), nothing is hard-coded per merchant:

- Past transactions with the same payee vote for their category. Votes count more
  when the amount is similar (a $4 hot dog vs a $900 Costco run) and when
  they're recent.
- A payee that has gone to two or more categories is "mixed" and always asks, with the
  closest match by amount first. So does any payee with Always ask on.
- A payee you've never seen falls back on the card's own category ("Restaurant"),
  translated through what you've done with that card category before.
- Refunds go back to the category of the purchase they refund; money in by e-transfer
  is matched to a shared expense it looks like a share of, never to a refund.
- **A payee is just a name** (docs/adr/0001). Anyone you e-transfer with starts with
  Always ask on, because money sent to one name goes to different things each time:
  each e-transfer is its own question, with what you chose last time suggested first.
  Switch it off (rent e-transferred to a landlord) and the payee is learnt like any
  other. Whatever the switch, "Accept all" in the inbox never includes an e-transfer.
- **Bank entries** (interest, fees, rewards) are never asked about: each kind (its
  payee) goes where you last put one, at first Interest & Fees or Income.

`analyze()` returns a Suggestion: up to four categories, best first, whether it's
sure (the importer then files it without asking), and the reason to show. A transaction
left in the inbox gets no category from it (docs/adr/0002): suggestions are only offered.
"""

import math
from dataclasses import dataclass, field

from ledger import assignable_category_ids, category_names, income_category, interest_category
from payees import BANK_ENTRY_HINTS
from util import days_between

SURE_SHARE = 0.9      # share of the (weighted) votes the top category needs to be filed without asking
MIXED_SHARE = 0.15    # a second category with this share of past uses makes a payee "mixed"
HISTORY_LIMIT = 40    # most recent same-payee transactions considered


@dataclass
class Suggestion:
    options: list = field(default_factory=list)  # category ids, best first
    sure: bool = False
    reason: str = ""
    refund_of: int = None
    mixed: bool = False   # this payee goes to different categories, so never filed without asking
    group: str = ""       # inbox items with the same group are shown as one (see analyze())
    individual: bool = False  # this one is its own inbox item (a refund, an unusual amount, Always ask...)


def _amount_similarity(a, b):
    a, b = abs(a) or 1, abs(b) or 1
    return 1.0 / (1.0 + 3.0 * abs(math.log(a / b)))


def _history(conn, txn):
    """(category_id, amount, date, source) for this payee, same direction of money first."""
    rows = conn.execute(
        """
        SELECT category_id, amount, date, 'own' AS source FROM transactions
         WHERE payee_id = :payee AND status = 'done' AND is_split = 0 AND category_id IS NOT NULL
           AND transfer_account_id IS NULL AND id != :id
        UNION ALL
        SELECT s.category_id, s.amount, t.date, 'own' FROM splits s JOIN transactions t ON t.id = s.transaction_id
         WHERE t.payee_id = :payee AND t.status = 'done' AND t.is_split = 1 AND s.category_id IS NOT NULL
           AND t.transfer_account_id IS NULL AND t.id != :id
        UNION ALL
        SELECT category_id, amount, date, 'example' FROM examples WHERE payee_id = :payee
        ORDER BY date DESC LIMIT :limit
        """, {"payee": txn["payee_id"], "id": txn["id"], "limit": HISTORY_LIMIT}).fetchall()
    same = [r for r in rows if (r["amount"] < 0) == (txn["amount"] < 0)]
    return (same, True) if same else (rows, False)


def _from_history(conn, txn, names, allowed):
    rows, same_direction = _history(conn, txn)
    rows = [r for r in rows if r["category_id"] in allowed]
    if not rows:
        return None
    weights, counts = {}, {}
    for r in rows:
        age = max(0, days_between(r["date"], txn["date"])) if r["date"] <= txn["date"] else 0
        w = (0.5 ** (age / 365.0)) * _amount_similarity(txn["amount"], r["amount"])
        weights[r["category_id"]] = weights.get(r["category_id"], 0.0) + w
        counts[r["category_id"]] = counts.get(r["category_id"], 0) + 1
    ranked = sorted(weights, key=lambda c: -weights[c])
    total = sum(weights.values())
    top = ranked[0]
    share = weights[top] / total if total else 0
    n = len(rows)
    mixed = sum(1 for c in counts if counts[c] / n >= MIXED_SHARE) >= 2
    always_ask = _always_ask(conn, txn)
    from_actual = all(r["source"] == "example" for r in rows)
    biggest = max(abs(r["amount"]) for r in rows)
    # A $576 Apple purchase isn't another $14 subscription, even if Apple always went there.
    unusual = abs(txn["amount"]) > 3 * biggest and abs(txn["amount"]) > 5000
    sure = same_direction and not mixed and not always_ask and not unusual and share >= SURE_SHARE
    if always_ask and txn["hint"] == "etransfer":
        if not same_direction:
            reason = ("You've only sent this payee money before" if txn["amount"] > 0
                      else "This payee has only sent you money before")
            if txn["amount"] > 0:  # what you paid them for says little about money they send you
                ranked = [income_category(conn)] + [c for c in ranked if c != income_category(conn)]
        else:
            reason = (f"Last time: {names.get(top, '?')}" if n == 1 else
                      f"Usually {names.get(top, '?')} ({counts[top]} of {n})")
        reason += ". E-transfers start with Always ask on"
        return Suggestion(ranked[:4], False, reason, mixed=True, individual=True)
    if unusual and not mixed:
        reason = (f"Much bigger than usual for this payee ({_money(-abs(txn['amount']))} vs at most "
                  f"{_money(-biggest)} before). {names.get(top, '?')}?")
        return Suggestion(ranked[:4], False, reason, mixed=True, individual=True)
    if mixed:
        parts = " · ".join(f"{names.get(c, '?')} {counts[c]}×" for c in sorted(counts, key=lambda c: -counts[c])[:3])
        reason = f"Goes to more than one category ({parts}); closest by amount first"
    elif always_ask:
        reason = f"You asked to always check this payee (usually {names.get(top, '?')})"
    elif from_actual:
        reason = f"{names.get(top, '?')}, learnt from your Actual Budget history"
    elif counts[top] == 1:
        reason = f"{names.get(top, '?')} last time"
    else:
        reason = f"Always {names.get(top, '?')} ({counts[top]} times before)" if share > 0.99 else \
            f"Usually {names.get(top, '?')} ({counts[top]} of {n})"
    if not same_direction:
        reason = "Money in from a payee you've only paid before"
    return Suggestion(ranked[:4], sure, reason, mixed=mixed or always_ask, individual=always_ask)


def _from_bank_category(conn, txn, names, allowed):
    if not txn["bank_category"]:
        return None
    rows = conn.execute(
        "SELECT category_id, COUNT(*) AS n FROM transactions WHERE bank_category = ? AND status = 'done' "
        "AND category_id IS NOT NULL AND (amount < 0) = (? < 0) GROUP BY category_id ORDER BY n DESC",
        (txn["bank_category"], txn["amount"])).fetchall()
    rows = [r for r in rows if r["category_id"] in allowed]
    total = sum(r["n"] for r in rows)
    if not rows or total < 3 or rows[0]["n"] / total < 0.6:
        return None  # too few, or too vague ("Other" covers everything)
    top = rows[0]
    reason = (f"New payee. The card calls it “{txn['bank_category']}”, which you usually categorize "
              f"as {names.get(top['category_id'], '?')}")
    return Suggestion([r["category_id"] for r in rows[:4]], False, reason)


def match_refund(conn, txn, names):
    """Money back from the payee you paid: back into the category the purchase came out of."""
    if txn["amount"] <= 0 or txn["payee_id"] is None:
        return None
    if txn["hint"] == "etransfer":
        return None  # an e-transfer in is a share of something (match_reimbursement), not a refund
    rows = conn.execute(
        """
        SELECT t.id, t.date, t.amount, t.category_id FROM transactions t
         WHERE t.payee_id = ? AND t.amount < 0 AND t.transfer_account_id IS NULL AND t.is_split = 0
           AND t.category_id IS NOT NULL AND t.date <= ? AND t.date >= date(?, '-120 days')
           AND NOT EXISTS (SELECT 1 FROM transactions r WHERE r.refund_of = t.id AND r.id != ?)
         ORDER BY t.date DESC
        """, (txn["payee_id"], txn["date"], txn["date"], txn["id"])).fetchall()
    exact = [r for r in rows if r["amount"] == -txn["amount"]]
    if exact:
        r = exact[0]
        return Suggestion([r["category_id"]], True,
                          f"Refund of the {_money(r['amount'])} purchase on {r['date']}: back to "
                          f"{names.get(r['category_id'], '?')}", refund_of=r["id"])
    bigger = [r for r in rows if -r["amount"] >= txn["amount"]]
    if bigger:
        r = bigger[0]
        return Suggestion([r["category_id"]], False,
                          f"Looks like a refund of the {_money(r['amount'])} purchase on {r['date']}. "
                          f"Back to {names.get(r['category_id'], '?')}?", refund_of=r["id"], individual=True)
    return None


def match_reimbursement(conn, txn, names):
    """An e-transfer in that's a share of something you paid: eight people chipping in
    for a $600 team fee. Suggests the category the bill went to."""
    if txn["amount"] <= 0 or txn["hint"] != "etransfer":
        return None
    # Several people sending the same amount around the same time: the bill was split
    # at least that many ways.
    senders = conn.execute(
        "SELECT COUNT(DISTINCT payee_id) FROM transactions WHERE amount BETWEEN ? AND ? AND "
        "date BETWEEN date(?, '-21 days') AND date(?, '+21 days') AND transfer_account_id IS NULL",
        (txn["amount"] - max(2, txn["amount"] // 100), txn["amount"] + max(2, txn["amount"] // 100),
         txn["date"], txn["date"])).fetchone()[0]
    rows = conn.execute(
        """
        SELECT id, date, amount, category_id, payee_id FROM transactions
         WHERE amount < 0 AND transfer_account_id IS NULL AND is_split = 0
           AND date BETWEEN date(?, '-45 days') AND date(?, '+20 days')
         ORDER BY date DESC
        """, (txn["date"], txn["date"])).fetchall()
    best = None
    for r in rows:
        ratio = -r["amount"] / txn["amount"]
        people = round(ratio)
        if people < max(2, senders) or people > 40:
            continue
        # Shares are the bill divided evenly, give or take rounding to the cent.
        if abs(-r["amount"] - people * txn["amount"]) <= max(3 * people, -r["amount"] // 2000):
            closeness = abs(days_between(r["date"], txn["date"]))
            if best is None or closeness < best[1]:
                best = (r, closeness, people)
    if best is None:
        return None
    r, _, people = best
    payee = conn.execute("SELECT name FROM payees WHERE id = ?", (r["payee_id"],)).fetchone()
    where = payee["name"] if payee else "a purchase"
    group = f"share:{r['id']}"
    if r["category_id"] is None:
        return Suggestion([], False, f"Looks like a 1/{people} share of {where} ({_money(r['amount'])} on "
                                     f"{r['date']}). Put it in that one's category.", group=group)
    return Suggestion([r["category_id"]], False,
                      f"Looks like a 1/{people} share of {where} ({_money(r['amount'])} on {r['date']}). "
                      f"Back to {names.get(r['category_id'], '?')}?", group=group)


def analyze(conn, txn):
    """The best guess for a transaction (a transactions row)."""
    names = category_names(conn)
    allowed = assignable_category_ids(conn) | {income_category(conn)}
    hint = txn["hint"]
    if hint in BANK_ENTRY_HINTS:
        return _bank_entry(conn, txn, names, allowed)

    refund = match_refund(conn, txn, names)
    if refund and refund.sure:
        return refund
    share = match_reimbursement(conn, txn, names)
    learnt = _from_history(conn, txn, names, allowed)
    if learnt and learnt.sure and not (txn["amount"] > 0 and (refund or share)):
        return learnt

    options, reasons, group = [], [], ""
    for candidate in (refund, share, learnt, _from_bank_category(conn, txn, names, allowed)):
        if candidate:
            if not reasons:
                reasons.append(candidate.reason)
                group = candidate.group
            options += [c for c in candidate.options if c not in options]
    if txn["amount"] > 0 and income_category(conn) not in options:
        options.append(income_category(conn))
    if hint == "cash" and not reasons:
        reasons.append("Cash: which category did it come out of?")
    if not reasons:
        reasons.append("New payee")
    always_ask = _always_ask(conn, txn)
    mixed = bool(learnt and learnt.mixed) or bool(refund) or (always_ask and hint == "etransfer")
    # One group per payee in the inbox (even a mixed one like Amazon: "One at a time"
    # splits it), except payees with Always ask on (anyone you e-transfer with, at first)
    # and items that are their own question (a refund, an unusual amount). Shares of one
    # shared expense keep their own group.
    individual = always_ask or any(c.individual for c in (refund, learnt) if c)
    if not group.startswith("share:"):
        group = "" if individual or not txn["payee_id"] else \
            f"payee:{txn['payee_id']}:{'in' if txn['amount'] > 0 else 'out'}"
    return Suggestion(options[:4], False, reasons[0], refund_of=refund.refund_of if refund else None,
                      mixed=mixed, group=group, individual=individual)


def guess(txn, suggestion, income_id):
    """The category "Accept all" in the inbox gives an inbox item: its top suggestion.
    None for an e-transfer, whatever its payee's Always ask (each is its own question),
    and for money in the app can only call Income (unknown money in is asked about)."""
    if not suggestion.options or txn["hint"] == "etransfer":
        return None
    if txn["amount"] > 0 and suggestion.options == [income_id]:
        return None
    return suggestion.options[0]


def _always_ask(conn, txn):
    payee = conn.execute("SELECT always_ask FROM payees WHERE id = ?", (txn["payee_id"],)).fetchone()
    return bool(payee and payee["always_ask"])


# Where each kind of bank entry (payees.BANK_ENTRY_HINTS) starts, and why.
_BANK_ENTRY_START = {"interest": (interest_category, "Interest charged on the card"),
                     "fee": (interest_category, "A bank fee"),
                     "income": (income_category, "Money from the bank: income")}


def _bank_entry(conn, txn, names, allowed):
    """Interest, a bank fee or a reward: never asked about. Each kind (its payee) goes to
    the category the newest one went to, so moving one moves the ones after it. Until
    then interest charged and fees go to Interest & Fees, interest earned and rewards to
    Income."""
    last = conn.execute(
        "SELECT category_id FROM transactions WHERE payee_id = ? AND id != ? AND status = 'done' AND is_split = 0 "
        "AND category_id IS NOT NULL AND transfer_account_id IS NULL ORDER BY date DESC, id DESC LIMIT 1",
        (txn["payee_id"], txn["id"])).fetchone()
    category, reason = _BANK_ENTRY_START[txn["hint"]]
    start = category(conn)
    if last and last["category_id"] != start and last["category_id"] in allowed:
        return Suggestion([last["category_id"]], True, f"Where the last one went: {names.get(last['category_id'], '?')}")
    return Suggestion([start], True, reason)


def _money(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"
