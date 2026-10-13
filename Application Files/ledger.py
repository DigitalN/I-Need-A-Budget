"""Core bookkeeping shared by the importer, the budget and the page: accounts and their
balances, the special categories (Income, debt payment envelopes, Interest & Fees),
and finding or creating payees.

Every function takes an open connection (`db.write()` / `db.read()`); none of them
commits. Amounts are cents.
"""

import transfers
from payees import find_similar, merchant_key
from util import day_before

CASH_KINDS = ("chequing", "savings", "cash")
DEBT_KINDS = ("credit_card", "line_of_credit")
ACCOUNT_KINDS = CASH_KINDS + DEBT_KINDS
KIND_LABELS = {"chequing": "Chequing", "savings": "Savings", "cash": "Cash",
               "credit_card": "Credit card", "line_of_credit": "Line of credit"}

DEBT_GROUP_NAME = "Debt Payments"
INTEREST_CATEGORY_NAME = "Interest & Fees"


def is_debt(kind):
    return kind in DEBT_KINDS


# ── accounts ──────────────────────────────────────────────────

def create_account(conn, name, kind, keys=(), last4=None, anchor_date=None, anchor_balance=None):
    if kind not in ACCOUNT_KINDS:
        raise ValueError(f"Unknown account type: {kind}")
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM accounts").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO accounts (name, kind, last4, anchor_date, anchor_balance, sort) VALUES (?, ?, ?, ?, ?, ?)",
        (name, kind, last4, anchor_date, anchor_balance, sort))
    account_id = cur.lastrowid
    for key in keys:
        conn.execute("INSERT OR IGNORE INTO account_sources (key, account_id) VALUES (?, ?)", (key, account_id))
    if is_debt(kind):
        ensure_payment_envelope(conn, account_id)
        interest_category(conn)
    return account_id


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
    rest move over, and `from_id` goes. Returns (moved, dropped)."""
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
    for t in conn.execute("SELECT id FROM transactions WHERE account_id = ? AND transfer_account_id = ?",
                          (into_id, into_id)).fetchall():
        transfers.unlink(conn, t["id"])  # was a transfer between the two copies
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
    return moved, dropped


def _drop_copy(conn, copy, twin):
    """Deletes `copy`, a second copy of `twin`, moving onto `twin` what only the copy has:
    your filing, a split, a note, its subscription, its transfer, its bank ID."""
    if twin["status"] == "review" and copy["reason"] in ("Filed by you", "Split by you"):
        conn.execute("UPDATE transactions SET category_id = ?, is_split = ?, status = 'done', reason = ?, refund_of = ? "
                     "WHERE id = ?", (copy["category_id"], copy["is_split"], copy["reason"], copy["refund_of"], twin["id"]))
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

def get_or_create_payee(conn, name, kind="merchant"):
    row = conn.execute("SELECT id FROM payees WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO payees (name, kind) VALUES (?, ?)", (name, kind)).lastrowid


def add_payee_key(conn, key, payee_id, how):
    conn.execute("INSERT INTO payee_keys (key, payee_id, how) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET payee_id = excluded.payee_id, how = excluded.how",
                 (key, payee_id, how))


def resolve_payee(conn, desc):
    """(payee_id, note, similar_payee_id) for a payees.Description. `note` explains a
    similar-name match it was sure of ("Matched to Safeway (similar name)"), else ''.
    `similar_payee_id` is a payee it wasn't sure enough of, to ask about."""
    row = conn.execute("SELECT payee_id FROM payee_keys WHERE key = ?", (desc.key,)).fetchone()
    if row:
        return row["payee_id"], "", None
    if desc.brand or desc.kind == "bank":
        payee_id = get_or_create_payee(conn, desc.name, desc.kind)
        add_payee_key(conn, desc.key, payee_id, "brand" if desc.brand else "exact")
        return payee_id, "", None
    known = [(r["key"], r["payee_id"], r["name"]) for r in conn.execute(
        "SELECT k.key, k.payee_id, p.name FROM payee_keys k JOIN payees p ON p.id = k.payee_id "
        "WHERE p.kind != 'bank'")]
    match = find_similar(desc.name, desc.key, known)
    if match and match[3]:
        payee_id, name = match[0], match[1]
        add_payee_key(conn, desc.key, payee_id, "similar")
        return payee_id, f"Matched to {name} (similar name)", None
    payee_id = get_or_create_payee(conn, desc.name, desc.kind)
    add_payee_key(conn, desc.key, payee_id, "exact")
    return payee_id, "", (match[0] if match else None)


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
