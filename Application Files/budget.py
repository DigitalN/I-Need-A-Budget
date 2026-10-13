"""The envelope budget: what's in each category, month by month.

`compute()` is pure (no database) and is where all the rules live:

- **Ready to assign** starts as the cash in your chequing/savings accounts when the
  budget starts. Income adds to it, assigning money to categories takes from it.
  Transactions without a category yet count against it too, so the totals always add up.
- A category's **available** = what carried over + assigned this month + its activity
  (spending is negative, refunds and reimbursements positive).
- **Overspending** (available below zero) doesn't carry over. Next month the category
  starts at zero again, and the overspent amount comes out of next month's Ready to
  assign ("Overspent last month"), like Actual Budget does.

Credit cards and lines of credit (the debt accounts) work like YNAB:

- Each one has a **payment envelope**. Spending on the card from a category that has
  the money moves that money into the card's payment envelope, so it's there when the
  bill comes. Refunds on the card move it back.
- If the category didn't have the money, the uncovered part is **new debt**: it isn't
  moved to the payment envelope, it just adds to what you owe ("credit overspending")
  and isn't taken from next month's Ready to assign.
- A payment from chequing to the card comes out of the payment envelope. The debt you
  had before the budget started isn't covered by anything: to pay it down, assign money
  to the payment envelope.
- Interest and fees go in a normal category (Interest & Fees), so they're covered the
  same way as any other card spending.
- Income landing on a card (cash-back rewards) adds to Ready to assign and takes the
  same amount out of the payment envelope, since you now owe less.
- A balance transfer between two debt accounts moves debt, not money: no envelope changes.

With these rules, at the end of every month:
  Ready to assign − overspent this month + Σ carried into next month == cash in cash accounts.
`tests/test_budget.py` checks that on random histories.
"""

from util import add_months, months_between


def compute(start, end, categories, lines, payments, assigned, opening_cash):
    """Budget figures for every month from `start` to `end`.

    categories: [{'id', 'role': 'normal' | 'income' | 'payment', 'debt_account_id'}]
    lines: [(month, account_id, is_debt_account, category_id or None, amount)] for every
        non-transfer transaction (split lines separately) in a budget month
    payments: [(month, debt_account_id, cash_side_amount)] for transfers between a cash
        account and a debt account (a card payment is negative: money left chequing)
    assigned: {(month, category_id): cents}
    opening_cash: cash in cash accounts at the end of the day before `start`
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
    previous_rta, previous_cash_over = opening_cash, 0
    for month in months_between(start, end):
        activity = {cid: 0 for cid in carry}
        card_spend = {}         # category → {debt account → net spent on it (positive = spent)}
        income = uncategorized = 0
        moved = {cid: 0 for cid in carry}

        for _, account_id, is_debt, category_id, amount in by_month.get(month, ()):
            role = roles.get(category_id)
            if role is None:  # uncategorized (or a category that was deleted)
                uncategorized += amount
                if is_debt and account_id in payment_for:
                    moved[payment_for[account_id]] -= amount
            elif role == "income":
                income += amount
                if is_debt and account_id in payment_for:
                    moved[payment_for[account_id]] -= amount
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
            _move_to_payment(spends, credit_over, moved, payment_for)
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
                         "available": available, "overspent_cash": cash_over, "overspent_credit": 0}

        assigned_total = sum(month_assigned.values())
        rta = previous_rta - previous_cash_over + income + uncategorized - assigned_total
        results[month] = {
            "month": month, "carried_rta": previous_rta, "overspent_last_month": previous_cash_over,
            "income": income, "uncategorized": uncategorized, "assigned": assigned_total,
            "ready_to_assign": rta, "overspent_this_month": cash_over_total, "new_debt": credit_over_total,
            "categories": cats,
        }
        carry = {cid: max(cats[cid]["available"], 0) for cid in carry}
        previous_rta, previous_cash_over = rta, cash_over_total
    return results


def _move_to_payment(spends, credit_over, moved, payment_for):
    """Moves card spending into each card's payment envelope, minus the part that wasn't
    covered (`credit_over`), which is shared between the cards that spent."""
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
        moved[payment_for[account_id]] += spent - shares.get(account_id, 0)


def average_spending(month_totals, months):
    """Average net spending per month over `months` (spending positive)."""
    if not months:
        return 0
    return round(sum(-month_totals.get(m, 0) for m in months) / len(months))


def complete_months_before(month, earliest, count):
    """Up to `count` whole months before `month`, not before `earliest`."""
    out = []
    m = add_months(month, -1)
    while len(out) < count and m >= earliest:
        out.append(m)
        m = add_months(m, -1)
    return out
