"""Core bookkeeping shared by the importer, the budget and the page: accounts and their
balances, the special categories (Income, debt payment envelopes, Interest & Fees),
and finding or creating payees.

Every function takes an open connection (`db.write()` / `db.read()`); none of them
commits. Amounts are cents.
"""

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
