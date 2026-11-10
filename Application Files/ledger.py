# SPDX-License-Identifier: GPL-3.0-only
"""Core bookkeeping shared by the importer, the budget and the page: accounts and their
balances, the special categories (Income, debt payment envelopes, Interest & Fees),
and finding or creating payees.

Every function takes an open connection (`db.write()` / `db.read()`); none of them
commits. Amounts are cents.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

import transfers
from errors import UserError
from payees import ADJUSTMENT, describe, find_similar, merchant_key, transaction_hint
from util import add_days, day_before

CASH_KINDS = ("chequing", "savings", "cash")
DEBT_KINDS = ("credit_card", "line_of_credit")
ACCOUNT_KINDS = CASH_KINDS + DEBT_KINDS
KIND_LABELS = {"chequing": "Chequing", "savings": "Savings", "cash": "Wallet",
               "credit_card": "Credit card", "line_of_credit": "Line of credit"}

DEBT_GROUP_NAME = "Debt Payments"
INTEREST_CATEGORY_NAME = "Interest & Fees"


def is_debt(kind):
    return kind in DEBT_KINDS


# ── accounts ──────────────────────────────────────────────────

def create_account(conn, name, kind, keys=(), last4=None, anchor_date=None, anchor_balance=None, bank=None):
    if kind not in ACCOUNT_KINDS:
        raise ValueError(f"Unknown account type: {kind}")
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM accounts").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO accounts (name, kind, bank, last4, anchor_date, anchor_balance, sort) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, kind, bank or None, last4, anchor_date, anchor_balance, sort))
    account_id = cur.lastrowid
    for key in keys:
        conn.execute("INSERT OR IGNORE INTO account_sources (key, account_id) VALUES (?, ?)", (key, account_id))
    if is_debt(kind):
        ensure_payment_envelope(conn, account_id)
        interest_category(conn)
    return account_id


def used_account_ids(conn):
    """Accounts that have been used: transactions in them or transfers to them, or money
    assigned to a card's payment category. A used account can only be closed, never
    deleted: deleting it would take its history with it and send the other halves of its
    transfers back to the inbox."""
    return {r[0] for r in conn.execute(
        "SELECT account_id FROM transactions "
        "UNION SELECT transfer_account_id FROM transactions WHERE transfer_account_id IS NOT NULL "
        "UNION SELECT c.debt_account_id FROM categories c JOIN budget b ON b.category_id = c.id "
        "WHERE c.debt_account_id IS NOT NULL AND b.assigned != 0")}


def account_by_key(conn, keys):
    for key in keys:
        row = conn.execute("SELECT account_id FROM account_sources WHERE key = ?", (key,)).fetchone()
        if row:
            return row["account_id"]
    return None


def account_balance(conn, account_id, as_of=None):
    """The balance at the end of `as_of` (default: after everything imported), worked out
    from the one balance the user gave us. None if they haven't given one yet.

    A balance adjustment from Check balance… dated the balance's own day, made after the file that
    gave it, is what the bank showed later that day: it comes after that balance, not in it (issue
    #137). A Quicken file's balance is usually dated the day it was downloaded, so that's the usual
    day to check."""
    acct = conn.execute("SELECT anchor_date, anchor_balance FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if acct is None or acct["anchor_date"] is None:
        return None
    anchor = acct["anchor_date"]
    as_of = as_of or "9999-12-31"
    late = "COALESCE(date = :anchor AND hint = :hint AND import_id IS NULL AND after_import >= :file, 0)"
    delta = conn.execute(
        f"SELECT COALESCE(SUM(CASE WHEN (date > :anchor OR {late}) AND date <= :asof THEN amount "
        f"                          WHEN date > :asof AND date <= :anchor AND NOT {late} THEN -amount ELSE 0 END), 0) "
        "FROM transactions WHERE account_id = :id",
        {"anchor": anchor, "asof": as_of, "id": account_id, "hint": ADJUSTMENT,
         "file": anchor_import(conn, account_id)}).fetchone()[0]
    return acct["anchor_balance"] + delta


def anchor_import(conn, account_id):
    """The import whose file balance is the account's balance now, or None: a balance you typed, or a
    file's from before version 7 recorded which import set it."""
    return conn.execute(
        "SELECT MAX(i.id) FROM imports i JOIN accounts a ON a.id = i.account_id WHERE a.id = ? "
        "AND i.anchor_after_date = a.anchor_date AND i.anchor_after_balance = a.anchor_balance", (account_id,)).fetchone()[0]


def newest_balance_import(conn, account_id):
    """The newest import that set the account's balance (None: none has): what a balance adjustment
    from Check balance… comes after."""
    return conn.execute("SELECT MAX(id) FROM imports WHERE account_id = ? AND anchor_after_date IS NOT NULL",
                        (account_id,)).fetchone()[0]


# ── the balance check ────────────────────────────────────────
#
# A file's balance (an OFX LEDGERBAL) becomes the account's: every other balance is worked out from it.
# A balance typed by hand gives way to the next file's as it is (one typed during the day misses what
# the bank adds later that day), and that file's balance is where the check starts (accounts.check_from).
# Each later file's balance is checked against it, by what arrived rather than by date: card files bring
# charges dated before they posted, so the transactions dated between two balances don't add up to the
# difference between them, while the transactions that arrived between them do. What arrived and doesn't
# explain waits in the Inbox as a balance adjustment, dated the newest balance's date, so the budget's
# starting balance stays put (Inbox.check_balance keeps it). It's worked out again from scratch after
# every import into the account, so it shrinks, or goes, as the missing transactions arrive, and Remove
# import puts it back the way it was. Older history a file fills in (rows from before the day the starting
# file covers from) was in the starting balance already, so it doesn't count (balance_difference).
#
# Check balance… doesn't end the check (issue #137). Its balance adjustment stands for what's happened since
# the newest file's balance, so the check leaves it alone until a file whose balance is as new as it comes in
# after it. That file takes it over: while it's the app's it becomes the check's own (it goes, or stays as what
# that file doesn't explain); one you've categorized counts as one you kept.

ADJUSTMENT_PAYEE = "Balance adjustment"


@dataclass
class Span:
    """What an account's balance check covers: from the file balance it starts from to the newest one."""
    check_from: int     # the import the check starts from
    start_balance: int  # its balance ...
    start_date: str     # ... and that balance's date
    first_day: str      # the first day it covers (issue #136)
    current: int        # the import that brought the newest balance
    balance: int        # the newest balance ...
    date: str           # ... and its date


# A balance adjustment from Check balance… (it has no import) that the check has taken over: a file's balance as
# new as it came in after it (the newest balance came from a later import than the newest one when you checked,
# and is dated on or after it), inside what the check covers (it came after the file the check starts from, or is
# dated after that file's balance).
_TAKEN_OVER = ("(import_id IS NULL AND COALESCE(after_import, 0) < :current AND date <= :date "
               "AND (COALESCE(after_import, 0) >= :check_from OR date > :start_date))")
# A balance adjustment inside the balance the check starts from, from before that file came in: one from Check
# balance… it's as new as, or one an earlier check made. That's an account whose balance wasn't checked yet (a typed
# one, or a file's from before 1.4), or a check that started again (Set balance, Merge into…, Remove import of the
# file it started from). That balance has it, so the check takes it over too: one of the app's is worked out again
# (one from Check balance… goes), and one you kept counts as kept, so a Keep both pair still cancels out and the Inbox
# asks before removing one whose money has arrived.
_IN_START = ("(((import_id IS NULL AND COALESCE(after_import, 0) < :check_from) OR import_id < :check_from) "
             "AND date <= :start_date)")
# The check's own balance adjustments, and those it has taken over.
_CHECKED = f"account_id = :id AND hint = :hint AND (import_id >= :check_from OR {_TAKEN_OVER} OR {_IN_START})"


def _span_params(account_id, span):
    return {"id": account_id, "hint": ADJUSTMENT, "check_from": span.check_from, "current": span.current,
            "date": span.date, "start_date": span.start_date}


def check_span(conn, account_id):
    """What the account's balance check covers, or None when its balance isn't checked (typed, or no file
    balance to start from yet)."""
    acct = conn.execute("SELECT anchor_date, anchor_balance, anchor_source, check_from FROM accounts WHERE id = ?",
                        (account_id,)).fetchone()
    if acct is None or acct["anchor_source"] != "file" or acct["check_from"] is None:
        return None
    start = conn.execute("SELECT anchor_after_balance, anchor_after_date, "
                         "COALESCE(covers_from, first_date, anchor_after_date) AS first_day "
                         "FROM imports WHERE id = ?", (acct["check_from"],)).fetchone()
    current = anchor_import(conn, account_id)
    if start is None or start["anchor_after_balance"] is None or current is None:
        return None
    return Span(acct["check_from"], start["anchor_after_balance"], start["anchor_after_date"], start["first_day"] or "",
                current, acct["anchor_balance"], acct["anchor_date"])


@dataclass
class Difference:
    waiting: int  # what no transaction explains, in cents: the balance adjustment that should wait
    span: Span

    @property
    def date(self):  # the newest file balance's date: the adjustment's
        return self.span.date

    @property
    def check_from(self):
        return self.span.check_from

    @property
    def current(self):
        return self.span.current


def balance_difference(conn, account_id):
    """What the account's newest file balance says that its transactions don't, or None when its
    balance isn't checked (typed, or no file balance to start from yet). The newest balance, less the
    one the check starts from, less the bank's transactions that arrived since (in a later import, up
    to the one that brought the newest balance, or in any later file that ends by then), less the
    balance adjustments you kept since. The adjustment waiting for you isn't counted: this is what it
    should be. A balance adjustment from Check balance… counts once the check has taken it over (see
    _TAKEN_OVER and _IN_START): one you kept, as kept; one of the app's becomes the one waiting.

    Older history isn't money that arrived since (issue #136): a row dated before the starting file's
    first day, in a file that starts before that day too, was in the starting balance already. A file's
    first day is the one it says it covers from (`imports.covers_from`, a Quicken file's DTSTART), else
    its first row's date, else (no rows) its balance's date. Not the first row's date when it says: a
    card file covers the days things posted, so it has charges dated before its first day, and a charge
    dated before it that posted after its balance comes in a later file that starts on that day too."""
    span = check_span(conn, account_id)
    if span is None:
        return None
    arrived = conn.execute(
        "SELECT COALESCE(SUM(t.amount), 0) FROM transactions t JOIN imports i ON i.id = t.import_id "
        "WHERE t.account_id = ? AND t.fingerprint IS NOT NULL AND t.import_id > ? AND (t.import_id <= ? OR i.last_date <= ?) "
        "AND (t.date >= ? OR COALESCE(i.covers_from, i.first_date) >= ?)",
        (account_id, span.check_from, span.current, span.date, span.first_day, span.first_day)).fetchone()[0]
    kept = conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE {_CHECKED} "
                        "AND NOT (by_you = 0 AND status = 'review')", _span_params(account_id, span)).fetchone()[0]
    return Difference(span.balance - span.start_balance - arrived - kept, span)


