"""Core bookkeeping shared by the importer, the budget and the page: accounts and their
balances, the special categories (Income, debt payment envelopes, Interest & Fees),
and finding or creating payees.

Every function takes an open connection (`db.write()` / `db.read()`); none of them
commits. Amounts are cents.
"""

import transfers
from payees import describe, find_similar, merchant_key, transaction_hint
from util import day_before

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
    from the one balance the user gave us. None if they haven't given one yet."""
    acct = conn.execute("SELECT anchor_date, anchor_balance FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if acct is None or acct["anchor_date"] is None:
        return None
    anchor = acct["anchor_date"]
    as_of = as_of or "9999-12-31"
    delta = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN date > :anchor AND date <= :asof THEN amount "
        "                         WHEN date > :asof AND date <= :anchor THEN -amount ELSE 0 END), 0) "
        "FROM transactions WHERE account_id = :id",
        {"anchor": anchor, "asof": as_of, "id": account_id}).fetchone()[0]
    return acct["anchor_balance"] + delta


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
    `into_id` already has are dropped (keeping anything you decided about the copy), the
    rest move over, and `from_id` goes. Returns (moved, dropped, ids): `ids` were a
    transfer between the two copies, both halves now in one account, for the inbox to
    ask about again."""
    a = conn.execute("SELECT * FROM accounts WHERE id = ?", (from_id,)).fetchone()
    b = conn.execute("SELECT * FROM accounts WHERE id = ?", (into_id,)).fetchone()
    if a is None or b is None or from_id == into_id:
        raise ValueError("Pick another account to merge into.")
    if is_debt(a["kind"]) != is_debt(b["kind"]):
        raise ValueError("A card or line of credit can only be merged with another one.")
    moved = dropped = 0
    for t in conn.execute("SELECT * FROM transactions WHERE account_id = ? ORDER BY date, id", (from_id,)).fetchall():
        twin = None
        if t["fingerprint"]:
            twin = conn.execute("SELECT * FROM transactions WHERE account_id = ? AND fingerprint = ?",
                                (into_id, t["fingerprint"])).fetchone()
        if twin is None and t["fitid"]:
            twin = conn.execute("SELECT * FROM transactions WHERE account_id = ? AND fitid = ?",
                                (into_id, t["fitid"])).fetchone()
        if twin is None:
            if t["fitid"] and conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND fitid = ?",
                                           (into_id, t["fitid"])).fetchone():
                conn.execute("UPDATE transactions SET fitid = NULL WHERE id = ?", (t["id"],))
            conn.execute("UPDATE transactions SET account_id = ? WHERE id = ?", (into_id, t["id"]))
            moved += 1
        else:
            _drop_copy(conn, t, twin)
            dropped += 1
    conn.execute("UPDATE transactions SET transfer_account_id = ? WHERE transfer_account_id = ?", (into_id, from_id))
    unlinked = [t["id"] for t in conn.execute("SELECT id FROM transactions WHERE account_id = ? AND "
                                              "transfer_account_id = ?", (into_id, into_id)).fetchall()]
    if a["bank"] and not b["bank"]:
        conn.execute("UPDATE accounts SET bank = ? WHERE id = ?", (a["bank"], into_id))
    for table in ("account_sources", "imports", "subscriptions"):
        conn.execute(f"UPDATE {table} SET account_id = ? WHERE account_id = ?", (into_id, from_id))
    if a["anchor_date"] is not None and (b["anchor_date"] is None or a["anchor_date"] > b["anchor_date"]):
        # The newer balance wins (the copy's usually came from the bank's file).
        conn.execute("UPDATE accounts SET anchor_date = ?, anchor_balance = ? WHERE id = ?",
                     (a["anchor_date"], a["anchor_balance"], into_id))
    if is_debt(a["kind"]):
        old, new = payment_categories(conn).get(from_id), ensure_payment_envelope(conn, into_id)
        for r in conn.execute("SELECT month, assigned FROM budget WHERE category_id = ?", (old,)).fetchall():
            conn.execute("INSERT INTO budget (month, category_id, assigned) VALUES (?, ?, ?) ON CONFLICT(month, "
                         "category_id) DO UPDATE SET assigned = assigned + excluded.assigned", (r["month"], new, r["assigned"]))
    conn.execute("DELETE FROM accounts WHERE id = ?", (from_id,))
    return moved, dropped, unlinked


def _drop_copy(conn, copy, twin):
    """Deletes `copy`, a second copy of `twin`, moving onto `twin` what only the copy has:
    what you decided about it (a category, a split, sending it back), a note, its
    subscription, its transfer, its bank ID."""
    if copy["by_you"] and not twin["by_you"] and not copy["transfer_account_id"] and not twin["transfer_account_id"]:
        conn.execute("UPDATE transactions SET category_id = ?, is_split = ?, status = ?, reason = ?, refund_of = ?, "
                     "by_you = 1 WHERE id = ?", (copy["category_id"], copy["is_split"], copy["status"], copy["reason"],
                                                 copy["refund_of"], twin["id"]))
        if copy["is_split"]:
            conn.execute("DELETE FROM splits WHERE transaction_id = ?", (twin["id"],))
            conn.execute("UPDATE splits SET transaction_id = ? WHERE transaction_id = ?", (twin["id"], copy["id"]))
    if copy["note"] and not twin["note"]:
        conn.execute("UPDATE transactions SET note = ? WHERE id = ?", (copy["note"], twin["id"]))
    if copy["subscription_id"] and not twin["subscription_id"]:
        conn.execute("UPDATE transactions SET subscription_id = ? WHERE id = ?", (copy["subscription_id"], twin["id"]))
    conn.execute("UPDATE transactions SET refund_of = ? WHERE refund_of = ?", (twin["id"], copy["id"]))
    other = conn.execute("SELECT * FROM transactions WHERE id = ?", (copy["transfer_id"],)).fetchone() \
        if copy["transfer_id"] else None
    conn.execute("DELETE FROM transactions WHERE id = ?", (copy["id"],))
    if copy["fitid"] and not twin["fitid"] and not conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND fitid = ?", (twin["account_id"], copy["fitid"])).fetchone():
        conn.execute("UPDATE transactions SET fitid = ? WHERE id = ?", (copy["fitid"], twin["id"]))
    if other is not None and not twin["transfer_id"]:
        transfers.link(conn, conn.execute("SELECT * FROM transactions WHERE id = ?", (twin["id"],)).fetchone(),
                       conn.execute("SELECT * FROM transactions WHERE id = ?", (other["id"],)).fetchone())


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
        raise ValueError("A payee needs a name.")
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
