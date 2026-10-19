"""Every money figure the pages show: the Budget page, the Dashboard, a category's
pop-over and Spent list, the Debt page, Assign money… and Catch up.

`Figures` reads what it needs from the database once per request and answers every
question from that, so all the numbers on a page come from the same moment. It only
reads: service.py makes every change (with its undo step) and arranges these figures for
the page (names, order, which months to show). Arithmetic on money happens here and in
budget.py, nowhere else.

What counts in a category is decided once, in `_lines`: a transaction, or one line of a
split, that isn't a transfer, once it's categorized (status 'done'). An inbox item counts
in no category (docs/adr/0002). The envelope rules themselves are budget.compute(), which
is pure; this module gathers what it needs and works out everything around it.
"""

from functools import cached_property

import budget
import ledger
from util import add_months, first_day, last_day, month_of, months_between

ABOVE_USUAL_CENTS = 2500  # a whole month is "above usual" at least $25 and 25% over its Usual


class Figures:
    def __init__(self, conn, today, overspending="this_month", usual_months=3):
        """`conn` must stay open (inside db.read() or db.write()) while this is used.
        `overspending` and `usual_months` are the settings of the same names."""
        self.conn = conn
        self.today = today
        self.current = month_of(today)
        self.overspending = overspending
        self.usual_months = usual_months
        self._computed = None  # (end, budget.compute's months through end)
        self._balances = {}

    # ── what's read ──

    @cached_property
    def start(self):
        """The month the budget starts, or None until it's set."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'budget_start'").fetchone()
        return row["value"] if row else None

    @cached_property
    def tree(self):
        """Category groups in budget order, each with its categories."""
        return ledger.category_tree(self.conn)

    @cached_property
    def first_month(self):
        """The month of the oldest transaction, or None."""
        first = self.conn.execute("SELECT MIN(date) FROM transactions").fetchone()[0]
        return month_of(first) if first else None

    @cached_property
    def coverage(self):
        """The first month every account has data for: whole months from here on count
        towards Usual."""
        firsts = [r[0] for r in self.conn.execute("SELECT MIN(date) FROM transactions GROUP BY account_id")]
        return max(month_of(d) for d in firsts) if firsts else self.current

    @cached_property
    def _roles(self):
        income = ledger.income_category_ids(self.conn)
        return [{"id": r["id"], "debt_account_id": r["debt_account_id"],
                 "role": "income" if r["id"] in income else "payment" if r["debt_account_id"] else "normal"}
                for r in self.conn.execute("SELECT id, debt_account_id FROM categories")]

    @cached_property
    def _lines(self):
        """Every line that can count in a category: each transaction that isn't split or a
        transfer, and each line of a split one, with its status and account's kind."""
        return [dict(r) for r in self.conn.execute(
            """
            SELECT t.id, t.date, t.account_id, a.kind, t.status, t.category_id, t.amount, 0 AS split
              FROM transactions t JOIN accounts a ON a.id = t.account_id
             WHERE t.transfer_account_id IS NULL AND t.is_split = 0
            UNION ALL
            SELECT t.id, t.date, t.account_id, a.kind, t.status, s.category_id, s.amount, 1
              FROM splits s JOIN transactions t ON t.id = s.transaction_id JOIN accounts a ON a.id = t.account_id
             WHERE t.transfer_account_id IS NULL AND t.is_split = 1
            """)]

    @cached_property
    def _spending(self):
        """{category: {month: Spent}} from every categorized line, budget or not."""
        out = {}
        for r in self._lines:
            if r["status"] == "done" and r["category_id"] is not None:
                months = out.setdefault(r["category_id"], {})
                months[month_of(r["date"])] = months.get(month_of(r["date"]), 0) + r["amount"]
        return out

    @cached_property
    def _payments(self):
        """Payments to a card or line of credit from a cash account, as the cash side saw
        them (negative: money left chequing)."""
        cash, debt = ledger.CASH_KINDS, ledger.DEBT_KINDS
        return [dict(r) for r in self.conn.execute(
            f"""
            SELECT t.id, t.date, t.transfer_account_id AS debt_account_id, t.amount FROM transactions t
              JOIN accounts a ON a.id = t.account_id JOIN accounts b ON b.id = t.transfer_account_id
             WHERE a.kind IN ({','.join('?' * len(cash))}) AND b.kind IN ({','.join('?' * len(debt))})
            """, (*cash, *debt))]

    @cached_property
    def _assigned(self):
        return {(r["month"], r["category_id"]): r["assigned"]
                for r in self.conn.execute("SELECT month, category_id, assigned FROM budget")}

    @cached_property
    def _accounts(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM accounts ORDER BY closed, sort, id")]

    def _balance(self, account_id, as_of=None):
        key = (account_id, as_of)
        if key not in self._balances:
            self._balances[key] = ledger.account_balance(self.conn, account_id, as_of)
        return self._balances[key]

    @property
    def without_balance(self):
        """Open accounts the app has no balance for yet (their names)."""
        return [a["name"] for a in self._accounts if not a["closed"] and self._balance(a["id"]) is None]

    # ── the envelope rules ──

    def _budget(self, end, assigned=None):
        """budget.compute()'s months from the budget start through `end` (more, when
        they've already been worked out), or {} before the budget starts. `assigned`:
        Assigned to use instead of what's saved (for Catch up)."""
        if not self.start or end < self.start:
            return {}
        if assigned is None and self._computed and self._computed[0] >= end:
            return self._computed[1]
        low, high = first_day(self.start), last_day(end)
        lines = [(month_of(r["date"]), r["account_id"], ledger.is_debt(r["kind"]),
                  r["category_id"] if r["status"] == "done" else None, r["amount"])
                 for r in self._lines if low <= r["date"] <= high]
        payments = [(month_of(p["date"]), p["debt_account_id"], p["amount"])
                    for p in self._payments if low <= p["date"] <= high]
        months = budget.compute(self.start, end, self._roles, lines, payments,
                                self._assigned if assigned is None else assigned, self._opening_cash,
                                overspending=self.overspending)
        if assigned is None:
            self._computed = (end, months)
        return months

    @cached_property
    def _opening_cash(self):
        total, _ = ledger.opening_cash(self.conn, self.start)
        return total

    def _cash(self, as_of=None):
        """Cash at the end of `as_of` (default: after everything imported): the money in
        open cash accounts, and in closed ones that still have money in them, from their
        own balances. {'total', 'accounts': their names}, or None while an open one has
        no balance."""
        total, names = 0, []
        for a in self.conn.execute("SELECT id, name, closed FROM accounts WHERE kind IN (?, ?, ?) ORDER BY sort, id",
                                   ledger.CASH_KINDS).fetchall():
            balance = self._balance(a["id"], as_of)
            if a["closed"] and not balance:
                continue
            if balance is None:
                return None
            total += balance
            names.append(a["name"])
        return {"total": total, "accounts": names}

    # ── what the pages ask ──

    def month(self, month):
        """A month on the Budget page. `summary`: Ready to assign and what it's checked
        against: Cash, and `off` when the two don't agree (None before the budget starts).
        `categories` and `groups`: Assigned, Spent ('activity'), Available and the rest, by
        id (before the budget starts only Spent); Usual is from the whole months before
        `month` (before this month, for this month and later ones). `overspent`:
        (category, amount) for each one below zero, in budget order, and their total.
        `short_later`: later months whose Ready to assign is below zero."""
        end = max(month, self.current)
        months = self._budget(end)
        figures = months.get(month)
        accounts = {a["id"] for a in self._accounts}
        usual_for = min(month, self.current)
        categories, groups, overspent = {}, {}, []
        for g in self.tree:
            if g["role"] == "income":
                continue
            rows = []
            for cat in g["categories"]:
                f = (figures or {}).get("categories", {}).get(cat["id"])
                history = self._spending.get(cat["id"], {})
                row = {
                    "assigned": f["assigned"] if f else 0,
                    "activity": f["activity"] if f else history.get(month, 0),
                    "available": f["available"] if f else None,
                    "carried": f["carried"] if f else 0,
                    "overspent_cash": f["overspent_cash"] if f else 0,
                    "overspent_credit": f["overspent_credit"] if f else 0,
                    "usual": budget.usual_spending(history, usual_for, self.coverage, self.usual_months)
                    if not cat["debt_account_id"] else None,
                }
                if cat["debt_account_id"] in accounts:
                    owing = -(self._balance(cat["debt_account_id"]) or 0)
                    row["owing"] = owing
                    row["set_aside"] = max(row["available"] or 0, 0)
                    row["not_covered"] = max(0, owing - row["set_aside"])
                if (row["available"] or 0) < 0:
                    overspent.append((cat["id"], -row["available"]))
                categories[cat["id"]] = row
                rows.append(row)
            groups[g["id"]] = {"assigned": sum(x["assigned"] for x in rows), "activity": sum(x["activity"] for x in rows),
                               "available": sum((x["available"] or 0) for x in rows)}
        summary = None
        if figures:
            cash = self._cash(last_day(month))
            summary = {**{k: v for k, v in figures.items() if k != "categories"}, "cash": cash,
                       "off": figures["ready_to_assign"] - (cash["total"] - figures["in_categories"] - figures["waiting"]
                                                            + figures["overspent_pending"]) if cash else None}
        return {"summary": summary, "categories": categories, "groups": groups, "overspent": overspent,
                "overspent_total": sum(amount for _, amount in overspent),
                "short_later": [m for m, f in months.items() if month < m <= end and f["ready_to_assign"] < 0]}

    def spent(self, category_ids, month):
        """The lines behind these categories' Spent in `month`, adding up to it (`total`):
        a split counts only its line in the category. A payment category also gets the
        payments to its card (`payment`) and the card spending set aside in it, which
        isn't a transaction: `moved_from` is the category it came from (None: card
        spending still in the inbox)."""
        wanted = set(category_ids)
        low, high = first_day(month), last_day(month)
        lines = [{"id": r["id"], "date": r["date"], "amount": r["amount"], "split": r["split"],
                  "category_id": r["category_id"]}
                 for r in self._lines
                 if r["status"] == "done" and r["category_id"] in wanted and low <= r["date"] <= high]
        debt = {r["id"]: r["debt_account_id"] for r in self._roles if r["id"] in wanted and r["debt_account_id"]}
        figures = {}
        if debt and self.start and month >= self.start:
            figures = (self._budget(month).get(month) or {}).get("categories", {})
        for cid, account_id in debt.items():
            if cid not in figures:
                continue  # before the budget starts, only what was categorized in it
            lines += [{"id": p["id"], "date": p["date"], "amount": p["amount"], "split": 0, "category_id": cid,
                       "payment": True}
                      for p in self._payments if p["debt_account_id"] == account_id and low <= p["date"] <= high]
            lines += [{"id": None, "date": None, "amount": amount, "split": 0, "category_id": cid, "moved_from": source}
                      for source, amount in figures[cid].get("moved_from", {}).items()]
        return {"lines": lines, "total": sum(line["amount"] for line in lines)}

    def history(self, category_id):
        """What a category spent each month from the oldest transaction to this one (and
        whether each is a whole month with data for every account), last month, and its
        average over the last 3, 6 and 12 whole months, counted like Usual: from its own
        first spending."""
        history = self._spending.get(category_id, {})
        averages = {}
        for n in (3, 6, 12):
            months = budget.usual_months(history, self.current, self.coverage, n)
            if months and (n == 3 or len(months) == n):
                averages[n] = {"months": len(months), "amount": budget.average_spending(history, months)}
        last = add_months(self.current, -1)
        return {"history": [{"month": m, "spent": -history.get(m, 0), "complete": self.coverage <= m < self.current}
                            for m in months_between(self.first_month or self.current, self.current)],
                "last_month": {"month": last, "amount": max(0, -history.get(last, 0))},
                "averages": averages}

    def spending(self, months):
        """The Dashboard's Spending table over `months`: Income, total spending, and each
        group's and category's Spent per month in budget order. A category also has its
        Usual for each month and whether it was above usual. Payment categories aren't
        listed (their money is card spending, counted where it came from), hidden ones only
        if they spent, and a group only with a category in it."""
        def above(spent, usual, month):
            return (self.coverage <= month < self.current and bool(usual) and spent >= usual + ABOVE_USUAL_CENTS
                    and spent * 4 >= usual * 5)

        income, total, groups = [0] * len(months), [0] * len(months), []
        for g in self.tree:
            if g["role"] == "income":
                for cat in g["categories"]:
                    history = self._spending.get(cat["id"], {})
                    income = [x + history.get(m, 0) for x, m in zip(income, months)]
                continue
            cats = []
            for cat in g["categories"]:
                if cat["debt_account_id"]:
                    continue
                history = self._spending.get(cat["id"], {})
                spent = [-history.get(m, 0) for m in months]
                if cat["hidden"] and not any(spent):
                    continue
                usuals = [budget.usual_spending(history, m, self.coverage, self.usual_months) for m in months]
                cats.append({"id": cat["id"], "spent": spent, "usual": usuals,
                             "above": [above(s, u, m) for s, u, m in zip(spent, usuals, months)]})
            if not cats:
                continue
            spent = [sum(x["spent"][i] for x in cats) for i in range(len(months))]
            total = [x + y for x, y in zip(total, spent)]
            groups.append({"id": g["id"], "spent": spent, "categories": cats,
                           "above": [any(x["above"][i] for x in cats) for i in range(len(months))]})
        return {"income": income, "total": total, "groups": groups}

    def balances(self):
        """Cash now (None while an open cash account has no balance) and what's owed, from
        the accounts in use: open ones, and closed ones with money left."""
        live = [a for a in self._accounts if ledger.is_debt(a["kind"]) and (not a["closed"] or self._balance(a["id"]))]
        cash = self._cash()
        return {"cash": cash["total"] if cash else None, "owed": -sum(self._balance(a["id"]) or 0 for a in live),
                "has_debt": bool(live)}

    def worth(self, months):
        """Cash, what's owed and net worth (cash − owed) at the end of each month (this
        month: so far), from every account that has a balance, closed ones included."""
        known = [a for a in self._accounts if a["anchor_date"]]
        out = {"months": months, "cash": [], "owed": [], "net": []}
        for m in months:
            day = min(last_day(m), self.today)
            cash = sum(self._balance(a["id"], day) for a in known if not ledger.is_debt(a["kind"]))
            owed = -sum(self._balance(a["id"], day) for a in known if ledger.is_debt(a["kind"]))
            out["cash"].append(cash)
            out["owed"].append(owed)
            out["net"].append(cash - owed)
        return out

    @cached_property
    def _interest(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT account_id, date, amount FROM transactions WHERE hint IN ('interest', 'fee')")]

    def debts(self):
        """Each open card and line of credit: what's owed, what's set aside for it, what
        isn't yet, interest and fees and what was owed over the last six months, an
        estimated monthly payment (the planned one, else the average paid to it from cash
        accounts over the last three whole months: a balance transfer from another card
        isn't a payment) and how long paying it off would take."""
        current = self.current
        f = self._budget(current).get(current, {}).get("categories", {})
        payment_cats = ledger.payment_categories(self.conn)
        months = [add_months(current, -i) for i in range(5, -1, -1)]
        paid_from, paid_to = first_day(add_months(current, -3)), last_day(add_months(current, -1))
        out = []
        for a in self._accounts:
            if not ledger.is_debt(a["kind"]) or a["closed"]:
                continue
            owing = -(self._balance(a["id"]) or 0)
            set_aside = max(0, f.get(payment_cats.get(a["id"]), {}).get("available", 0))
            interest = [{"month": m, "amount": -sum(t["amount"] for t in self._interest if t["account_id"] == a["id"]
                                                      and first_day(m) <= t["date"] <= last_day(m))} for m in months]
            balances = [{"month": m, "owing": -(self._balance(a["id"], last_day(m)) or 0)} for m in months]
            paid = -sum(p["amount"] for p in self._payments
                        if p["debt_account_id"] == a["id"] and paid_from <= p["date"] <= paid_to)
            payment = a["planned_payment"] or (round(paid / 3) if paid else None)
            out.append({"account_id": a["id"], "owing": owing, "set_aside": set_aside,
                        "not_covered": max(0, owing - set_aside), "estimate_payment": payment,
                        "interest": interest, "interest_total": sum(i["amount"] for i in interest),
                        "balances": balances, "has_balance": self._balance(a["id"]) is not None,
                        "payoff": payoff(owing, a["apr"], payment)})
        return out

    def assign_plan(self, month, amount=None):
        """How Assign money… would share out `amount` (default: all of Ready to assign):
        each category with a budgeted amount is filled up to it, in budget order, counting
        what's already in it (carried in, plus assigned this month). When overspending
        comes out of Ready to assign the month it happens, covering this month's
        overspending costs nothing (`covered`): only the rest of an amount is used up."""
        view = self.month(month)
        available = view["summary"]["ready_to_assign"] if view["summary"] else 0
        amount = available if amount in (None, "") else int(amount)
        remaining = max(0, amount)
        plan = []
        for g in self.tree:
            if g["role"] == "income":
                continue
            for cat in g["categories"]:
                target = cat["monthly_budget"]
                if cat["hidden"] or target is None:
                    continue
                f = view["categories"][cat["id"]]
                has = f["carried"] + f["assigned"]
                need = max(0, target - has)
                covered = f["overspent_cash"] if self.overspending == "this_month" else 0
                give = min(need, remaining + covered)
                remaining -= max(0, give - covered)
                plan.append({"category_id": cat["id"], "need": need, "monthly_budget": target,
                             "assigned": f["assigned"], "has": has, "amount": give, "covered": covered})
        return {"month": month, "amount": amount, "ready_to_assign": available, "plan": plan, "left_over": remaining}

    def catch_up(self, months):
        """Assigned for each category in each of `months` (in order) so none ends it below
        zero: {(month, category): Assigned}, ordinary categories first, then payment
        categories (covering card spending moves money into them). None if a month is
        before the budget starts."""
        assigned = dict(self._assigned)
        roles = {r["id"]: r["role"] for r in self._roles}
        changes = {}
        for month in months:
            for role in ("normal", "payment"):
                f = self._budget(month, assigned).get(month)
                if f is None:
                    return None
                for cid, cat in f["categories"].items():
                    if roles.get(cid) == role and cat["available"] < 0:
                        assigned[(month, cid)] = changes[(month, cid)] = cat["assigned"] - cat["available"]
        return changes


def payoff(owing, apr, payment):
    """Months to pay off `owing` at `payment` a month, and the interest that costs.
    None if it would never be paid off (the payment doesn't cover the interest)."""
    if owing <= 0:
        return {"months": 0, "interest": 0}
    if not payment or payment <= 0:
        return None
    rate = (apr or 0) / 100 / 12
    balance, interest, months = owing, 0, 0
    while balance > 0 and months < 600:
        charge = round(balance * rate)
        if payment <= charge:
            return None
        interest += charge
        balance = balance + charge - payment
        months += 1
    return {"months": months, "interest": interest}