def waiting_adjustments(conn, account_id, span):
    """The account's balance adjustments that are the app's and wait in the Inbox, made since the
    check started (or taken over by it, or in the balance it starts from), newest first: what
    Inbox.check_balance keeps up to date."""
    return [r["id"] for r in conn.execute(
        f"SELECT id FROM transactions WHERE {_CHECKED} AND by_you = 0 AND status = 'review' ORDER BY id DESC",
        _span_params(account_id, span))]


def kept_adjustments(conn, account_id, span):
    """The balance adjustments you kept (categorized, or decided about) since the check started (or
    taken over by it, or in the balance it starts from), oldest first."""
    return conn.execute(
        f"SELECT * FROM transactions WHERE {_CHECKED} AND NOT (by_you = 0 AND status = 'review') ORDER BY date, id",
        _span_params(account_id, span)).fetchall()


def likely_gap(conn, account_id, check_from):
    """(the last day a file covered, the first day the next file covers) for the first stretch of
    days no file covered from the file the check starts from on: where a download may be missing.
    None when the files leave no gap."""
    # A file with a balance covers the days up to its balance's date, even with no transactions on them.
    spans = conn.execute("SELECT first_date, MAX(last_date, COALESCE(anchor_after_date, last_date)) AS last_date "
                         "FROM imports WHERE account_id = ? AND first_date IS NOT NULL ORDER BY first_date",
                         (account_id,)).fetchall()
    start = conn.execute("SELECT first_date FROM imports WHERE id = ?", (check_from,)).fetchone()
    if start is None or start["first_date"] is None:
        return None
    covered = None
    for span in spans:
        if covered is not None and span["first_date"] > add_days(covered, 1):
            if covered >= start["first_date"]:
                return covered, span["first_date"]
            covered = None
        covered = span["last_date"] if covered is None else max(covered, span["last_date"])
    return None


