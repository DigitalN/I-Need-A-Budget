"""Subscriptions and bills: found automatically, then tracked on their own page.

Each tracked one has a **price type**:

- `fixed`: the same price every time (Apple, Netflix). Only the exact price (±2¢)
  matches, and any other price from that payee is a question: a price rise, something
  new, or a one-off? The person asked for this: an Apple charge at a new price could be a
  price rise or an app from the store.
- `currency`: billed in another currency (a US-dollar service), so the price wobbles a
  little every month with the exchange rate. Anything within 6% is the same price and
  isn't flagged as a change; a bigger move is a real price change. Recognised on its own:
  the card tags it "Foreign Currency Spend", or its price has drifted more than once.
- `varies`: a different amount every time (a power bill). Any charge from that payee is
  the bill; the page shows the typical amount and the range instead of price changes.

Detection looks for a payee charging on a steady rhythm (weekly, monthly or yearly):
either about the same amount each time, and the *only* thing that payee charges at that
amount in that stretch (a coffee shop you visit every few weeks isn't a subscription), or,
for bills that vary, a payee that charges once a month and nothing else.

Found ones are suggestions until you track them or dismiss them. Once tracked, every new
charge is linked to it, so the page can say when it's next due, when the price changed,
when it seems to have stopped, and (the important one) when something charges you that
shouldn't: after you marked it cancelled, or after its optional end date (a payment plan's
last payment, a promotion's end). Such a charge goes to the inbox with a warning.

One payee can have several subscriptions told apart by price: Apple bills iCloud+ and
Apple Music under the same "APPLE.COM/BILL". Each tracked subscription has its own name
("iCloud+") and its own category; its charges show as "Apple · iCloud+" and are filed in
that category without asking.
"""

from util import add_days, days_between, same_day_next_month

TOLERANCE = 0.06      # how far a currency-billed price wobbles and still counts as the same
CADENCES = {"weekly": (6, 8, 7), "monthly": (25, 38, 30.44), "yearly": (350, 380, 365.25)}
PRICINGS = ("fixed", "currency", "varies")
RECENT_CHARGES = 6    # how many recent charges the typical amount of a varying bill is taken from


def close(a, b, tolerance=TOLERANCE, floor=50):
    """Amounts (positive cents) within `tolerance` of each other, or `floor` cents."""
    return abs(a - b) <= max(floor, tolerance * max(a, b))


def same_price(a, b):
    """The same price, give or take a cent or two of rounding."""
    return abs(a - b) <= 2


def same_price_for(pricing, a, b):
    """Whether two charges are the same price for a subscription with this price type."""
    if pricing == "varies":
        return True
    if pricing == "currency":
        return close(a, b, TOLERANCE, floor=2)
    return same_price(a, b)


def monthly_equivalent(amount, cadence):
    return round(amount * {"weekly": 52 / 12, "monthly": 1, "yearly": 1 / 12}.get(cadence, 1))


def next_due(last_date, cadence):
    if cadence == "monthly":
        return same_day_next_month(last_date)
    return add_days(last_date, round(CADENCES.get(cadence, CADENCES["monthly"])[2]))


def looks_foreign(charges):
    """Billed in another currency: the card says so, or the price has drifted more than once.
    `charges`: dicts or rows with `amt` (positive cents, oldest first) and `bank_category`."""
    if any("foreign currency" in (c["bank_category"] or "").lower() for c in charges):
        return True
    changes = sum(1 for a, b in zip(charges, charges[1:]) if not same_price(a["amt"], b["amt"]))
    return changes >= 2


def previous_price(pricing, amounts):
    """The price before the latest one, if it changed (as this price type sees changes)."""
    if pricing == "varies" or not amounts:
        return None
    latest = amounts[-1]
    return next((a for a in reversed(amounts[:-1]) if not same_price_for(pricing, a, latest)), None)


def _chains(txns):
    """Groups one payee's charges (sorted by date) into steady rhythms of about the same amount."""
    chains = []
    for t in txns:
        best = None
        for chain in chains:
            last = chain["txns"][-1]
            gap = days_between(last["date"], t["date"])
            for cadence, (low, high, _) in CADENCES.items():
                if chain["cadence"] not in (None, cadence) or not low <= gap <= high:
                    continue
                if close(last["amt"], t["amt"]):
                    diff = abs(last["amt"] - t["amt"])
                    if best is None or diff < best[2]:
                        best = (chain, cadence, diff)
        if best:
            best[0]["txns"].append(t)
            best[0]["cadence"] = best[1]
        else:
            chains.append({"txns": [t], "cadence": None})
    return [c for c in chains if len(c["txns"]) >= 2]


