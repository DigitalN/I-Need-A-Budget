"""Money moving between your own accounts: paying the card from chequing, moving a
balance to a line of credit.

A transfer shows up twice, once in each account's export, often a day or two apart
("Tangerine Credit Card Payment" -1,250.00 in Chequing, "PAYMENT - THANK YOU"
+1,250.00 on the card). The two halves can be at different banks. Both halves get `transfer_account_id` (the other account) and,
once both are imported, `transfer_id` (each other). Transfers have no category: they
aren't spending. The budget counts a card payment once, from the cash side.

If only one half is imported so far, it's still marked as a transfer to the obvious
account, and the other half pairs with it when it arrives.
"""

import re

PAIR_WINDOW_DAYS = 7


def _accounts(conn):
    return {r["id"]: r for r in conn.execute("SELECT id, name, kind, bank FROM accounts WHERE closed = 0")}


def find_counterpart(conn, txn, window=PAIR_WINDOW_DAYS):
    """The other half of a transfer: the opposite amount in another account, within a
    week, not paired yet, and looking like a transfer on at least one side."""
    return conn.execute(
        """
        SELECT * FROM transactions
         WHERE account_id != :account AND amount = :opposite AND transfer_id IS NULL AND id != :id
           AND date BETWEEN date(:date, :minus) AND date(:date, :plus)
           AND (hint = 'transfer' OR :hint = 'transfer' OR transfer_account_id = :account
                OR :target = account_id)
           AND (transfer_account_id IS NULL OR transfer_account_id = :account)
         ORDER BY abs(julianday(date) - julianday(:date)), id
         LIMIT 1
        """, {"account": txn["account_id"], "opposite": -txn["amount"], "id": txn["id"], "date": txn["date"],
              "minus": f"-{window} days", "plus": f"+{window} days", "hint": txn["hint"],
              "target": txn["transfer_account_id"] or 0}).fetchone()


def link(conn, a, b, accounts=None):
    accounts = accounts or _accounts(conn)
    for this, other in ((a, b), (b, a)):
        verb = "from" if this["amount"] > 0 else "to"
        other_name = accounts[other["account_id"]]["name"] if other["account_id"] in accounts else "another account"
        conn.execute(
            "UPDATE transactions SET transfer_id = ?, transfer_account_id = ?, category_id = NULL, is_split = 0, "
            "status = 'done', reason = ? WHERE id = ?",
            (other["id"], other["account_id"], f"Transfer {verb} {other_name}", this["id"]))
    conn.execute("DELETE FROM splits WHERE transaction_id IN (?, ?)", (a["id"], b["id"]))


def guess_other_account(conn, txn, accounts=None):
    """For a transfer whose other half isn't imported: the one account it can be."""
    accounts = accounts or _accounts(conn)
    here = accounts.get(txn["account_id"])
    if here is None:
        return None
    text = f"{txn['raw_name']} {txn['raw_memo']}".upper()
    if here["kind"] in ("chequing", "savings", "cash"):
        wanted = ("credit_card",) if "CREDIT CARD" in text else \
            ("line_of_credit",) if "LINE OF CREDIT" in text else ("credit_card", "line_of_credit")
    else:
        wanted = ("chequing",)
    options = [a for a in accounts.values() if a["kind"] in wanted and a["id"] != here["id"]]
    if len(options) > 1:
        # Several cards (at different banks): only one the text names, never a guess.
        options = [a for a in options if _named_in(a, text)]
    return options[0] if len(options) == 1 else None


def _named_in(account, text):
    """The account's bank, or a distinctive word of its name, is in `text` (upper case)."""
    words = [account["bank"]] if account["bank"] else []
    words += [w for w in re.split(r"[^A-Za-z0-9]+", account["name"])
              if len(w) >= 3 and w.upper() not in ("CARD", "CREDIT", "PAYMENT", "THE", "AND", "LINE", "ACCOUNT")]
    return any(re.search(rf"\b{re.escape(w.upper())}\b", text) for w in words)


def pair(conn, txn):
    """Marks `txn` as a transfer if it is one. Returns True if it did."""
    accounts = _accounts(conn)
    if txn["transfer_id"]:
        return True
    other = find_counterpart(conn, txn)
    if other is not None:
        link(conn, txn, other, accounts)
        return True
    if txn["hint"] != "transfer" and not txn["transfer_account_id"]:
        return False
    target = accounts.get(txn["transfer_account_id"]) if txn["transfer_account_id"] else guess_other_account(conn, txn, accounts)
    if target is None:
        return False
    verb = "from" if txn["amount"] > 0 else "to"
    conn.execute(
        "UPDATE transactions SET transfer_account_id = ?, category_id = NULL, is_split = 0, status = 'done', "
        "reason = ? WHERE id = ?",
        (target["id"], f"Transfer {verb} {target['name']} (its other half isn't imported yet)", txn["id"]))
    conn.execute("DELETE FROM splits WHERE transaction_id = ?", (txn["id"],))
    return True


def make_transfer(conn, txn_id, account_id):
    """The user says this is a transfer to/from `account_id`: pair it with the other half
    if that's already imported, otherwise just mark it."""
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    unlink(conn, txn_id)
    conn.execute("UPDATE transactions SET transfer_account_id = ? WHERE id = ?", (account_id, txn_id))
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    other = conn.execute(
        """
        SELECT * FROM transactions WHERE account_id = ? AND amount = ? AND transfer_id IS NULL AND id != ?
           AND date BETWEEN date(?, '-10 days') AND date(?, '+10 days')
         ORDER BY abs(julianday(date) - julianday(?)) LIMIT 1
        """, (account_id, -txn["amount"], txn_id, txn["date"], txn["date"], txn["date"])).fetchone()
    if other is not None:
        link(conn, txn, other)
    else:
        pair(conn, txn)


def unlink(conn, txn_id):
    """Stops treating a transaction (and its other half) as a transfer."""
    txn = conn.execute("SELECT transfer_id FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    ids = [txn_id] + ([txn["transfer_id"]] if txn and txn["transfer_id"] else [])
    for i in ids:
        conn.execute("UPDATE transactions SET transfer_id = NULL, transfer_account_id = NULL, status = 'review', "
                     "reason = 'No longer a transfer' WHERE id = ?", (i,))