def opening_cash(conn, start_month):
    """Money in cash accounts when the budget starts (end of the day before)."""
    before = day_before(f"{start_month}-01")
    total, missing = 0, []
    for acct in conn.execute("SELECT id, name, kind FROM accounts WHERE kind IN (?, ?, ?)", CASH_KINDS):
        balance = account_balance(conn, acct["id"], before)
        if balance is None:
            missing.append(acct["name"])
        else:
            total += balance
    return total, missing


OTHER_KIND_DAYS = 2  # a bank's CSV and its Quicken file can date one transaction this many days apart


def same_kind_of_file(a, b):
    """Whether two imports' formats are the same kind of file. A set-up CSV is stored as
    `csv:<layout id>:<its shape>`; one imported before that was stored as plain `csv`, and counts
    as the same kind as any of them."""
    if a == b:
        return True
    return "csv" in (a, b) and a.startswith("csv") and b.startswith("csv")


def one_transaction_in_two_files(a, b):
    """Whether two rows (dicts with `date`, and `first` / `last`: the dates of the file each came in,
    None when not known, which counts as inside) can be one transaction seen in two kinds of file:
    at least one of them falls within the other's file dates. Rows that are both outside it sit at
    the edges of two downloads (a CSV ending Oct 15, a Quicken file starting Oct 16): two
    transactions, however alike they look."""
    def inside(row, other):
        return (not other["first"] or row["date"] >= other["first"]) and (not other["last"] or row["date"] <= other["last"])
    return inside(a, b) or inside(b, a)


@lru_cache(maxsize=None)
def _day_number(day):
    return date.fromisoformat(day).toordinal()