def _monthly_run(txns):
    """The latest unbroken run of once-a-month charges (any amounts), oldest first."""
    run = []
    for t in reversed(txns):
        if run and not CADENCES["monthly"][0] <= days_between(t["date"], run[0]["date"]) <= CADENCES["monthly"][1]:
            break
        run.insert(0, t)
    return run


def _day_spread(dates):
    """How many days apart in the month these dates fall, allowing for month ends
    (the 30th, 1st and 31st are 2 days apart)."""
    days = [int(d[8:10]) for d in dates]
    plain = max(days) - min(days)
    wrapped = [d + 31 if d <= 7 else d for d in days]
    return min(plain, max(wrapped) - min(wrapped))


def _typical(amounts):
    recent = amounts[-RECENT_CHARGES:]
    return round(sum(recent) / len(recent)) if recent else 0


def detect(conn, today):
    """Suggested subscriptions and bills: steady charges not yet tracked or dismissed."""
    rows = conn.execute(
        """
        SELECT t.id, t.payee_id, p.name, p.kind, t.date, -t.amount AS amt, t.account_id, t.category_id,
               t.subscription_id, t.bank_category
          FROM transactions t JOIN payees p ON p.id = t.payee_id
         WHERE t.amount < 0 AND t.transfer_account_id IS NULL AND p.kind NOT IN ('person', 'bank')
         ORDER BY t.payee_id, t.date, t.id
        """).fetchall()
    known = conn.execute("SELECT payee_id, amount, pricing FROM subscriptions").fetchall()
    known_payees = {k["payee_id"] for k in known}
    by_payee = {}
    for r in rows:
        by_payee.setdefault(r["payee_id"], []).append(dict(r))
    found = []
    for payee_id, txns in by_payee.items():
        unlinked = [t for t in txns if not t["subscription_id"]]
        # A bill that's different every month: the payee charges once a month and nothing
        # else, and the amount has moved more than once (one move is just a price change).
        run = _monthly_run(unlinked)
        if len(run) >= 3 and payee_id not in known_payees and \
                days_between(run[-1]["date"], today) <= CADENCES["monthly"][2] * 1.6 + 7:
            amounts = [t["amt"] for t in run]
            moves = sum(1 for a, b in zip(amounts, amounts[1:]) if not close(a, b, TOLERANCE, floor=2))
            if moves >= 2:
                typical = _typical(amounts)
                last = run[-1]
                found.append({
                    "payee_id": payee_id, "name": last["name"], "amount": typical, "cadence": "monthly",
                    "count": len(run), "last_date": last["date"], "next_date": next_due(last["date"], "monthly"),
                    "previous_amount": None, "pricing": "varies", "low": min(amounts), "high": max(amounts),
                    "last_amount": amounts[-1], "account_id": last["account_id"], "category_id": last["category_id"],
                    "transaction_ids": [t["id"] for t in run], "kind": "bill", "monthly": typical,
                })
                continue
        for chain in _chains(unlinked):
            members = {t["id"] for t in chain["txns"]}
            first, last = chain["txns"][0], chain["txns"][-1]
            during = [t for t in txns if t["id"] not in members and first["date"] <= t["date"] <= last["date"]]
            if any(close(t["amt"], last["amt"]) for t in during):
                continue  # a shop you go to often, not a subscription
            if chain["cadence"] == "monthly" and _day_spread([t["date"] for t in chain["txns"]]) > 6:
                continue  # subscriptions charge on (about) the same day each month
            if len(members) == 2:
                # Only two charges: they need to look like the same bill on the same day of the month.
                a, b = chain["txns"]
                if not close(a["amt"], b["amt"], 0.02, floor=10) or (
                        chain["cadence"] == "monthly" and abs(int(a["date"][8:]) - int(b["date"][8:])) > 4):
                    continue
            interval = CADENCES[chain["cadence"]][2]
            if days_between(last["date"], today) > interval * 1.6 + 7:
                continue  # stopped a while ago
            if any(k["payee_id"] == payee_id and close(k["amount"], last["amt"]) for k in known):
                continue
            pricing = "currency" if looks_foreign(chain["txns"]) else "fixed"
            amounts = [t["amt"] for t in chain["txns"]]
            found.append({
                "payee_id": payee_id, "name": last["name"], "amount": amounts[-1], "cadence": chain["cadence"],
                "count": len(amounts), "last_date": last["date"], "next_date": next_due(last["date"], chain["cadence"]),
                "previous_amount": previous_price(pricing, amounts), "pricing": pricing,
                "low": min(amounts), "high": max(amounts), "account_id": last["account_id"],
                "category_id": last["category_id"], "transaction_ids": [t["id"] for t in chain["txns"]],
                "kind": guess_kind(conn, last), "monthly": monthly_equivalent(amounts[-1], chain["cadence"]),
            })
    found.sort(key=lambda c: -c["monthly"])
    return found


