"""The envelope budget: what's in each category, month by month.

`compute()` is pure (no database) and is where all the rules live:

- **Ready to assign** starts as the cash in your chequing/savings accounts when the
  budget starts. Income adds to it, assigning money to categories takes from it.
- A transaction still in the inbox counts in no category (docs/adr/0002). Money out
  without a category has left the bank, so it comes off Ready to assign (on a card, it's
  set aside in the card's payment envelope, as covered card spending). Money in without
  one **waits**: it may be a reimbursement that belongs to a category, so it isn't Ready
  to assign until it's categorized. Money in on a card changes nothing until then.
- A category's **available** = what carried over + assigned this month + its activity
  (spending is negative, refunds and reimbursements positive).
- **Overspending** (available below zero) is paid for one of three ways, a setting
  (`overspending`):
  - `this_month` (the default): it comes out of Ready to assign in the month it happens
    ("Overspent in October"). That money has already left the bank, so Ready to assign
    is always money you really have. The category shows below zero until the month
    ends, then starts the next month at zero. Covering it (assigning to it, or Catch
    up) doesn't change Ready to assign, since the money is already gone; covering it
    from another category gives Ready to assign that much back.
  - `next_month` (YNAB and Actual Budget): the same, but it comes out of next month's
    Ready to assign ("Overspent in September"), so until then this month's figure still
    counts money that's been spent. Assigning to cover it uses Ready to assign now.
  - `carry_over`: the category starts next month below zero, still owing what it
    overspent, and Ready to assign is left alone. Money assigned to it next month
    covers that first.
  Only cash overspending is paid for like this: uncovered card spending is new debt on
  the card instead (below), whichever way is chosen.

Credit cards and lines of credit (the debt accounts) work like YNAB:

- Each one has a **payment envelope**. Spending on the card from a category that has
  the money moves that money into the card's payment envelope, so it's there when the
  bill comes. Refunds on the card move it back.
- If the category didn't have the money, the uncovered part is **new debt**: it isn't
  moved to the payment envelope, it just adds to what you owe ("credit overspending")
  and isn't taken from Ready to assign.
- A payment from chequing to the card comes out of the payment envelope. The debt you
  had before the budget started isn't covered by anything: to pay it down, assign money
  to the payment envelope.
- Interest and fees go in a normal category (Interest & Fees), so they're covered the
  same way as any other card spending.
- Income landing on a card (cash-back rewards) adds to Ready to assign and takes the
  same amount out of the payment envelope, since you now owe less.
- A balance transfer between two debt accounts moves debt, not money: no envelope changes.

With these rules, at the end of every month:
  Ready to assign + Σ max(available, 0) − overspent_pending + waiting == cash in cash accounts
where `overspent_pending` is cash overspending not taken from Ready to assign (yet): none
with `this_month`, this month's with `next_month`, what the categories still owe with
`carry_over`; and `waiting` is money in on cash accounts that isn't categorized yet. `tests/test_budget.py` checks that on random histories for all three. The
Budget page shows it under Ready to assign, from the accounts' own balances, as a check.
"""

from util import add_months, months_between


OVERSPENDING = ("this_month", "next_month", "carry_over")