def pair_nearest(wanted, offered, days=OTHER_KIND_DAYS, allowed=None):
    """[(wanted id, offered id)]: rows (dicts with id, date, amount) that are the same transaction,
    seen in two kinds of file: the same amount, dates up to `days` apart, each used once, the
    nearest dates first over all of them at once (a tie goes to the lower ids), not in the order
    the rows come. `allowed(w, o)` can rule a pair out. Each wanted row meets only the offered rows
    of its amount within `days` of it (found in dates sorted per amount), never all of them: a
    long history repeats the same amounts for years."""
    by_amount = {}
    for o in offered:
        by_amount.setdefault(o["amount"], []).append((_day_number(o["date"]), o))
    for rows in by_amount.values():
        rows.sort(key=lambda r: r[0])
    day_numbers = {amount: [d for d, _ in rows] for amount, rows in by_amount.items()}
    candidates = []
    for w in wanted:
        rows = by_amount.get(w["amount"])
        if not rows:
            continue
        day, numbers = _day_number(w["date"]), day_numbers[w["amount"]]
        for other_day, o in rows[bisect_left(numbers, day - days):bisect_right(numbers, day + days)]:
            if allowed is None or allowed(w, o):
                candidates.append((abs(other_day - day), w["id"], o["id"]))
    candidates.sort()
    used_w, used_o, out = set(), set(), []
    for _, w, o in candidates:
        if w not in used_w and o not in used_o:
            used_w.add(w)
            used_o.add(o)
            out.append((w, o))
    return out


def cross_kind_pairs(conn, a_id, b_id, days=OTHER_KIND_DAYS, skip_a=(), skip_b=()):
    """[(a's transaction id, b's)]: rows of two accounts that are the same transaction seen in
    different kinds of file (a bank's CSV and its Quicken file, or a CSV set up again), by
    pair_nearest, and only where one of the two rows falls within the other's file dates
    (one_transaction_in_two_files). `skip_a` / `skip_b`:
    rows that already have an exact twin (the same fingerprint or bank ID), which pair with nothing
    else. Only the rows of one account within `days` of the other's dates are looked at."""
    def span(account_id):
        return conn.execute("SELECT MIN(date), MAX(date) FROM transactions WHERE account_id = ?", (account_id,)).fetchone()

    def rows(account_id, skip, within):  # a balance adjustment is never a copy of anything
        return [dict(r) for r in conn.execute(
            "SELECT t.id, t.date, t.amount, COALESCE(i.format, '') AS kind, i.first_date AS first, i.last_date AS last "
            "FROM transactions t LEFT JOIN imports i ON i.id = t.import_id "
            "WHERE t.account_id = ? AND t.date BETWEEN ? AND ? AND t.hint != ?",
            (account_id, add_days(within[0], -days), add_days(within[1], days), ADJUSTMENT)) if r["id"] not in skip]
    a_span, b_span = span(a_id), span(b_id)
    if a_span[0] is None or b_span[0] is None:
        return []
    return pair_nearest(rows(a_id, set(skip_a), b_span), rows(b_id, set(skip_b), a_span), days,
                        allowed=lambda a, b: not same_kind_of_file(a["kind"], b["kind"])
                        and one_transaction_in_two_files(a, b))


def duplicate_accounts(conn):
    """Accounts that look like a second copy of another one (a file was imported into a
    new account when it belonged to an existing one): nearly all of the newer account's
    transactions, where their dates overlap, are already in the older one.
    [{'account_id', 'name', 'into_id', 'into_name', 'shared', 'total'}]"""
    pairs = {}
    for r in conn.execute("SELECT GROUP_CONCAT(DISTINCT account_id) AS ids FROM transactions "
                          "WHERE fingerprint IS NOT NULL GROUP BY fingerprint HAVING COUNT(DISTINCT account_id) > 1"):
        ids = sorted(int(i) for i in r["ids"].split(","))
        for older in ids[:-1]:
            pairs[(ids[-1], older)] = pairs.get((ids[-1], older), 0) + 1
    accounts = {r["id"]: r for r in conn.execute(
        "SELECT a.id, a.name, a.kind, MIN(t.date) AS first, MAX(t.date) AS last, COUNT(t.id) AS n FROM accounts a "
        "JOIN transactions t ON t.account_id = a.id GROUP BY a.id")}
    # The same transactions in different kinds of file (a CSV and a Quicken file worded differently) share
    # no fingerprint: pair them by date and amount.
    ids = sorted(accounts)
    for i, older in enumerate(ids):
        for newer in ids[i + 1:]:
            a, b = accounts[older], accounts[newer]
            if is_debt(a["kind"]) != is_debt(b["kind"]) or a["first"] > b["last"] or b["first"] > a["last"]:
                continue
            shared = len(cross_kind_pairs(conn, older, newer))
            if shared > pairs.get((newer, older), 0):
                pairs[(newer, older)] = shared
    out, seen = [], set()
    for (newer, older), shared in sorted(pairs.items(), key=lambda kv: -kv[1]):
        a, b = accounts.get(newer), accounts.get(older)
        if not a or not b or newer in seen or is_debt(a["kind"]) != is_debt(b["kind"]):
            continue
        overlapping = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ? AND date BETWEEN ? AND ?",
                                   (newer, b["first"], b["last"])).fetchone()[0]
        if shared >= min(3, a["n"]) and shared >= 0.8 * overlapping:
            seen.add(newer)
            out.append({"account_id": newer, "name": a["name"], "into_id": older, "into_name": b["name"],
                        "shared": shared, "total": a["n"]})
    return out