def guess_kind(conn, txn):
    acct = conn.execute("SELECT kind FROM accounts WHERE id = ?", (txn["account_id"],)).fetchone()
    if txn["amt"] >= 10_000 or (acct and acct["kind"] in ("chequing", "savings") and txn["kind"] == "organization"):
        return "bill"
    return "subscription"


def _link_matching(conn, sub_id, payee_id, amount, pricing):
    """Links this payee's past charges that belong to the subscription (by its price type)."""
    for t in conn.execute("SELECT id, amount FROM transactions WHERE payee_id = ? AND amount < 0 AND "
                          "subscription_id IS NULL AND transfer_account_id IS NULL", (payee_id,)).fetchall():
        if same_price_for(pricing, int(amount), -t["amount"]):
            conn.execute("UPDATE transactions SET subscription_id = ? WHERE id = ?", (sub_id, t["id"]))


def track(conn, candidate, kind=None, name=None, category_id=None, pricing=None, ends_on=None):
    """Starts tracking a detected subscription. Its past charges are linked to it, and
    any still waiting for review are filed in its category."""
    category_id = category_id or candidate.get("category_id")
    pricing = pricing if pricing in PRICINGS else candidate.get("pricing") or "fixed"
    sub_id = conn.execute(
        "INSERT INTO subscriptions (name, payee_id, amount, cadence, kind, status, account_id, category_id, pricing, "
        "ends_on) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
        (name or candidate["name"], candidate["payee_id"], candidate["amount"], candidate["cadence"],
         kind or candidate.get("kind") or "subscription", candidate.get("account_id"), category_id,
         pricing, ends_on or None)).lastrowid
    _link_ids(conn, sub_id, candidate.get("transaction_ids") or [])
    if pricing == "varies":
        _link_matching(conn, sub_id, candidate["payee_id"], candidate["amount"], pricing)
    file_pending(conn, sub_id)
    return sub_id