def compute(start, end, categories, lines, payments, assigned, opening_cash, overspending="this_month"):
    """Budget figures for every month from `start` to `end`.

    categories: [{'id', 'role': 'normal' | 'income' | 'payment', 'debt_account_id'}]
    lines: [(month, account_id, is_debt_account, category_id or None, amount)] for every
        non-transfer transaction (split lines separately) in a budget month
    payments: [(month, debt_account_id, cash_side_amount)] for transfers between a cash
        account and a debt account (a card payment is negative: money left chequing)
    assigned: {(month, category_id): cents}
    opening_cash: cash in cash accounts at the end of the day before `start`
    overspending: how cash overspending is paid for (see OVERSPENDING and the docstring above)
    """
    roles = {c["id"]: c["role"] for c in categories}
    payment_for = {c["debt_account_id"]: c["id"] for c in categories if c["role"] == "payment"}
    by_month = {}
    for line in lines:
        by_month.setdefault(line[0], []).append(line)
    payments_by_month = {}
    for month, account_id, amount in payments:
        payments_by_month.setdefault(month, []).append((account_id, amount))

    results = {}
    carry = {c["id"]: 0 for c in categories if c["role"] != "income"}
    if overspending not in OVERSPENDING:
        raise ValueError(f"Unknown overspending rule: {overspending}")
    previous_rta, previous_over, waiting = opening_cash, 0, 0
    for month in months_between(start, end):
        activity = {cid: 0 for cid in carry}
        card_spend = {}         # category → {debt account → net spent on it (positive = spent)}
        income = uncategorized = 0
        moved = {cid: 0 for cid in carry}
        moved_from = {cid: {} for cid in carry}  # payment envelope → {category it came from (None: none): amount}

        for _, account_id, is_debt, category_id, amount in by_month.get(month, ()):
            role = roles.get(category_id)
            if role is None:  # still in the inbox (or a category that was deleted)
                if amount > 0:
                    if not is_debt:
                        waiting += amount  # money in waits until it's categorized
                    continue
                uncategorized += amount
                if is_debt and account_id in payment_for:
                    _move(moved, moved_from, payment_for[account_id], None, -amount)
            elif role == "income":
                income += amount
                if is_debt and account_id in payment_for:
                    _move(moved, moved_from, payment_for[account_id], category_id, -amount)
            else:
                activity[category_id] += amount
                if is_debt and role == "normal" and account_id in payment_for:
                    spends = card_spend.setdefault(category_id, {})
                    spends[account_id] = spends.get(account_id, 0) - amount

        for account_id, amount in payments_by_month.get(month, ()):
            if account_id in payment_for:
                activity[payment_for[account_id]] += amount

        month_assigned = {cid: assigned.get((month, cid), 0) for cid in carry}
        cats, cash_over_total, credit_over_total = {}, 0, 0
        for cid in carry:
            if roles[cid] != "normal":
                continue
            available = carry[cid] + month_assigned[cid] + activity[cid]
            overspent = max(0, -available)
            spends = card_spend.get(cid, {})
            credit_spent = sum(spends.values())
            credit_over = min(overspent, max(credit_spent, 0))
            _move_to_payment(spends, credit_over, moved, payment_for, moved_from, cid)
            cash_over = overspent - credit_over
            cash_over_total += cash_over
            credit_over_total += credit_over
            cats[cid] = {"carried": carry[cid], "assigned": month_assigned[cid], "activity": activity[cid],
                         "available": available, "overspent_cash": cash_over, "overspent_credit": credit_over}

        for cid in carry:
            if roles[cid] != "payment":
                continue
            available = carry[cid] + month_assigned[cid] + activity[cid] + moved[cid]
            cash_over = max(0, -available)
            cash_over_total += cash_over
            cats[cid] = {"carried": carry[cid], "assigned": month_assigned[cid],
                         "activity": activity[cid] + moved[cid], "payments": activity[cid], "moved": moved[cid],
                         "moved_from": {k: v for k, v in moved_from[cid].items() if v},
                         "available": available, "overspent_cash": cash_over, "overspent_credit": 0}

        assigned_total = sum(month_assigned.values())
        rta = previous_rta - previous_over + income + uncategorized - assigned_total
        if overspending == "this_month":  # it's already left the bank
            rta -= cash_over_total
        results[month] = {
            "month": month, "carried_rta": previous_rta, "overspent_last_month": previous_over,
            "income": income, "uncategorized": uncategorized, "waiting": waiting, "assigned": assigned_total,
            "overspent": cash_over_total, "ready_to_assign": rta, "new_debt": credit_over_total,
            "in_categories": sum(max(cat["available"], 0) for cat in cats.values()),
            "overspent_pending": 0 if overspending == "this_month" else cash_over_total,
            "categories": cats,
        }
        if overspending == "carry_over":  # what was overspent in cash stays with the category, below zero
            carry = {cid: max(cats[cid]["available"], 0) - cats[cid]["overspent_cash"] for cid in carry}
        else:
            carry = {cid: max(cats[cid]["available"], 0) for cid in carry}
        previous_rta = rta
        previous_over = cash_over_total if overspending == "next_month" else 0
    return results


def _move(moved, moved_from, payment_cid, source, amount):
    moved[payment_cid] += amount
    moved_from[payment_cid][source] = moved_from[payment_cid].get(source, 0) + amount


def _move_to_payment(spends, credit_over, moved, payment_for, moved_from, source):
    """Moves card spending into each card's payment envelope, minus the part that wasn't
    covered (`credit_over`), which is shared between the cards that spent. `source` is the
    category it's moved from."""
    positive = {a: s for a, s in spends.items() if s > 0}
    total_positive = sum(positive.values())
    remaining = credit_over
    shares = {}
    for i, (account_id, spent) in enumerate(sorted(positive.items())):
        share = remaining if i == len(positive) - 1 else round(credit_over * spent / total_positive)
        share = min(share, spent, remaining)
        shares[account_id] = share
        remaining -= share
    for account_id, spent in spends.items():
        _move(moved, moved_from, payment_for[account_id], source, spent - shares.get(account_id, 0))


def average_spending(month_totals, months):
    """Average net spending per month over `months` (spending positive)."""
    if not months:
        return 0
    return round(sum(-month_totals.get(m, 0) for m in months) / len(months))


def usual_spending(month_totals, month, earliest, count):
    """A category's Usual for `month`: what it actually spent on average in up to `count`
    whole months before it, from `earliest` (the first month every account has data for)
    and from its own first spending, so a bill that started last month isn't averaged
    with months it didn't exist. None when there's no such month."""
    spent = [m for m, total in month_totals.items() if total]
    if not spent:
        return None
    months = complete_months_before(month, max(earliest, min(spent)), count)
    return average_spending(month_totals, months) if months else None


def complete_months_before(month, earliest, count):
    """Up to `count` whole months before `month`, not before `earliest`."""
    out = []
    m = add_months(month, -1)
    while len(out) < count and m >= earliest:
        out.append(m)
        m = add_months(m, -1)
    return out