def merge_accounts(conn, from_id, into_id):
    """`from_id` turned out to be the same account as `into_id`. Its transactions that
    `into_id` already has are dropped (see _drop_copy: what you decided stays), the rest
    move over, and `from_id` goes. Returns (moved, dropped, unlinked, asked): `unlinked` were
    a transfer between the two copies, both halves now in one account, and `asked` are the
    other halves of dropped copies that no longer have one: the inbox asks about both again."""
    a = conn.execute("SELECT * FROM accounts WHERE id = ?", (from_id,)).fetchone()
    b = conn.execute("SELECT * FROM accounts WHERE id = ?", (into_id,)).fetchone()
    if a is None or b is None or from_id == into_id:
        raise UserError("Pick another account to merge into.")
    if is_debt(a["kind"]) != is_debt(b["kind"]):
        raise UserError("A card or line of credit can only be merged with another one.")
    moved = dropped = 0
    asked = []
    copies = conn.execute("SELECT * FROM transactions WHERE account_id = ? ORDER BY date, id", (from_id,)).fetchall()
    # As in an import: a copy's exact twin first (the same fingerprint, then the same bank ID), and
    # those rows leave the pool. Rows seen in a different kind of file share neither, so the rest pair
    # by date and amount, nearest first, from what's left.
    twins = {}
    for t in copies:
        twin = None
        if t["fingerprint"]:
            twin = conn.execute("SELECT * FROM transactions WHERE account_id = ? AND fingerprint = ?",
                                (into_id, t["fingerprint"])).fetchone()
        if twin is None and t["fitid"]:
            twin = conn.execute("SELECT * FROM transactions WHERE account_id = ? AND fitid = ?",
                                (into_id, t["fitid"])).fetchone()
        if twin is not None:
            twins[t["id"]] = twin
    paired = cross_kind_pairs(conn, from_id, into_id, skip_a=set(twins), skip_b={t["id"] for t in twins.values()})
    for copy_id, twin_id in paired:
        twins[copy_id] = conn.execute("SELECT * FROM transactions WHERE id = ?", (twin_id,)).fetchone()
    for t in copies:
        twin = twins.get(t["id"])
        if twin is None:
            if t["fitid"] and conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND fitid = ?",
                                           (into_id, t["fitid"])).fetchone():
                conn.execute("UPDATE transactions SET fitid = NULL WHERE id = ?", (t["id"],))
            conn.execute("UPDATE transactions SET account_id = ? WHERE id = ?", (into_id, t["id"]))
            moved += 1
        else:
            asked += _drop_copy(conn, t, twin)
            dropped += 1
    conn.execute("UPDATE transactions SET transfer_account_id = ? WHERE transfer_account_id = ?", (into_id, from_id))
    unlinked = [t["id"] for t in conn.execute("SELECT id FROM transactions WHERE account_id = ? AND "
                                              "transfer_account_id = ?", (into_id, into_id)).fetchall()]
    if a["bank"] and not b["bank"]:
        conn.execute("UPDATE accounts SET bank = ? WHERE id = ?", (a["bank"], into_id))
    for table in ("account_sources", "imports", "subscriptions"):
        conn.execute(f"UPDATE {table} SET account_id = ? WHERE account_id = ?", (into_id, from_id))
    if a["anchor_date"] is not None and (b["anchor_date"] is None or a["anchor_date"] > b["anchor_date"]):
        # The newer balance wins (the copy's usually came from the bank's file), and the copy's balance
        # check with it: its imports are this account's now.
        conn.execute("UPDATE accounts SET anchor_date = ?, anchor_balance = ?, anchor_source = ?, check_from = ? "
                     "WHERE id = ?", (a["anchor_date"], a["anchor_balance"], a["anchor_source"], a["check_from"], into_id))
    if is_debt(a["kind"]):
        old, new = payment_categories(conn).get(from_id), ensure_payment_envelope(conn, into_id)
        for r in conn.execute("SELECT month, assigned FROM budget WHERE category_id = ?", (old,)).fetchall():
            conn.execute("INSERT INTO budget (month, category_id, assigned) VALUES (?, ?, ?) ON CONFLICT(month, "
                         "category_id) DO UPDATE SET assigned = assigned + excluded.assigned", (r["month"], new, r["assigned"]))
    conn.execute("DELETE FROM accounts WHERE id = ?", (from_id,))
    return moved, dropped, unlinked, [i for i in dict.fromkeys(asked) if i not in unlinked]