def create(conn, payee_id, name, amount, cadence="monthly", kind="subscription", category_id=None, pricing="fixed",
           ends_on=None):
    """Adds a subscription by hand ("Apple Music, $11.99 a month"). Past charges from that
    payee at that price (any price, for a bill that varies) are linked to it."""
    pricing = pricing if pricing in PRICINGS else "fixed"
    sub_id = conn.execute(
        "INSERT INTO subscriptions (name, payee_id, amount, cadence, kind, status, category_id, pricing, ends_on) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (name, payee_id, int(amount), cadence, kind, category_id, pricing, ends_on or None)).lastrowid
    _link_matching(conn, sub_id, payee_id, amount, pricing)
    file_pending(conn, sub_id)
    return sub_id


def set_pricing(conn, sub_id, pricing):
    """Changes a subscription's price type; a bill that varies picks up its payee's other charges."""
    if pricing not in PRICINGS:
        return
    sub = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    if sub is None:
        return
    conn.execute("UPDATE subscriptions SET pricing = ? WHERE id = ?", (pricing, sub_id))
    if pricing == "varies":
        _link_matching(conn, sub_id, sub["payee_id"], sub["amount"], pricing)
        file_pending(conn, sub_id)


def file_pending(conn, sub_id):
    """Files this subscription's charges that are waiting for review in its category."""
    sub = conn.execute("SELECT name, category_id FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    if sub is None or sub["category_id"] is None:
        return 0
    return conn.execute(
        "UPDATE transactions SET category_id = ?, status = 'done', reason = ? "
        "WHERE subscription_id = ? AND status = 'review' AND is_split = 0 AND transfer_account_id IS NULL "
        "AND reason NOT LIKE '%charged you again%'",
        (sub["category_id"], f"{sub['name']} subscription", sub_id)).rowcount


def dismiss(conn, candidate):
    """Not a subscription: remember that, so it isn't suggested again."""
    sub_id = conn.execute(
        "INSERT INTO subscriptions (name, payee_id, amount, cadence, kind, status, account_id, pricing) "
        "VALUES (?, ?, ?, ?, 'subscription', 'ignored', ?, ?)",
        (candidate["name"], candidate["payee_id"], candidate["amount"], candidate["cadence"],
         candidate.get("account_id"), candidate.get("pricing") or "fixed")).lastrowid
    _link_ids(conn, sub_id, candidate.get("transaction_ids") or [])
    return sub_id


def _link_ids(conn, sub_id, ids):
    for txn_id in ids:
        conn.execute("UPDATE transactions SET subscription_id = ? WHERE id = ?", (sub_id, txn_id))


def _last_charge(conn, sub_id):
    row = conn.execute("SELECT MAX(date) FROM transactions WHERE subscription_id = ?", (sub_id,)).fetchone()
    return row[0] if row else None


def _charges(conn, sub_id):
    return conn.execute("SELECT id, date, -amount AS amt, account_id, bank_category FROM transactions "
                        "WHERE subscription_id = ? ORDER BY date, id", (sub_id,)).fetchall()


def effective_pricing(conn, sub):
    """The price type to use: a "fixed" subscription that turns out to be billed in
    another currency is treated as one."""
    if sub["pricing"] == "fixed" and looks_foreign(_charges(conn, sub["id"])):
        return "currency"
    return sub["pricing"]


def _is_charge(txn):
    return txn["amount"] < 0 and not txn["transfer_account_id"] and txn["payee_id"] is not None


def match(conn, txn):
    """The tracked subscription a charge belongs to, or None. Several subscriptions from
    one payee (Apple) are told apart by price; see the module docstring for price types."""
    if not _is_charge(txn):
        return None
    amount = -txn["amount"]
    subs = conn.execute("SELECT * FROM subscriptions WHERE payee_id = ? ORDER BY status = 'active' DESC, id",
                        (txn["payee_id"],)).fetchall()
    priced = [s for s in subs if s["pricing"] != "varies"
              and same_price_for(effective_pricing(conn, s), s["amount"], amount)]
    if priced:
        return min(priced, key=lambda s: abs(s["amount"] - amount))
    varying = [s for s in subs if s["pricing"] == "varies"]
    return varying[0] if varying else None


def price_change_choices(conn, txn):
    """For a charge from a payee you have subscriptions with, at a price none of them has:
    the ones it might be a new price for, likeliest first (the one due around then)."""
    if not _is_charge(txn) or txn["subscription_id"]:
        return []
    amount = -txn["amount"]
    out = []
    for s in conn.execute("SELECT * FROM subscriptions WHERE payee_id = ? AND status = 'active' "
                          "AND pricing != 'varies'", (txn["payee_id"],)).fetchall():
        last = _last_charge(conn, s["id"])
        expected = next_due(last, s["cadence"]) if last else None
        off_by = abs(days_between(expected, txn["date"])) if expected else 99
        out.append({"id": s["id"], "name": s["name"], "amount": s["amount"], "expected": expected,
                    "due_now": off_by <= 5, "_rank": (off_by > 5, abs(s["amount"] - amount))})
    out.sort(key=lambda s: s.pop("_rank"))
    return out


def unmatched_question(conn, txn):
    """The question to ask about a charge from a subscription payee at an unknown price, or None."""
    choices = price_change_choices(conn, txn)
    if not choices:
        return None
    payee = conn.execute("SELECT name FROM payees WHERE id = ?", (txn["payee_id"],)).fetchone()
    known = ", ".join(f"{c['name']} (${c['amount'] / 100:,.2f})" for c in choices[:3])
    return (f"{payee['name'] if payee else 'This payee'} charged ${-txn['amount'] / 100:,.2f}, which isn't the "
            f"price of {known}. A new price, something new, or a one-off purchase?")


def link_to(conn, txn_id, sub_id):
    """The person says this charge is `sub_id` at a new price: link it, take the new
    price, and file it in the subscription's category."""
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    sub = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
    if txn is None or sub is None:
        return
    new = -txn["amount"]
    reason = f"{sub['name']}: new price ${sub['amount'] / 100:,.2f} → ${new / 100:,.2f}" \
        if not same_price(sub["amount"], new) else f"{sub['name']} subscription"
    conn.execute("UPDATE subscriptions SET amount = ? WHERE id = ?", (new, sub_id))
    conn.execute("UPDATE transactions SET subscription_id = ?, reason = ? WHERE id = ?", (sub_id, reason, txn_id))
    if sub["category_id"] and not txn["is_split"]:
        conn.execute("UPDATE transactions SET category_id = ?, status = 'done' WHERE id = ?",
                     (sub["category_id"], txn_id))


def link_transaction(conn, txn_id):
    """Links a newly imported charge to its tracked subscription. Returns (subscription
    row or None, warning or None); the warning is for a cancelled subscription that has
    charged again."""
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if txn is None:
        return None, None
    sub = match(conn, txn)
    if sub is None:
        return None, None
    conn.execute("UPDATE transactions SET subscription_id = ? WHERE id = ?", (sub["id"], txn_id))
    if sub["pricing"] != "varies" and sub["amount"] != -txn["amount"]:
        # Exchange-rate drift: centre the next match on the latest price.
        conn.execute("UPDATE subscriptions SET amount = ? WHERE id = ?", (-txn["amount"], sub["id"]))
    return sub, charged_when_it_shouldnt(sub, txn["date"])


def charged_when_it_shouldnt(sub, day):
    """The warning for a charge on `day` after the subscription was cancelled or should
    have ended, else None. (The inbox keeps any reason with "charged you again" in it.)"""
    if sub["status"] == "cancelled" and sub["cancelled_on"] and day > sub["cancelled_on"]:
        return f"{sub['name']} was cancelled on {sub['cancelled_on']}, but it charged you again"
    if sub["ends_on"] and day > sub["ends_on"]:
        return f"{sub['name']} should have ended on {sub['ends_on']}, but it charged you again"
    return None


def listing(conn, today):
    """Everything the Subscriptions page shows."""
    subs = []
    for s in conn.execute(
            "SELECT s.*, p.name AS payee_name, c.name AS category_name FROM subscriptions s "
            "LEFT JOIN payees p ON p.id = s.payee_id LEFT JOIN categories c ON c.id = s.category_id "
            "WHERE s.status != 'ignored' ORDER BY s.name COLLATE NOCASE"):
        charges = _charges(conn, s["id"])
        amounts = [c["amt"] for c in charges]
        last = charges[-1] if charges else None
        pricing = effective_pricing(conn, s)
        amount = _typical(amounts) if pricing == "varies" and amounts else (last["amt"] if last else s["amount"])
        recent = amounts[-RECENT_CHARGES:]
        next_date = next_due(last["date"], s["cadence"]) if last else None
        ended = bool(s["ends_on"] and today > s["ends_on"])
        if next_date and s["ends_on"] and next_date > s["ends_on"]:
            next_date = None  # no more charges expected
        overdue = bool(next_date and s["status"] == "active" and days_between(next_date, today) > 7)
        charged_after_cancel = [dict(c) for c in charges
                                if s["status"] == "cancelled" and s["cancelled_on"] and c["date"] > s["cancelled_on"]]
        charged_after_end = [dict(c) for c in charges if s["ends_on"] and c["date"] > s["ends_on"]]
        subs.append({
            "id": s["id"], "name": s["name"], "payee_id": s["payee_id"], "payee_name": s["payee_name"],
            "category_id": s["category_id"], "category_name": s["category_name"],
            "kind": s["kind"], "status": s["status"], "pricing": pricing, "chosen_pricing": s["pricing"],
            "cadence": s["cadence"], "amount": amount, "usual_amount": s["amount"],
            "previous_amount": previous_price(pricing, amounts),
            "last_amount": last["amt"] if last else None,
            "low": min(recent) if recent else None, "high": max(recent) if recent else None,
            "monthly": monthly_equivalent(amount, s["cadence"]), "last_date": last["date"] if last else None,
            "next_date": next_date if s["status"] == "active" else None, "overdue": overdue,
            "account_id": (last["account_id"] if last else s["account_id"]), "note": s["note"],
            "cancelled_on": s["cancelled_on"], "charged_after_cancel": charged_after_cancel,
            "ends_on": s["ends_on"], "ended": ended, "charged_after_end": charged_after_end,
            "ending_soon": bool(s["ends_on"] and not ended and days_between(today, s["ends_on"]) <= 30),
            "charges": [{"date": c["date"], "amount": c["amt"]} for c in charges[-12:]],
        })
    active = [s for s in subs if s["status"] == "active"]
    return {
        "subscriptions": [s for s in active if s["kind"] == "subscription"],
        "bills": [s for s in active if s["kind"] == "bill"],
        "cancelled": [s for s in subs if s["status"] == "cancelled"],
        "suggested": detect(conn, today),
        "monthly_subscriptions": sum(s["monthly"] for s in active if s["kind"] == "subscription"),
        "monthly_bills": sum(s["monthly"] for s in active if s["kind"] == "bill"),
    }
