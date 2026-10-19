"""An inbox item from arrival until it's categorized, split or a transfer.

Only this module puts a transaction in the inbox (status 'review') or takes it out
(transfers.py pairs the halves of a transfer for it). It asks learn.py for suggestions
and subscriptions.py for the recurring-charge rules; service.py wraps each call in its
undo step and keeps the counts. Every category change comes through here, from every
page: a refund goes back with its purchase, a recurring charge learns its category, and
"What's this for?" is asked.

**What you decided stays decided** (docs/adr/0005). Who decided is a fact on the
transaction, `by_you`:

- Yours: what you categorize (one, a group, Accept all), split, mark as a transfer or Not
  a transfer, send back, or add by hand. Nothing the app does on its own changes it: the
  re-check skips it, and a transfer's other half never pairs with it (that half waits in
  the inbox, saying which of yours it looks like the other half of; Transfer pairs them).
  Not a transfer sends both halves back, and they're never paired again.
- The app's: what an import or the re-check categorizes ("also categorized 3 more like
  it"), and what removing an import or a merge sends back to the inbox (`ask_again`).

A transaction you sent back is its own inbox item, outside its payee's group and outside
Accept all, still with suggestions. A category change outside the inbox changes only that
transaction; in the inbox, categorizing one re-checks its payee unless you chose One at a
time (`recheck_payee`).

`reason` is only text to show: no code reads it. What the inbox says about an item is
worked out each time (`view()`): today's suggestion, "charged you again" from its
recurring charge, "Matched to X (similar name)" from how its payee was found.

A recurring charge's price follows a charge only when it first arrives (`arrive()`) or
when you choose "New price for…" (`new_price()`); the re-check never changes a price.
"""

from dataclasses import dataclass

import ledger
import subscriptions
import transfers
from learn import analyze, guess
from payees import describe

SENT_BACK = "You sent this back"
NOT_A_TRANSFER = "You said this isn't a transfer"


@dataclass
class Arrived:
    categorized: int = 0  # without asking
    waiting: int = 0      # in the inbox


@dataclass
class Outcome:
    categorized: int = 0  # of the transactions you gave it
    also: int = 0         # others the re-check categorized after it
    question: dict = None  # "What's this for?" (see _question), or None