def _drop_copy(conn, copy, twin):
    """Deletes `copy`, a second copy of `twin`, moving onto `twin` what only the copy has: a
    note, its subscription, its bank ID, and what you decided about it. What you decided
    stays decided (docs/adr/0005): if `twin` is yours it's kept as it is; if only the copy is
    yours, `twin` takes the whole decision (a category, a split, sending it back, being a
    transfer or not). Nothing the app did replaces a decision of yours. Returns the ids of
    other halves of transfers that lost their partner and are the app's to decide again."""
    asked = []
    partner = copy["transfer_id"]
    if copy["by_you"] and not twin["by_you"]:
        if twin["transfer_id"] and twin["transfer_id"] != partner:
            asked += transfers.unlink(conn, twin["id"])[1:]  # the app's pairing of the original goes
        conn.execute("UPDATE transactions SET category_id = ?, is_split = ?, status = ?, reason = ?, refund_of = ?, "
                     "transfer_account_id = ?, transfer_id = ?, by_you = 1 WHERE id = ?",
                     (copy["category_id"], copy["is_split"], copy["status"], copy["reason"], copy["refund_of"],
                      copy["transfer_account_id"], partner, twin["id"]))
        conn.execute("DELETE FROM splits WHERE transaction_id = ?", (twin["id"],))
        if copy["is_split"]:
            conn.execute("UPDATE splits SET transaction_id = ? WHERE transaction_id = ?", (twin["id"], copy["id"]))
        if partner:
            conn.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (twin["id"], partner))
        twin = conn.execute("SELECT * FROM transactions WHERE id = ?", (twin["id"],)).fetchone()
        partner = None  # it has a partner now (or never had one)
    if copy["note"] and not twin["note"]:
        conn.execute("UPDATE transactions SET note = ? WHERE id = ?", (copy["note"], twin["id"]))
    if copy["subscription_id"] and not twin["subscription_id"]:
        conn.execute("UPDATE transactions SET subscription_id = ? WHERE id = ?", (copy["subscription_id"], twin["id"]))
    conn.execute("UPDATE transactions SET refund_of = ? WHERE refund_of = ?", (twin["id"], copy["id"]))
    conn.execute("DELETE FROM transactions WHERE id = ?", (copy["id"],))
    if copy["fitid"] and not twin["fitid"] and not conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND fitid = ?", (twin["account_id"], copy["fitid"])).fetchone():
        conn.execute("UPDATE transactions SET fitid = ? WHERE id = ?", (copy["fitid"], twin["id"]))
    if partner is not None:  # the copy was paired by the app (or the original is yours)
        if twin["by_you"] or twin["transfer_id"]:
            asked.append(partner)  # never paired with yours; one already paired has its own
        else:
            transfers.link(conn, conn.execute("SELECT * FROM transactions WHERE id = ?", (twin["id"],)).fetchone(),
                           conn.execute("SELECT * FROM transactions WHERE id = ?", (partner,)).fetchone())
    return asked


# ── special categories ───────────────────────────────────────

def _group(conn, role, name):
    row = conn.execute("SELECT id FROM category_groups WHERE role = ? ORDER BY sort, id LIMIT 1", (role,)).fetchone()
    if row:
        return row["id"]
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM category_groups").fetchone()[0]
    if role == "income":
        sort = 10_000  # always last
    return conn.execute("INSERT INTO category_groups (name, role, sort) VALUES (?, ?, ?)",
                        (name, role, sort)).lastrowid


def income_category(conn):
    group = _group(conn, "income", "Income")
    row = conn.execute("SELECT id FROM categories WHERE group_id = ? ORDER BY sort, id LIMIT 1", (group,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO categories (group_id, name) VALUES (?, 'Income')", (group,)).lastrowid


def income_category_ids(conn):
    return {r["id"] for r in conn.execute(
        "SELECT c.id FROM categories c JOIN category_groups g ON g.id = c.group_id WHERE g.role = 'income'")}


def debt_group(conn):
    return _group(conn, "debt", DEBT_GROUP_NAME)


def ensure_payment_envelope(conn, account_id):
    row = conn.execute("SELECT id FROM categories WHERE debt_account_id = ?", (account_id,)).fetchone()
    if row:
        return row["id"]
    acct = conn.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    group = debt_group(conn)
    sort = conn.execute("SELECT COALESCE(MIN(sort), 1) - 1 FROM categories WHERE group_id = ?", (group,)).fetchone()[0]
    return conn.execute(
        "INSERT INTO categories (group_id, name, sort, debt_account_id) VALUES (?, ?, ?, ?)",
        (group, f"{acct['name']} payment", sort, account_id)).lastrowid


def payment_categories(conn):
    """{debt account id: its payment envelope's category id}"""
    return {r["debt_account_id"]: r["id"] for r in conn.execute(
        "SELECT id, debt_account_id FROM categories WHERE debt_account_id IS NOT NULL")}


def category_problem(conn, category_id, hidden_ok=False):
    """Why a transaction (or a recurring charge) can't go in this category, as (name, why) with
    why 'missing', 'payment' (a card's payment category: set-aside money, not spending) or
    'hidden' (not for a choice of yours); None when it can."""
    row = conn.execute("SELECT name, hidden, debt_account_id FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        return "", "missing"
    if row["debt_account_id"]:
        return row["name"], "payment"
    if row["hidden"] and not hidden_ok:
        return row["name"], "hidden"
    return None


def check_category(conn, category_id, hidden_ok=False):
    """Raises UserError, with a message to show, unless something can be filed in this
    category (see category_problem). The page only offers such categories; this is what keeps
    the budget's rules true for anything else that asks."""
    problem = category_problem(conn, category_id, hidden_ok)
    if problem is None:
        return
    name, why = problem
    if why == "missing":
        raise UserError("That category no longer exists.")
    if why == "payment":
        raise UserError(f"{name} is where the card's payments are set aside: pick a category for what was bought.")
    raise UserError(f"{name} is hidden: show it again, or pick another category.")


def interest_category(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'interest_category_id'").fetchone()
    if row and conn.execute("SELECT 1 FROM categories WHERE id = ?", (int(row["value"]),)).fetchone():
        return int(row["value"])
    group = debt_group(conn)
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM categories WHERE group_id = ?", (group,)).fetchone()[0]
    cid = conn.execute("INSERT INTO categories (group_id, name, sort) VALUES (?, ?, ?)",
                       (group, INTEREST_CATEGORY_NAME, sort)).lastrowid
    conn.execute("INSERT INTO meta (key, value) VALUES ('interest_category_id', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(cid),))
    return cid


def category_names(conn):
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM categories")}


def category_tree(conn):
    """Category groups in budget order, each with its categories in order."""
    groups = []
    for g in conn.execute("SELECT * FROM category_groups ORDER BY sort, id").fetchall():
        cats = [dict(r) for r in conn.execute(
            "SELECT id, name, hidden, monthly_budget, debt_account_id, sort FROM categories "
            "WHERE group_id = ? ORDER BY sort, id", (g["id"],))]
        groups.append({**dict(g), "categories": cats})
    return groups


def used_category_ids(conn):
    """Categories that have been used: a transaction or a split line in them, an example
    learnt from Actual, money assigned in any month, or a recurring charge filed there. A
    used category can only be hidden, never deleted: deleting it would send its
    transactions back to the inbox and take its money out of every month."""
    return {r[0] for r in conn.execute(
        "SELECT category_id FROM transactions WHERE category_id IS NOT NULL "
        "UNION SELECT category_id FROM splits WHERE category_id IS NOT NULL "
        "UNION SELECT category_id FROM examples "
        "UNION SELECT category_id FROM budget WHERE assigned != 0 "
        "UNION SELECT category_id FROM subscriptions WHERE category_id IS NOT NULL")}



def category_uses(conn, category_id):
    """What makes a category used (used_category_ids), counted: what the Categories
    pop-up says when a used one's ✕ is clicked."""
    one = lambda sql: conn.execute(sql, (category_id,)).fetchone()[0]
    return {"transactions": one("SELECT COUNT(*) FROM transactions WHERE category_id = ?")
                            + one("SELECT COUNT(*) FROM splits WHERE category_id = ?"),
            "assigned": bool(one("SELECT COUNT(*) FROM budget WHERE category_id = ? AND assigned != 0")),
            "recurring": one("SELECT COUNT(*) FROM subscriptions WHERE category_id = ?"),
            "examples": one("SELECT COUNT(*) FROM examples WHERE category_id = ?")}

def own_category_ids(conn):
    """Income and Interest & Fees, which the app needs: never deleted. Only looks (unlike
    income_category() and interest_category(), which make them when they're missing)."""
    income = conn.execute("SELECT c.id FROM categories c JOIN category_groups g ON g.id = c.group_id "
                          "WHERE g.role = 'income' ORDER BY g.sort, g.id, c.sort, c.id LIMIT 1").fetchone()
    interest = conn.execute("SELECT value FROM meta WHERE key = 'interest_category_id'").fetchone()
    return {int(r[0]) for r in (income, interest) if r}


def assignable_category_ids(conn):
    """Categories a transaction can be filed under: not hidden, not a payment envelope."""
    return {r["id"] for r in conn.execute(
        "SELECT id FROM categories WHERE hidden = 0 AND debt_account_id IS NULL")}


def find_category_by_name(conn, name):
    """By current name or a name it used to have (case-insensitive)."""
    wanted = str(name).strip().lower()
    for row in conn.execute("SELECT id, name, old_names FROM categories ORDER BY id"):
        names = [row["name"]] + [n for n in row["old_names"].split("|") if n]
        if wanted in (n.strip().lower() for n in names):
            return row["id"]
    return None


# ── payees ───────────────────────────────────────────────────

def get_or_create_payee(conn, name, kind="merchant", always_ask=False):
    """The payee with this name, or a new one. `always_ask` only applies to a new payee:
    anyone you e-transfer with starts with Always ask on (docs/adr/0001)."""
    row = conn.execute("SELECT id FROM payees WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO payees (name, kind, always_ask) VALUES (?, ?, ?)",
                        (name, kind, 1 if always_ask else 0)).lastrowid


def add_payee_key(conn, key, payee_id, how):
    conn.execute("INSERT INTO payee_keys (key, payee_id, how) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET payee_id = excluded.payee_id, how = excluded.how",
                 (key, payee_id, how))


def resolve_payee(conn, desc):
    """(payee_id, similar_payee_id) for a payees.Description. A similar-name match it's
    sure of is kept as the key's `how` ('similar': the inbox says "Matched to Safeway
    (similar name)"). `similar_payee_id` is a payee it wasn't sure enough of, to ask about."""
    row = conn.execute("SELECT payee_id FROM payee_keys WHERE key = ?", (desc.key,)).fetchone()
    if row:
        return row["payee_id"], None
    if desc.brand or desc.kind == "bank":
        payee_id = get_or_create_payee(conn, desc.name, desc.kind)
        add_payee_key(conn, desc.key, payee_id, "brand" if desc.brand else "exact")
        return payee_id, None
    known = [(r["key"], r["payee_id"], r["name"]) for r in conn.execute(
        "SELECT k.key, k.payee_id, p.name FROM payee_keys k JOIN payees p ON p.id = k.payee_id "
        "WHERE p.kind != 'bank'")]
    match = find_similar(desc.name, desc.key, known)
    if match and match[3]:
        add_payee_key(conn, desc.key, match[0], "similar")
        return match[0], None
    payee_id = get_or_create_payee(conn, desc.name, desc.kind, always_ask=transaction_hint(desc) == "etransfer")
    add_payee_key(conn, desc.key, payee_id, "exact")
    return payee_id, (match[0] if match else None)


def set_payee(conn, txn_id, name):
    """This transaction's bank description belongs to payee `name` (an existing one, or a
    new one): every transaction with the same bank description moves too, and the app
    remembers. Returns the payee's id."""
    txn = conn.execute("SELECT t.*, a.kind FROM transactions t JOIN accounts a ON a.id = t.account_id "
                       "WHERE t.id = ?", (txn_id,)).fetchone()
    old = txn["payee_id"]
    old_row = conn.execute("SELECT name, kind FROM payees WHERE id = ?", (old,)).fetchone() if old else None
    if old_row and old_row["name"].lower() == name.lower():
        return old
    others = conn.execute("SELECT COUNT(*) FROM transactions WHERE payee_id = ? AND id != ?", (old, txn_id)).fetchone()[0] \
        if old else 0
    exists = conn.execute("SELECT id FROM payees WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if old and not exists and others == 0:
        return rename_payee(conn, old, name)  # its only transaction: just rename it
    target = exists["id"] if exists else get_or_create_payee(
        conn, name, old_row["kind"] if old_row else "merchant", always_ask=txn["hint"] == "etransfer")
    key = describe(txn["raw_name"], txn["raw_memo"], txn["kind"]).key if txn["raw_name"] else None
    if key:
        add_payee_key(conn, key, target, "user")
        for other in conn.execute("SELECT t.id, t.raw_name, t.raw_memo, a.kind FROM transactions t JOIN accounts a "
                                  "ON a.id = t.account_id WHERE t.payee_id = ?", (old,)).fetchall():
            if describe(other["raw_name"], other["raw_memo"], other["kind"]).key == key:
                conn.execute("UPDATE transactions SET payee_id = ? WHERE id = ?", (target, other["id"]))
    conn.execute("UPDATE transactions SET payee_id = ? WHERE id = ?", (target, txn_id))
    if old and not conn.execute("SELECT 1 FROM transactions WHERE payee_id = ?", (old,)).fetchone() \
            and not conn.execute("SELECT 1 FROM payee_keys WHERE payee_id = ?", (old,)).fetchone():
        conn.execute("DELETE FROM payees WHERE id = ?", (old,))
    return target


def rename_payee(conn, payee_id, new_name):
    """Renames a payee, or merges it into the payee that already has that name."""
    new_name = " ".join(str(new_name).split())
    if not new_name:
        raise UserError("A payee needs a name.")
    other = conn.execute("SELECT id FROM payees WHERE name = ? COLLATE NOCASE AND id != ?",
                         (new_name, payee_id)).fetchone()
    if other is None:
        conn.execute("UPDATE payees SET name = ? WHERE id = ?", (new_name, payee_id))
        add_payee_key(conn, merchant_key(new_name), payee_id, "user")
        return payee_id
    merge_payees(conn, payee_id, other["id"])
    return other["id"]


def merge_payees(conn, from_id, into_id):
    if from_id == into_id:
        return
    for table in ("transactions", "examples", "subscriptions"):
        conn.execute(f"UPDATE {table} SET payee_id = ? WHERE payee_id = ?", (into_id, from_id))
    conn.execute("UPDATE payee_keys SET payee_id = ?, how = 'user' WHERE payee_id = ?", (into_id, from_id))
    conn.execute("DELETE FROM payees WHERE id = ?", (from_id,))