class Inbox:
    def __init__(self, conn, today):
        self.conn = conn
        self.today = today
        self._kinds = None
        self._income = None

    # ── arriving, and the re-check ──

    def arrive(self, txn_ids):
        """An import's new rows (or a balance adjustment), oldest first: a transfer, a
        recurring charge (whose price follows this charge), or what the app has learnt;
        whatever it isn't sure of waits in the inbox."""
        out = Arrived()
        for txn in self._in_order(txn_ids):
            if self._settle(txn, arriving=True):
                out.categorized += 1
            else:
                out.waiting += 1
        return out

    def recheck(self, payee_ids=None):
        """Looks again at the inbox items that aren't yours (all of them, or these payees')
        and categorizes what the app is now sure of: at launch, after an import, after you
        categorize. Never yours, never a price. Returns how many it categorized."""
        where, params = "", []
        if payee_ids is not None:
            params = sorted({p for p in payee_ids if p})
            if not params:
                return 0
            where = f"AND payee_id IN ({','.join('?' * len(params))})"
        ids = [r["id"] for r in self.conn.execute(
            f"SELECT id FROM transactions WHERE status = 'review' AND by_you = 0 {where}", params)]
        return self._recheck_ids(ids)

    def _recheck_ids(self, ids):
        done = 0
        for txn in self._in_order(ids):
            if txn["status"] == "review" and not txn["by_you"] and self._settle(txn, arriving=False):
                done += 1
        return done

    def _in_order(self, ids):
        """These transactions oldest first, each read just before it's looked at (pairing
        one transfer half changes the other)."""
        ids = list(ids)
        order = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            order += [tuple(r) for r in self.conn.execute(
                f"SELECT date, id FROM transactions WHERE id IN ({','.join('?' * len(chunk))})", chunk)]
        for _, txn_id in sorted(order):
            txn = self._txn(txn_id)
            if txn is not None:
                yield txn

    def _settle(self, txn, arriving):
        """An inbox item that isn't yours: categorized if the app is sure, else it waits.
        True if it's out of the inbox."""
        c = self.conn
        if transfers.pair(c, txn):
            return True
        mine = transfers.find_counterpart(c, txn, yours=True)
        if mine is not None:
            return self._wait(txn, self._other_half_of(mine))
        sub = subscriptions.link_transaction(c, txn["id"], follow_price=arriving)
        if sub is not None:
            warning = subscriptions.charged_when_it_shouldnt(sub, txn["date"])
            if warning:
                return self._wait(txn, warning)
            if sub["category_id"] and sub["status"] == "active":
                c.execute("UPDATE transactions SET category_id = ?, status = 'done', reason = ?, refund_of = NULL "
                          "WHERE id = ?", (sub["category_id"], f"{sub['name']} subscription", txn["id"]))
                return True
        else:
            question = subscriptions.unmatched_question(c, txn)
            if question:
                return self._wait(txn, question)  # a new price? Always asked
        s = analyze(c, txn)
        reason = ". ".join(r for r in (self._similar_name(txn), s.reason) if r)
        if s.sure and s.options and not txn["similar_payee_id"]:
            c.execute("UPDATE transactions SET category_id = ?, status = 'done', reason = ?, refund_of = ? WHERE id = ?",
                      (s.options[0], reason, s.refund_of, txn["id"]))
            return True
        return self._wait(txn, reason)

    def _wait(self, txn, reason):
        """It waits in the inbox, in no category (docs/adr/0002). Returns False: not settled."""
        self.conn.execute("UPDATE transactions SET category_id = NULL, status = 'review', reason = ?, refund_of = NULL "
                          "WHERE id = ?", (reason, txn["id"]))
        return False

    # ── what the Inbox page shows ──

    def view(self):
        """What the Inbox page shows: every inbox item, newest first, each with its
        suggestions (best first), today's reason, its question (same payee? new price?),
        its group key ('payee:…' or 'share:…'; '' for an item of its own) and its guess
        (None: Accept all leaves it out); and the ids of what the app categorized on its
        own from the recent imports."""
        c = self.conn
        names = ledger.category_names(c)
        items = [self._look(txn, names) for txn in c.execute(
            "SELECT * FROM transactions WHERE status = 'review' ORDER BY date DESC, id DESC LIMIT 1000").fetchall()]
        recent = [r["id"] for r in c.execute("SELECT id FROM imports ORDER BY id DESC LIMIT 10")]
        categorized = [r["id"] for r in c.execute(
            f"SELECT id FROM transactions WHERE status = 'done' AND by_you = 0 AND import_id IN "
            f"({','.join('?' * len(recent))}) ORDER BY date DESC, id DESC LIMIT 300", recent)] if recent else []
        return {"items": items, "categorized": categorized}

    def _look(self, txn, names=None):
        """One inbox item as the page shows it (see view())."""
        c = self.conn
        names = names if names is not None else ledger.category_names(c)
        s = analyze(c, txn)
        yours = bool(txn["by_you"])
        if self._income is None:
            self._income = ledger.income_category(c)
        item = {"id": txn["id"], "suggestions": [{"id": cid, "name": names.get(cid, "?")} for cid in s.options],
                "guess": guess(txn, s, self._income), "mixed": s.mixed, "group": s.group,
                "yours": yours}
        reasons = [txn["reason"] or SENT_BACK] if yours else []
        alone = yours  # its own item, outside Accept all
        sub = c.execute("SELECT * FROM subscriptions WHERE id = ?", (txn["subscription_id"],)).fetchone() \
            if txn["subscription_id"] else None
        warning = subscriptions.charged_when_it_shouldnt(sub, txn["date"]) if sub else None
        choices = subscriptions.price_change_choices(c, txn)
        mine = None if yours else transfers.find_counterpart(c, txn, yours=True)
        if mine is not None:
            reasons.append(self._other_half_of(mine, names))
            alone = True
        elif warning:
            reasons.append(warning)
            alone = True
        elif choices:
            item["subscription_choices"] = choices
            reasons.append(subscriptions.unmatched_question(c, txn))
            alone = True
        else:
            reasons += [self._similar_name(txn), s.reason]
        if txn["similar_payee_id"]:
            p = c.execute("SELECT id, name FROM payees WHERE id = ?", (txn["similar_payee_id"],)).fetchone()
            item["similar_payee"] = dict(p) if p else None
            alone = True
        if alone:
            item["group"], item["guess"] = "", None
        item["reason"] = ". ".join(r for r in reasons if r)
        return item

    def _similar_name(self, txn):
        """'Matched to Safeway (similar name)' when this transaction's payee was found by
        a similar name (payee_keys.how), else ''."""
        if not txn["raw_name"] or not txn["payee_id"]:
            return ""
        key = describe(txn["raw_name"], txn["raw_memo"], self._kind(txn["account_id"])).key
        row = self.conn.execute("SELECT p.name FROM payee_keys k JOIN payees p ON p.id = k.payee_id "
                                "WHERE k.key = ? AND k.how = 'similar' AND k.payee_id = ?",
                                (key, txn["payee_id"])).fetchone()
        return f"Matched to {row['name']} (similar name)" if row else ""

    def _other_half_of(self, mine, names=None):
        """Why a transfer half waits: its other half looks like `mine`, which you decided about."""
        names = names if names is not None else ledger.category_names(self.conn)
        account = self.conn.execute("SELECT name FROM accounts WHERE id = ?", (mine["account_id"],)).fetchone()
        what = (f"which you put in {names.get(mine['category_id'], 'a category')}" if mine["category_id"] else
                "which you split" if mine["is_split"] else "which is waiting for you in the inbox")
        return (f"Looks like the other half of the {_money(mine['amount'])} "
                f"{account['name'] if account else 'other'} transaction on {mine['date']}, {what}")

    def _kind(self, account_id):
        if self._kinds is None:
            self._kinds = {r["id"]: r["kind"] for r in self.conn.execute("SELECT id, kind FROM accounts")}
        return self._kinds.get(account_id, "chequing")

    # ── your decisions (all marked yours) ──

    def categorize(self, txn_ids, category_id, recheck_payee=False):
        """You put these transactions in `category_id` (replacing a split, or a transfer:
        its other half then waits for you). Money back for a purchase is linked to it as a
        refund when that's what the category says; a recurring charge without a category
        learns this one, and its charges waiting in the inbox follow. `recheck_payee`: the
        inbox's way, where categorizing re-checks the rest of the payee (not One at a time,
        and never outside the inbox)."""
        if category_id in (None, ""):
            raise ValueError("Pick a category first.")
        txns = [self._yours(txn_id) for txn_id in txn_ids]
        learnt = [self._categorize(txn, int(category_id)) for txn in txns]
        also = self._follow(learnt)
        if recheck_payee:
            also += self.recheck({t["payee_id"] for t in txns})
        return Outcome(len(txns), also, self._question([t["id"] for t in txns]))

    def _categorize(self, txn, category_id):
        """One transaction into a category, yours. Returns its recurring charge if that
        learnt its category from this one (see _follow), else None."""
        c = self.conn
        refund_of = None
        if txn["amount"] > 0:
            s = analyze(c, txn)
            if s.refund_of and s.options and s.options[0] == category_id:
                refund_of = s.refund_of
        c.execute("DELETE FROM splits WHERE transaction_id = ?", (txn["id"],))
        c.execute("UPDATE transactions SET category_id = ?, is_split = 0, status = 'done', reason = 'Categorized by you', "
                  "similar_payee_id = NULL, refund_of = ?, by_you = 1 WHERE id = ?", (category_id, refund_of, txn["id"]))
        # A recurring charge without a category learns it from the first charge you categorize.
        if txn["subscription_id"] and c.execute("UPDATE subscriptions SET category_id = ? WHERE id = ? AND "
                                                "category_id IS NULL", (category_id, txn["subscription_id"])).rowcount:
            return txn["subscription_id"]
        return None

    def _follow(self, sub_ids):
        """The charges of these recurring charges waiting in the inbox (not yours) follow
        the category they just learnt. Returns how many were categorized."""
        ids = []
        for sub_id in {s for s in sub_ids if s}:
            ids += [r["id"] for r in self.conn.execute(
                "SELECT id FROM transactions WHERE subscription_id = ? AND status = 'review' AND by_you = 0", (sub_id,))]
        return self._recheck_ids(ids)

    def accept_all(self, txn_ids):
        """Accept all in the inbox: each item goes to its own guess (worked out again here,
        as view() shows it). Items without one (yours, e-transfers, questions) are left.
        Then their payees are re-checked."""
        names = ledger.category_names(self.conn)
        picked = []
        for txn_id in txn_ids:
            txn = self._txn(txn_id)
            if txn is not None and txn["status"] == "review":
                category_id = self._look(txn, names)["guess"]
                if category_id is not None:
                    picked.append((txn, category_id))
        learnt = [self._categorize(txn, category_id) for txn, category_id in picked]
        also = self._follow(learnt) + (self.recheck({txn["payee_id"] for txn, _ in picked}) if picked else 0)
        return Outcome(len(picked), also, None)

    def split(self, txn_id, parts):
        """You split a transaction across categories (replacing a transfer: its other
        half then waits for you)."""
        c = self.conn
        txn = self._txn(txn_id)
        if txn is None:
            raise ValueError("That transaction no longer exists.")
        parts = [p for p in parts if int(p.get("amount") or 0) != 0]
        if len(parts) < 2:
            raise ValueError("A split needs at least two parts.")
        if sum(int(p["amount"]) for p in parts) != txn["amount"]:
            raise ValueError("The parts have to add up to the transaction's amount.")
        if any(not p.get("category_id") for p in parts):
            raise ValueError("Every part needs a category.")
        txn = self._yours(txn_id)
        c.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
        for p in parts:
            c.execute("INSERT INTO splits (transaction_id, category_id, amount, note) VALUES (?, ?, ?, ?)",
                      (txn_id, int(p["category_id"]), int(p["amount"]), str(p.get("note") or "")))
        c.execute("UPDATE transactions SET is_split = 1, category_id = NULL, status = 'done', reason = 'Split by you', "
                  "similar_payee_id = NULL, refund_of = NULL, by_you = 1 WHERE id = ?", (txn_id,))

    def mark_transfer(self, txn_id, account_id):
        """You say this is a transfer to or from `account_id`: paired with its other half
        if that's imported (even one you'd categorized), else marked until it arrives. A
        half it was paired with before waits for you."""
        c = self.conn
        txn = self._txn(txn_id)
        if txn is None:
            raise ValueError("That transaction no longer exists.")
        before = txn["transfer_id"]
        transfers.make_transfer(c, txn_id, int(account_id))
        after = self._txn(txn_id)["transfer_id"]
        c.execute("UPDATE transactions SET by_you = 1, similar_payee_id = NULL WHERE id IN (?, ?)", (txn_id, after or 0))
        if before and before != after:
            self._send_back(before, NOT_A_TRANSFER)

    def not_a_transfer(self, txn_id):
        """It isn't a transfer: both halves wait for you, and are never paired again."""
        for i in transfers.unlink(self.conn, txn_id):
            self._send_back(i, NOT_A_TRANSFER)

    def send_back(self, txn_id):
        """You put a transaction back in the inbox: no category, not split (a transfer:
        Not a transfer). It waits for you, as its own inbox item."""
        txn = self._txn(txn_id)
        if txn is None:
            raise ValueError("That transaction no longer exists.")
        if txn["transfer_account_id"]:
            self.not_a_transfer(txn_id)
        else:
            self._send_back(txn_id, SENT_BACK)

    def _send_back(self, txn_id, reason):
        self.conn.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
        self.conn.execute("UPDATE transactions SET category_id = NULL, is_split = 0, status = 'review', reason = ?, "
                          "refund_of = NULL, by_you = 1 WHERE id = ?", (reason, txn_id))

    def ask_again(self, txn_ids, why):
        """Transactions the app decides about again, straight away, as if they'd just
        arrived (but never a price): a transfer half whose other half was removed (with its
        import, or deleted), or both halves of a transfer that a merge put in one account.
        They're the app's, whoever decided before. One that waits in the inbox says `why`."""
        c = self.conn
        for txn_id in txn_ids:
            transfers.unlink(c, txn_id)
            c.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
            c.execute("UPDATE transactions SET category_id = NULL, is_split = 0, status = 'review', "
                      "refund_of = NULL, by_you = 0 WHERE id = ?", (txn_id,))
        for txn in self._in_order(txn_ids):
            if txn["status"] == "review" and not self._settle(txn, arriving=False):
                c.execute("UPDATE transactions SET reason = ? || '. ' || reason WHERE id = ?", (why, txn["id"]))

    def _yours(self, txn_id):
        """A transaction you're deciding about: if it was a transfer, the pairing goes and
        its other half waits for you."""
        txn = self._txn(txn_id)
        if txn is None:
            raise ValueError("That transaction no longer exists.")
        if txn["transfer_account_id"]:
            for other in transfers.unlink(self.conn, txn_id)[1:]:
                self._send_back(other, NOT_A_TRANSFER)
            txn = self._txn(txn_id)
        return txn

    # ── answers to an item's own question ──

    def same_payee(self, txn_id, same):
        """Is this payee the same as the similar-named one? Yes moves the transaction (and
        every other with the same bank description) to it. Either way it isn't asked again."""
        c = self.conn
        txn = self._txn(txn_id)
        if txn is None or not txn["similar_payee_id"]:
            return
        if same:
            target = c.execute("SELECT name FROM payees WHERE id = ?", (txn["similar_payee_id"],)).fetchone()
            if target:
                ledger.set_payee(c, txn_id, target["name"])
            txn = self._txn(txn_id)
        c.execute("UPDATE transactions SET similar_payee_id = NULL WHERE similar_payee_id = ? AND payee_id = ?",
                  (txn["similar_payee_id"], txn["payee_id"]))

    def new_price(self, txn_id, sub_id):
        """This charge is recurring charge `sub_id` at a new price: the price follows it,
        and it's categorized in the recurring charge's category (yours)."""
        c = self.conn
        txn = self._txn(txn_id)
        sub = subscriptions.link_to(c, txn_id, sub_id) if txn is not None else None
        if sub is None:
            return
        new = -txn["amount"]
        reason = f"{sub['name']}: new price ${sub['amount'] / 100:,.2f} → ${new / 100:,.2f}" \
            if not subscriptions.same_price(sub["amount"], new) else f"{sub['name']} subscription"
        if sub["category_id"]:
            self._yours(txn_id)
            c.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
            c.execute("UPDATE transactions SET category_id = ?, is_split = 0, status = 'done', reason = ?, "
                      "similar_payee_id = NULL, refund_of = NULL, by_you = 1 WHERE id = ?",
                      (sub["category_id"], reason, txn_id))
        else:
            c.execute("UPDATE transactions SET reason = ? WHERE id = ?", (reason, txn_id))

    def set_always_ask(self, payee_id, on):
        """Switches a payee's Always ask on or off. Switched off, its waiting transactions
        the app is now sure of are categorized: returns how many."""
        self.conn.execute("UPDATE payees SET always_ask = ? WHERE id = ?", (1 if on else 0, payee_id))
        return 0 if on else self.recheck([payee_id])

    # ── "What's this for?" ──

    def _question(self, txn_ids):
        """After you categorize charges that look like a subscription or bill that isn't
        tracked yet (put in a subscriptions category, or charging on a steady rhythm): what's
        it for? None otherwise."""
        c = self.conn
        rows = []
        for txn_id in txn_ids:
            r = c.execute("SELECT t.*, p.name AS payee_name, cat.name AS category_name "
                          "FROM transactions t JOIN payees p ON p.id = t.payee_id "
                          f"LEFT JOIN categories cat ON cat.id = t.category_id WHERE t.id = ? AND {subscriptions.CAN_RECUR_SQL}",
                          (txn_id,)).fetchone()
            if r and r["amount"] < 0 and not r["transfer_account_id"] and not r["subscription_id"] and r["category_id"]:
                rows.append(r)
        if not rows:
            return None
        first = rows[0]
        price = -first["amount"]
        dismissed = c.execute("SELECT 1 FROM subscriptions WHERE payee_id = ? AND status = 'ignored' "
                              "AND abs(amount - ?) <= 2", (first["payee_id"], price)).fetchone()
        if dismissed:
            return None
        in_subscriptions = "subscript" in (first["category_name"] or "").lower() or c.execute(
            "SELECT 1 FROM subscriptions WHERE category_id = ? AND status != 'ignored'", (first["category_id"],)).fetchone()
        candidate = next((cand for cand in subscriptions.detect(c, self.today)
                          if cand["payee_id"] == first["payee_id"] and first["id"] in cand["transaction_ids"]), None)
        if not in_subscriptions and candidate is None:
            return None
        count = sum(1 for t in c.execute("SELECT amount FROM transactions WHERE payee_id = ? AND amount < 0 "
                                         "AND subscription_id IS NULL AND transfer_account_id IS NULL",
                                         (first["payee_id"],)) if subscriptions.same_price(-t["amount"], price))
        cadence = candidate["cadence"] if candidate else "monthly"
        if candidate:
            pricing = candidate.get("pricing", "fixed")
        else:
            pricing = "currency" if "foreign currency" in (first["bank_category"] or "").lower() else "fixed"
        if candidate and pricing == "varies":
            count = candidate["count"]
            why = "It charges once a month, a different amount each time"
        elif candidate:
            why = f"It looks like it charges {cadence}"
        else:
            why = f"You put it in {first['category_name']}"
        return {"txn_id": first["id"], "payee": first["payee_name"], "amount": price, "count": count,
                "category_id": first["category_id"], "category": first["category_name"], "cadence": cadence,
                "kind": candidate["kind"] if candidate else "subscription", "candidate": candidate,
                "pricing": pricing, "why": why}

    def _txn(self, txn_id):
        return self.conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()


def _money(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"
