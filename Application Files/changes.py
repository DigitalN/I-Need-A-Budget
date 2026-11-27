# SPDX-License-Identifier: GPL-3.0-only
"""What changed: a budget changed on two computers, each side compared with the save both
started from (docs/adr/0008, issue #184).

When two computers have each changed the budget since the save they both started from, the
app shows what each one changed and asks which to keep. This module works out what to show.
It only reads: no window, no network, no files written, the same on any system.

    compare(start, this, other) → {"this": <side>, "other": <side>}
    what_changed(start, side)   → <side>

Each budget is one of:
- a path to a complete budget file nobody writes while it's read: a save, the copy of the save
  a computer started from, a backup, or a copy made with VACUUM INTO (budget_folder's
  `two_computers()` gives all three that way). It's opened `immutable`: nothing is written,
  locked or left beside it (no `-wal`, no `-shm`), and a network path (`\\\\server\\share\\…`, a
  mapped drive) works. A file with changes still in its `-wal` is refused (ValueError): it's
  open somewhere, so pass a connection to it instead;
- an open `sqlite3.Connection`, or a `db.Database` (under its lock): a budget the app keeps
  writing, such as this computer's working copy. It's read inside one transaction, so all of it
  is from one moment.

A side is:

    {"changed": bool,          # anything listed at all
     "summary": str,           # one line: "1 file imported (14 transactions) · 9 categorized · …"
     "sections": [{"kind": str, "title": str, "items": [{"text": str, "app": bool}, …]}, …]}

`summary` is the sections' titles joined by " · ", in the order below; "Show every change"
lists every section's items under its title. An item's `app` is true for what the app did on
its own, and its text says so: the re-check categorizing or pairing a transfer, a transaction
it decides about again ("back in the Inbox"), the balance check's balance adjustments after
an import, a recurring charge's price following a charge as it arrived. Otherwise it's false:
yours, or what can't be told apart from yours (an import: a file you dropped and one the app
found in Downloads leave nothing to tell them apart). The kinds, in order: imported,
import_removed, not_imported, categorized, split, transfer, sent_back, back_in_inbox, edited,
added, deleted, balance_adjustment, assigned, budgeted, categories, accounts,
recurring_charges, payees, settings, actual.

Rows are matched by id, and each side is only ever compared with the start, so an id both
sides added is two different things. SQLite gives a deleted newest row's id to the next one,
so a row under the same id is only the same thing when what it keeps says so: a transaction,
account, import or recurring charge made at the same moment (an account of the same type); a
category with the same name or the start's among its earlier names (a card's payment category:
the same card); a category group with the same name or the same categories in it; a payee with
the same transactions or the same bank wording that finds it. Otherwise it's one removed and
another added, never a rename.

Some differences are never changes: this session's undo, backups' own meta, what the budget
folder keeps about saves (`BOOKKEEPING_META`), the inbox's reasons (only text to show), a group
folded on the Budget page, a recurring charge's price type as the app works it out (only the
one you chose is kept), and what the app keeps for itself (how bank wording finds a payee, CSV
layouts, which file each transaction was in, what a file's row replaced, remembered answers
about files). Anything else in the budget's meta that isn't one of its settings is listed as
"Something else in the budget changed", so a key nobody described is seen rather than lost.

Amounts are written as the page writes them ("−$4.50", "$1,234.00"), months as "December 2026"
and days as "Oct 3", in docs/CONTEXT.md's words.
"""

import os
import sqlite3
from pathlib import Path
from urllib.request import pathname2url

import backups
from ledger import DEBT_KINDS, KIND_LABELS
from payees import ADDED_HALF, ADJUSTMENT
from settings import BUDGET_RAN_ON, validate_budget
from subscriptions import PRICINGS
from util import month_name, short_day

# Meta keys that are the app's own bookkeeping, never a change: backups' (what the last backup
# was), the budget folder's (`save`, backups.SAVE_KEY: the save this budget is or started from,
# who saved it, when, the versions), the schema's version, and flags for work done once.
BOOKKEEPING_META = {*backups.BACKUP_META, "save", "schema_version", "onboarded", "read_imported_files",
                    "currency_read", "last_onboarding_account", "interest_category_id"}

# The budget's own settings and the other meta a person changes, as each is shown.
OVERSPENDING_LABELS = {"next_month": "Next month", "this_month": "This month", "carry_over": "Carry over"}
PRICING_LABELS = {"fixed": "Same Price", "currency": "Same Price, Different Currency",
                  "varies": "Fluctuates Every Time"}
assert set(PRICING_LABELS) == set(PRICINGS)
_SETTINGS = ("overspending", "average_months", "budget_start", "currency")
_EVERY = {"weekly": "a week", "monthly": "a month", "yearly": "a year"}

_KINDS = ("imported", "import_removed", "not_imported", "categorized", "split", "transfer", "sent_back",
          "back_in_inbox", "edited", "added", "deleted", "balance_adjustment", "assigned", "budgeted", "categories",
          "accounts", "recurring_charges", "payees", "settings", "actual")

# A section's title when all its items are one kind of change: (for one, for {n}). Mixed, it's
# "{n} changes to <its things>". Each item is one thing: one account, one recurring charge…
_TITLES = {
    ("balance_adjustment", "added"): ("1 balance adjustment added", "{n} balance adjustments added"),
    ("balance_adjustment", "removed"): ("1 balance adjustment removed", "{n} balance adjustments removed"),
    ("balance_adjustment", "changed"): ("1 balance adjustment changed", "{n} balance adjustments changed"),
    ("categories", "group added"): ("1 category group added", "{n} category groups added"),
    ("categories", "group deleted"): ("1 category group deleted", "{n} category groups deleted"),
    ("categories", "group renamed"): ("1 category group renamed", "{n} category groups renamed"),
    ("categories", "group moved"): ("1 category group moved", "{n} category groups moved"),
    ("categories", "added"): ("1 category added", "{n} categories added"),
    ("categories", "deleted"): ("1 category deleted", "{n} categories deleted"),
    ("categories", "renamed"): ("1 category renamed", "{n} categories renamed"),
    ("categories", "hidden"): ("1 category hidden", "{n} categories hidden"),
    ("categories", "shown"): ("1 category shown again", "{n} categories shown again"),
    ("categories", "moved"): ("1 category moved", "{n} categories moved"),
    ("categories", "notes"): ("Notes changed for 1 category", "Notes changed for {n} categories"),
    ("accounts", "added"): ("1 account added", "{n} accounts added"),
    ("accounts", "deleted"): ("1 account deleted", "{n} accounts deleted"),
    ("accounts", "merged"): ("1 account merged", "{n} accounts merged"),
    ("accounts", "renamed"): ("1 account renamed", "{n} accounts renamed"),
    ("accounts", "edited"): ("1 account edited", "{n} accounts edited"),
    ("accounts", "closed"): ("1 account closed", "{n} accounts closed"),
    ("accounts", "reopened"): ("1 account reopened", "{n} accounts reopened"),
    ("accounts", "balance"): (None, "{n} balances set"),
    ("recurring_charges", "added"): ("1 recurring charge added", "{n} recurring charges added"),
    ("recurring_charges", "stopped"): ("Stopped tracking 1 recurring charge", "Stopped tracking {n} recurring charges"),
    ("recurring_charges", "not recurring"): ("1 charge isn't recurring", "{n} charges aren't recurring"),
    ("recurring_charges", "edited"): ("1 recurring charge changed", "{n} recurring charges changed"),
    ("payees", "merged"): ("1 payee merged into another", "{n} payees merged into others"),
    ("payees", "moved"): ("Transactions moved to another payee", "Transactions moved between payees ({n})"),
    ("payees", "renamed"): ("1 payee renamed", "{n} payees renamed"),
    ("payees", "always ask"): ("Always ask changed for 1 payee", "Always ask changed for {n} payees"),
}
_THINGS = {"balance_adjustment": "balance adjustments", "categories": "categories", "accounts": "accounts",
           "recurring_charges": "recurring charges", "payees": "payees"}


def compare(start, this, other):
    """What each computer changed since the save both started from."""
    begin = _read(start)
    return {"this": _Side(begin, _read(this)).result(), "other": _Side(begin, _read(other)).result()}


def what_changed(start, side):
    """What one budget changed since `start`."""
    return _Side(_read(start), _read(side)).result()


# ── words ──

def money(cents):
    """As the page's money(): 123456 → '$1,234.56', -450 → '−$4.50'."""
    whole, part = divmod(abs(int(cents)), 100)
    return f"{'−' if cents < 0 else ''}${whole:,}.{part:02d}"


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _owner(name):
    """Food → Food's, Bills → Bills'."""
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _quoted(text, limit=40):
    text = " ".join(str(text).split())
    return f"“{text if len(text) <= limit else text[:limit - 1].rstrip() + '…'}”"


def _day(day):
    return short_day(day) if day else "no date"


def _long_day(day):
    return f"{short_day(day)}, {day[:4]}"


# ── reading a budget ──

# The columns each table is read for: only what's compared or shown.
_COLUMNS = {
    "accounts": ("id", "name", "kind", "bank", "apr", "planned_payment", "closed", "anchor_date", "anchor_balance",
                 "anchor_source", "created_at"),
    "category_groups": ("id", "name", "role", "sort"),
    "categories": ("id", "group_id", "name", "sort", "hidden", "monthly_budget", "debt_account_id", "old_names", "note"),
    "payees": ("id", "name", "always_ask"),
    "imports": ("id", "filename", "sha256", "account_id", "imported_at", "anchor_before_date", "anchor_before_balance"),
    "subscriptions": ("id", "name", "amount", "cadence", "kind", "status", "pricing", "ends_on", "category_id", "note",
                      "created_at"),
    "transactions": ("id", "account_id", "date", "amount", "payee_id", "hint", "category_id", "is_split",
                     "transfer_account_id", "transfer_id", "subscription_id", "status", "by_you", "note", "import_id",
                     "created_at"),
}


class _Budget:
    """Everything this module compares, read once from one budget."""

    def __init__(self, conn):
        def have(table):
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

        def rows(table, extra=()):
            """The table's rows as dicts of _COLUMNS[table] (None for a column an older budget doesn't have
            yet: none at all for a table it doesn't have) and of `extra`: (name, SQL, the columns it needs)."""
            present = have(table)
            if not present:
                return []
            wanted = _COLUMNS.get(table, ())
            picked = [c for c in wanted if c in present] + [f"{sql} AS {name}" for name, sql, needs in extra
                                                            if set(needs) <= present]
            names = [c for c in wanted if c in present] + [name for name, _, needs in extra if set(needs) <= present]
            out = [dict(zip(names, r)) for r in conn.execute(f"SELECT {', '.join(picked)} FROM {table}")]
            missing = dict.fromkeys(n for n in [*wanted, *(name for name, _, _ in extra)] if n not in names)
            if missing:
                for row in out:
                    row.update(missing)
            return out

        def by_id(table, extra=()):
            return {r["id"]: r for r in rows(table, extra)}

        self.meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
        self.accounts = by_id("accounts")
        self.groups = by_id("category_groups")
        self.categories = by_id("categories")
        self.payees = by_id("payees")
        self.imports = by_id("imports")
        self.subscriptions = by_id("subscriptions")
        # Where a transaction came from, not its fingerprint or bank ID (long, and never shown); the bank's
        # wording only where there's no payee to name it by.
        self.transactions = by_id("transactions", (
            ("from_file", "(fingerprint IS NOT NULL OR fitid IS NOT NULL)", ("fingerprint", "fitid")),
            ("raw_name", "CASE WHEN payee_id IS NULL THEN raw_name END", ("raw_name", "payee_id"))))
        self.keys = {}  # payee id → the bank wording that finds it (payee_keys)
        if have("payee_keys"):
            for key, payee_id in conn.execute("SELECT key, payee_id FROM payee_keys"):
                self.keys.setdefault(payee_id, set()).add(key)
        self.splits, self.split_notes = {}, {}
        if have("splits"):
            for txn_id, category_id, amount, note in conn.execute(
                    "SELECT transaction_id, category_id, amount, note FROM splits ORDER BY id"):
                self.splits.setdefault(txn_id, []).append((category_id, amount))
                self.split_notes.setdefault(txn_id, []).append(note or "")
        self.assigned = {(month, cid): cents for month, cid, cents in
                         conn.execute("SELECT month, category_id, assigned FROM budget")} if have("budget") else {}
        self.examples = {r[0] for r in conn.execute("SELECT id FROM examples")} if have("examples") else set()
        self.ignored = dict(conn.execute("SELECT sha256, filename FROM ignored_files")) if have("ignored_files") else {}

    def settings(self):
        """Overspending and Usual's months as the budget runs on them (settings.budget_settings)."""
        months = self.meta.get("average_months")
        return validate_budget({"overspending": self.meta.get("overspending"),
                                "average_months": int(months) if months and months.isdigit() else None}, BUDGET_RAN_ON)


def _read(source):
    if isinstance(source, _Budget):
        return source
    if isinstance(source, sqlite3.Connection):
        return _snapshot(source)
    if hasattr(source, "read") and hasattr(source, "conn"):  # a db.Database: under its lock
        with source.read() as conn:
            return _snapshot(conn)
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"No budget at {path}")
    wal = Path(f"{path}-wal")
    if wal.is_file() and wal.stat().st_size:
        raise ValueError(f"{path.name} has changes in its -wal file, so it's open somewhere: pass a connection to it")
    conn = sqlite3.connect(f"{_uri(path)}?mode=ro&immutable=1", uri=True)
    try:
        return _Budget(conn)
    finally:
        conn.close()


def _uri(path):
    """A file: URI SQLite opens, for any path: C:\\… or /Users/…, and \\\\server\\share\\… (file:////server/share/…:
    SQLite refuses a host in the URI). Not resolved: on Windows that would turn a mapped drive into its share."""
    url = pathname2url(os.path.abspath(path))
    if url.startswith("//") and not url.startswith("///"):
        url = "//" + url
    return f"file:{url}"


def _snapshot(conn):
    """A budget the app may be writing, read inside one transaction: everything from one moment."""
    began = not conn.in_transaction
    if began:
        conn.execute("BEGIN")
    try:
        return _Budget(conn)
    finally:
        if began:
            conn.execute("ROLLBACK")


def _split_rows(start, side, same):
    """(gone, new, both) between two tables of rows by id: rows only the start has, rows only the
    side has, and (start's, side's) pairs. A row under the same id that `same(start's, side's)`
    says is another thing is two rows: one gone, one new."""
    gone, new, both = {}, {}, []
    for row_id, b in start.items():
        s = side.get(row_id)
        if s is not None and same(b, s):
            both.append((b, s))
        else:
            gone[row_id] = b
    paired = {s["id"] for _, s in both}
    new = {row_id: s for row_id, s in side.items() if row_id not in paired}
    return gone, new, both


def _made_alike(*columns):
    """Rows made at the same moment (and alike in what never changes about them) are one row."""
    return lambda b, s: all(b[c] == s[c] for c in columns)


def _same_category(b, s):
    """A category keeps its name, or has the start's among its earlier names (rename_category keeps
    them); a card's payment category is named after its card, so it's the same card's."""
    if b["debt_account_id"] is not None or s["debt_account_id"] is not None:
        return b["debt_account_id"] == s["debt_account_id"]
    return b["name"] == s["name"] or b["name"] in (s["old_names"] or "").split("|")


def _moved(start_order, side_order):
    """The ids in both orders whose place among the others changed: the fewest that, moved, turn
    the start's order into the side's (those outside a longest run kept in the same order)."""
    common = set(start_order) & set(side_order)
    rank = {i: n for n, i in enumerate(i for i in start_order if i in common)}
    seq = [i for i in side_order if i in common]
    # Longest increasing subsequence of the ranks, by patience sorting.
    tails, tail_at, before = [], [], [None] * len(seq)
    for k, i in enumerate(seq):
        r = rank[i]
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if tails[mid] < r:
                lo = mid + 1
            else:
                hi = mid
        before[k] = tail_at[lo - 1] if lo else None
        if lo == len(tails):
            tails.append(r)
            tail_at.append(k)
        else:
            tails[lo], tail_at[lo] = r, k
    kept, k = set(), tail_at[-1] if tail_at else None
    while k is not None:
        kept.add(seq[k])
        k = before[k]
    return [i for i in seq if i not in kept]


# ── one side ──

class _Side:
    def __init__(self, start, side):
        self.start, self.side = start, side
        self.sections = {kind: [] for kind in _KINDS}
        self.verbs = {kind: [] for kind in _KINDS}
        # What's the same thing in both, what's gone and what's new, table by table.
        self.a_gone, self.a_new, self.a_both = _split_rows(start.accounts, side.accounts,
                                                           _made_alike("created_at", "kind"))
        self.i_gone, self.i_new, _ = _split_rows(start.imports, side.imports, _made_alike("imported_at", "sha256"))
        self.t_gone, self.t_new, self.t_both = _split_rows(start.transactions, side.transactions,
                                                           _made_alike("created_at"))
        self.c_gone, self.c_new, self.c_both = _split_rows(start.categories, side.categories, _same_category)
        kept_in = {b["group_id"] for b, s in self.c_both if b["group_id"] == s["group_id"]}
        self.g_gone, self.g_new, self.g_both = _split_rows(
            start.groups, side.groups, lambda b, s: b["role"] == s["role"] and (b["name"] == s["name"] or b["id"] in kept_in))
        paid = {b["payee_id"] for b, s in self.t_both if b["payee_id"] is not None and b["payee_id"] == s["payee_id"]}
        self.p_gone, self.p_new, self.p_both = _split_rows(
            start.payees, side.payees, lambda b, s: b["id"] in paid or b["name"] == s["name"]
            or bool(start.keys.get(b["id"], set()) & side.keys.get(s["id"], set())))
        self.s_gone, self.s_new, self.s_both = _split_rows(start.subscriptions, side.subscriptions,
                                                           _made_alike("created_at"))
        self.copies = self._merges()
        # Accounts an import came into or went out of here, or that a merge went into: the balance check
        # worked their balance adjustments out again.
        self.checked = ({i["account_id"] for i in self.i_new.values()} | {i["account_id"] for i in self.i_gone.values()}
                        | set(self.copies))
        self.added_ids = set()
        self._imports()
        self._transactions()
        self._assigned()
        self._categories()
        self._accounts()
        self._recurring_charges()
        self._payees()
        self._settings()
        self._actual()

    def add(self, kind, text, app=False, verb=None):
        self.sections[kind].append({"text": text, "app": bool(app)})
        self.verbs[kind].append(verb)

    def result(self):
        sections = [{"kind": kind, "title": self._title(kind, items), "items": items}
                    for kind, items in self.sections.items() if items]
        return {"changed": bool(sections), "summary": " · ".join(s["title"] for s in sections), "sections": sections}

    # Names. A row refers to rows of its own budget: a start row's (what this side deleted or changed)
    # is named as the start had it, a side row's as this side has it.

    def _name(self, table, row_id, budget, missing):
        row = getattr(budget, table).get(row_id)
        return row["name"] if row is not None else missing

    def category(self, category_id, budget=None):
        if not category_id:
            return "no category"
        return self._name("categories", category_id, budget or self.side, "a category no longer there")

    def account(self, account_id, budget=None):
        return self._name("accounts", account_id, budget or self.side, "an account no longer there")

    def group(self, group_id, budget=None):
        return self._name("groups", group_id, budget or self.side, "a group no longer there")

    def payee(self, payee_id, budget=None):
        return self._name("payees", payee_id, budget or self.side, "a payee no longer there")

    def label(self, t, budget=None):
        """A transaction as a person finds it: "Sample Cafe, Oct 3, −$4.50"."""
        who = self.payee(t["payee_id"], budget) if t["payee_id"] else (t["raw_name"] or "A transaction")
        return f"{who}, {_day(t['date'])}, {money(t['amount'])}"

    def route(self, t, budget=None):
        """A transfer's money and accounts: "$60.00 from Chequing to Savings, Sep 5"."""
        here, there = self.account(t["account_id"], budget), self.account(t["transfer_account_id"], budget)
        way = f"from {here} to {there}" if t["amount"] < 0 else f"from {there} to {here}"
        return f"{money(abs(t['amount']))} {way}, {_day(t['date'])}"

    # ── files ──

    def _imports(self):
        files, total = set(), 0
        for i in self.i_new.values():
            n = sum(1 for t in self.t_new.values() if t["import_id"] == i["id"] and t["hint"] != ADJUSTMENT)
            files.add(i["sha256"])
            total += n
            what = _plural(n, "new transaction") if n else "no new transactions"
            self.add("imported", f"{i['filename']} into {self.account(i['account_id'])}: {what}")
        self.imported = (len(files), total)

        files, total = set(), 0
        for i in self.i_gone.values():
            n = sum(1 for t in self.t_gone.values() if t["import_id"] == i["id"] and t["hint"] != ADJUSTMENT)
            # As Remove import's message says it (removedText in the page): what another file has stays under
            # that file; one you added that the file's row had taken the place of goes back to what you entered.
            under = [s for b, s in self.t_both if b["import_id"] == i["id"]]
            stay = sum(1 for s in under if s["import_id"] is not None)
            yours = len(under) - stay
            files.add(i["sha256"])
            total += n
            text = f"{i['filename']} ({self.account(i['account_id'], self.start)}): {_plural(n, 'transaction')} removed"
            if stay:
                text += f"; {stay} {'stays' if stay == 1 else 'stay'}: another file has {'it' if stay == 1 else 'them'}"
            if yours:
                text += f"; {yours} you added {'stays' if yours == 1 else 'stay'}"
            self.add("import_removed", text)
        self.removed = (len(files), total)

        for sha, filename in self.side.ignored.items():
            if sha not in self.start.ignored:
                self.add("not_imported", filename)

    # ── transactions ──

    def _transactions(self):
        """Every transaction this side deleted, added or changed, oldest first."""
        rows = ([(t["date"], t["id"], t, None) for t in self.t_gone.values()]
                + [(t["date"], t["id"], None, t) for t in self.t_new.values()]
                + [(s["date"], s["id"], b, s) for b, s in self.t_both])
        transfers, back = [], []
        for _, _, b, s in sorted(rows, key=lambda r: (r[0], r[1], r[2] is None)):
            if s is None:
                self._gone(b)
            elif b is None:
                self._new(s, transfers, back)
            else:
                self._changed(b, s, transfers, back)
        self._back(back)
        self._transfers(transfers)

    def _gone(self, t):
        if t["hint"] == ADJUSTMENT:
            app = not t["by_you"] and t["account_id"] in self.checked
            self.add("balance_adjustment", f"{self.adjustment(t, self.start)}: removed"
                     + (" by the app" if app else ""), app=app, verb="removed")
        elif t["import_id"] in self.i_gone or t["account_id"] in self.a_gone or t["hint"] == ADDED_HALF:
            return  # its import was removed, its account merged away, or the transfer it was half of ended
        else:
            self.add("deleted", f"{self.label(t, self.start)} in {self.account(t['account_id'], self.start)}: deleted")

    def _new(self, t, transfers, back):
        if t["hint"] == ADJUSTMENT:
            # The balance check's, after an import, carries the import it was worked out for; Check balance…'s none.
            app = t["import_id"] is not None
            self.add("balance_adjustment", f"{self.adjustment(t)}: added by {'the app' if app else 'Check balance…'}",
                     app=app, verb="added")
            self._decided(_INBOX, t, transfers, back)
        elif t["from_file"] or t["import_id"] is not None:
            self._decided(_INBOX, t, transfers, back)  # its import says it came; this, what was decided about it
        elif t["hint"] != ADDED_HALF:  # a transfer's other half the app added: the transfer says so
            state, as_what = self.decision(t), ""
            if state[0] == "category":
                as_what = f" ({self.category(state[1])})"
            elif state[0] == "split":
                as_what = " (split)"
            elif state[0] == "transfer":
                as_what = f" (a transfer {'to' if t['amount'] < 0 else 'from'} {self.account(state[1])})"
            self.added_ids.add(t["id"])
            self.add("added", f"{self.label(t)} in {self.account(t['account_id'])}: added by hand{as_what}")

    def _changed(self, b, s, transfers, back):
        self._decided(self.decision(b, self.start), s, transfers, back)
        if b["hint"] == ADJUSTMENT and s["hint"] == ADJUSTMENT:
            if (b["amount"], b["date"]) != (s["amount"], s["date"]):
                app = not s["by_you"] and s["account_id"] in self.checked
                self.add("balance_adjustment", f"Balance adjustment in {self.account(s['account_id'])}: "
                         f"{money(b['amount'])} on {_day(b['date'])} → {money(s['amount'])} on {_day(s['date'])}"
                         + (" by the app" if app else ""), app=app, verb="changed")
            return
        parts = []
        # Only one added by hand has a date and an amount of your own: a file's change with the files that have it.
        by_hand = not (b["from_file"] or s["from_file"]) and b["import_id"] is None and s["import_id"] is None
        if by_hand and b["date"] != s["date"]:
            parts.append(f"date {_day(b['date'])} → {_day(s['date'])}")
        if by_hand and b["amount"] != s["amount"]:
            parts.append(f"amount {money(b['amount'])} → {money(s['amount'])}")
        if (b["note"] or "") != (s["note"] or ""):
            parts.append(f"note {_quoted(s['note'])}" if s["note"] else "note removed")
        if (b["is_split"] and s["is_split"] and self.decision(b, self.start) == self.decision(s)
                and self.start.split_notes.get(b["id"]) != self.side.split_notes.get(s["id"])):
            parts.append("notes on its split lines changed")
        if parts:
            self.add("edited", f"{self.label(b, self.start)}: {'; '.join(parts)}")

    def adjustment(self, t, budget=None):
        return f"Balance adjustment in {self.account(t['account_id'], budget)}, {_day(t['date'])}, {money(t['amount'])}"

    def decision(self, t, budget=None):
        """What was decided about a transaction: in the inbox, a category, split (and how), or a transfer."""
        if t["transfer_account_id"] is not None:
            return ("transfer", t["transfer_account_id"])
        if t["is_split"]:
            return ("split", tuple((budget or self.side).splits.get(t["id"], ())))
        if t["status"] == "review":
            return _INBOX
        return ("category", t["category_id"]) if t["category_id"] else ("none",)

    def _decided(self, was, t, transfers, back):
        now = self.decision(t)
        if now == was or now == ("none",):
            return
        app = not t["by_you"]
        by_app = " by the app" if app else ""
        if now[0] == "transfer":
            transfers.append(t)
        elif now == _INBOX:
            back.append((t, was))
        elif now[0] == "split":
            lines = " + ".join(f"{self.category(c)} {money(a)}" for c, a in now[1])
            self.add("split", f"{self.label(t)}: split into {lines}{by_app}", app=app)
        elif was[0] == "category":
            self.add("categorized", f"{self.label(t)}: {self.category(was[1], self.start)} → "
                     f"{self.category(now[1])}{by_app}", app=app)
        else:
            self.add("categorized", f"{self.label(t)}: categorized as {self.category(now[1])}{by_app}", app=app)

    def _back(self, back):
        """Back in the Inbox: sent back by you, or for the app to decide again. A transfer's two halves
        going back together (Not a transfer) are one change, as marking them was."""
        ids = {t["id"] for t, _ in back}
        done = set()
        for t, was in back:
            if t["id"] in done:
                continue
            done.add(t["id"])
            app = not t["by_you"]
            kind, went = ("back_in_inbox", "back in the Inbox to decide again") if app else \
                ("sent_back", "sent back to the Inbox")
            b = self.start.transactions.get(t["id"])
            partner = b["transfer_id"] if b is not None and was[0] == "transfer" else None
            if partner in ids and partner not in done:
                done.add(partner)
                self.add(kind, f"{self.route(b, self.start)}: not a transfer, both {went}", app=app)
            elif was[0] == "transfer":
                self.add(kind, f"{self.label(t)}: not a transfer, {went}", app=app)
            else:
                self.add(kind, f"{self.label(t)}: {went}", app=app)

    def _transfers(self, changed):
        """A transfer's two halves are one transfer; one added by hand says so where it's added."""
        ids = {t["id"] for t in changed}
        done = set()
        for t in changed:
            if t["id"] in done:
                continue
            partner = self.side.transactions.get(t["transfer_id"]) if t["transfer_id"] else None
            if partner is not None and partner["id"] in self.added_ids:
                continue
            if partner is not None and partner["id"] in ids:
                done.add(partner["id"])
                if t["amount"] > 0:
                    t, partner = partner, t
            done.add(t["id"])
            app = not t["by_you"] and not (partner and partner["by_you"])
            how = "paired as a transfer by the app" if app else "marked as a transfer"
            self.add("transfer", f"{self.route(t)}: {how}", app=app)

    # ── the budget ──

    def _assigned(self):
        start, side = self.start, self.side
        order = self._category_order(side)
        # A merge adds the copy's payment category's Assigned to the card it's merged into (ledger.merge_accounts).
        payment = {c["debt_account_id"]: cid for cid, c in start.categories.items() if c["debt_account_id"] is not None}
        merged_in = {}
        for into, copies in self.copies.items():
            into_cat = next((cid for cid, c in side.categories.items() if c["debt_account_id"] == into), None)
            for copy in copies:
                for (month, cid), cents in start.assigned.items():
                    if cid == payment.get(copy["id"]) and into_cat is not None:
                        merged_in[(month, into_cat)] = merged_in.get((month, into_cat), 0) + cents
        same = {s["id"] for _, s in self.c_both}
        changes = []
        for key in set(start.assigned) | set(side.assigned) | set(merged_in):
            month, cid = key
            if cid not in side.categories:
                continue  # the category went (its Assigned with it): it says so
            was = (start.assigned.get(key, 0) if cid in same else 0) + merged_in.get(key, 0)
            now = side.assigned.get(key, 0)
            if was != now:
                changes.append((month, order.get(cid, (1 << 30,)), cid, was, now))
        for month, _, cid, was, now in sorted(changes):
            self.add("assigned", f"{_owner(self.category(cid))} Assigned for {month_name(month)} {money(was)} → {money(now)}")

        starts = {s["id"]: b for b, s in self.c_both}
        for cid, s in sorted(side.categories.items(), key=lambda kv: order.get(kv[0], (1 << 30,))):
            b = starts.get(cid)
            was = b["monthly_budget"] if b else None
            if s["monthly_budget"] != was:
                self.add("budgeted", f"{_owner(s['name'])} Budgeted {_budgeted(was)} → {_budgeted(s['monthly_budget'])}")

    @staticmethod
    def _category_order(budget):
        """category id → its place on the Budget page."""
        groups = budget.groups
        return {cid: (groups.get(c["group_id"], {}).get("sort", 0), c["group_id"], c["sort"], cid)
                for cid, c in budget.categories.items()}

    def _categories(self):
        start, side = self.start, self.side
        for g in self.g_new.values():
            if g["role"] == "normal":
                self.add("categories", f"Added category group {g['name']}", verb="group added")
        for g in self.g_gone.values():
            if g["role"] == "normal":
                self.add("categories", f"Deleted category group {g['name']}", verb="group deleted")
        for b, s in self.g_both:
            if b["name"] != s["name"]:
                self.add("categories", f"Renamed category group {b['name']} → {s['name']}", verb="group renamed")
        same_groups = {s["id"] for _, s in self.g_both}
        movable = lambda budget: [g["id"] for g in sorted(budget.groups.values(), key=lambda g: (g["sort"], g["id"]))
                                  if g["role"] != "income" and g["id"] in same_groups]
        for gid in _moved(movable(start), movable(side)):
            self.add("categories", f"Moved category group {self.group(gid)}", verb="group moved")

        for c in self.c_new.values():
            if c["debt_account_id"] is None and c["id"] not in self._own(side):
                self.add("categories", f"Added category {c['name']} in {self.group(c['group_id'])}", verb="added")
        for c in self.c_gone.values():
            if c["debt_account_id"] is None:
                self.add("categories", f"Deleted category {c['name']}", verb="deleted")
        stayed = set()
        for b, s in self.c_both:
            payment = s["debt_account_id"] is not None  # named after its card, shown while the card is open
            if b["name"] != s["name"] and not payment:
                self.add("categories", f"Renamed category {b['name']} → {s['name']}", verb="renamed")
            if b["hidden"] != s["hidden"] and not payment:
                self.add("categories", f"Hid category {s['name']}" if s["hidden"] else f"Showed category {s['name']} again",
                         verb="hidden" if s["hidden"] else "shown")
            if b["group_id"] == s["group_id"] and s["group_id"] in same_groups:
                stayed.add(s["id"])
            else:
                self.add("categories", f"Moved category {s['name']} from {self.group(b['group_id'], start)} to "
                         f"{self.group(s['group_id'])}", verb="moved")
            if (b["note"] or "") != (s["note"] or ""):
                self.add("categories", f"Changed the notes for {s['name']}", verb="notes")
        for gid in same_groups:
            in_group = lambda budget: [c["id"] for c in sorted(budget.categories.values(), key=lambda c: (c["sort"], c["id"]))
                                       if c["group_id"] == gid and c["id"] in stayed]
            for cid in _moved(in_group(start), in_group(side)):
                self.add("categories", f"Moved category {self.category(cid)} in {self.group(gid)}", verb="moved")

    @staticmethod
    def _own(budget):
        """Income and Interest & Fees, which the app makes when it needs them (ledger.own_category_ids)."""
        value = budget.meta.get("interest_category_id")
        own = {int(value)} if value and value.isdigit() else set()
        income = [(g["sort"], g["id"], c["sort"], c["id"]) for c in budget.categories.values()
                  if (g := budget.groups.get(c["group_id"])) and g["role"] == "income"]
        return own | ({min(income)[3]} if income else set())

    # ── accounts ──

    def _merges(self):
        """Merge into…: the account each account gone here went into ({into: [the start's copies]}),
        found by its transactions or imports, now in that account. One gone without a trace was deleted."""
        gone = self.a_gone
        into = {}
        for b, s in self.t_both:
            if b["account_id"] in gone and s["account_id"] != b["account_id"]:
                into.setdefault(b["account_id"], s["account_id"])
        for i in self.start.imports.values():
            s = self.side.imports.get(i["id"])
            if i["account_id"] in gone and i["id"] not in self.i_gone and s["account_id"] not in (None, i["account_id"]):
                into.setdefault(i["account_id"], s["account_id"])
        copies = {}
        for copy_id, target in into.items():
            copies.setdefault(target, []).append(gone[copy_id])
        return copies

    def _accounts(self):
        start, side = self.start, self.side
        merged_away = {a["id"]: into for into, copies in self.copies.items() for a in copies}
        for a in self.a_new.values():
            self.add("accounts", f"Added account {a['name']} ({KIND_LABELS.get(a['kind'], a['kind'])})", verb="added")
        for a in self.a_gone.values():
            if a["id"] in merged_away:
                self.add("accounts", f"Merged {a['name']} into {self.account(merged_away[a['id']])}", verb="merged")
            else:
                self.add("accounts", f"Deleted account {a['name']}", verb="deleted")
        for b, s in self.a_both:
            merged = self.copies.get(s["id"], [])
            parts = []
            if b["name"] != s["name"]:
                parts.append(f"renamed to {s['name']}")
            # A merge gives the account its copy's bank when it had none, and the newer balance: the merge says so.
            if (b["bank"] or "") != (s["bank"] or "") and not (not b["bank"] and s["bank"] in [a["bank"] for a in merged]):
                parts.append(f"bank {b['bank'] or 'none'} → {s['bank'] or 'none'}")
            if b["apr"] != s["apr"]:
                parts.append(f"interest rate {_rate(b['apr'])} → {_rate(s['apr'])}")
            if b["planned_payment"] != s["planned_payment"]:
                parts.append(f"planned payment {_budgeted(b['planned_payment'])} → {_budgeted(s['planned_payment'])}")
            if parts == [f"renamed to {s['name']}"]:
                self.add("accounts", f"Renamed account {b['name']} → {s['name']}", verb="renamed")
            elif parts:
                self.add("accounts", f"{b['name']}: {'; '.join(parts)}", verb="edited")
            if b["closed"] != s["closed"]:
                self.add("accounts", f"Closed account {s['name']}" if s["closed"] else f"Reopened account {s['name']}",
                         verb="closed" if s["closed"] else "reopened")
            if self._balance_typed(b, s, merged):
                self.add("accounts", f"{_owner(s['name'])} balance set: {_balance(s)} on {_long_day(s['anchor_date'])}",
                         verb="balance")
        for a in self.a_new.values():
            if a["anchor_date"] is not None and a["anchor_source"] == "typed":
                self.add("accounts", f"{_owner(a['name'])} balance set: {_balance(a)} on {_long_day(a['anchor_date'])}",
                         verb="balance")

    def _balance_typed(self, b, s, merged):
        """Whether you typed this account's balance here (Set balance, onboarding's Balances). Not a file's (its
        import says so), a merge's (the newer balance), Check balance…'s on the day of a typed balance (its
        balance adjustment goes into that day's balance: its adjustment says so), nor the typed balance Remove
        import put back."""
        anchor = lambda a: (a["anchor_date"], a["anchor_balance"])
        if anchor(b) == anchor(s) or s["anchor_source"] != "typed" or anchor(s) in map(anchor, merged):
            return False
        if any(i["account_id"] == s["id"] and (i["anchor_before_date"], i["anchor_before_balance"]) == anchor(s)
               for i in self.i_gone.values()):
            return False
        if b["anchor_date"] == s["anchor_date"] and b["anchor_balance"] is not None:
            checked = sum(t["amount"] for t in self.t_new.values() if t["account_id"] == s["id"]
                          and t["hint"] == ADJUSTMENT and t["import_id"] is None and t["date"] == s["anchor_date"])
            if checked and b["anchor_balance"] + checked == s["anchor_balance"]:
                return False
        return True

    # ── recurring charges ──

    def _recurring_charges(self):
        # The charges linked to a recurring charge here (new, or newly linked): a price that follows one of them,
        # as it arrived, is the app's (subscriptions.link_transaction); "New price for…" makes the charge yours.
        linked = {}
        for t in self.t_new.values():
            if t["subscription_id"] is not None:
                linked.setdefault(t["subscription_id"], []).append(t)
        for b, s in self.t_both:
            if s["subscription_id"] is not None and s["subscription_id"] != b["subscription_id"]:
                linked.setdefault(s["subscription_id"], []).append(s)

        for s in self.s_new.values():
            if s["status"] == "ignored":
                self.add("recurring_charges", f"{s['name']} at {money(s['amount'])} isn't recurring", verb="not recurring")
            else:
                kind = " (a bill)" if s["kind"] == "bill" else ""
                self.add("recurring_charges", f"Started tracking {s['name']}{kind}: {money(s['amount'])} "
                         f"{_EVERY.get(s['cadence'], s['cadence'])}", verb="added")
        for s in self.s_gone.values():
            if s["status"] != "ignored":
                self.add("recurring_charges", f"Stopped tracking {s['name']}", verb="stopped")
        for b, s in self.s_both:
            parts, app_price = [], False
            if b["name"] != s["name"]:
                parts.append(f"renamed to {s['name']}")
            if b["amount"] != s["amount"]:
                follows = [t for t in linked.get(s["id"], ()) if -t["amount"] == s["amount"]]
                app_price = bool(follows) and not any(t["by_you"] for t in follows)
                price = f"price {money(b['amount'])} → {money(s['amount'])}"
                parts.append(f"{price} by the app, from its charge on {_day(follows[-1]['date'])}" if app_price else price)
            if b["cadence"] != s["cadence"]:
                parts.append(f"charges {_EVERY.get(b['cadence'], b['cadence'])} → {_EVERY.get(s['cadence'], s['cadence'])}")
            if b["kind"] != s["kind"]:
                parts.append("a bill now" if s["kind"] == "bill" else "a subscription now")
            if b["pricing"] != s["pricing"]:
                parts.append(f"price type {PRICING_LABELS.get(b['pricing'], b['pricing'])} → "
                             f"{PRICING_LABELS.get(s['pricing'], s['pricing'])}")
            if b["ends_on"] != s["ends_on"]:
                parts.append(f"ends {_long_day(s['ends_on'])}" if s["ends_on"] else "no end date")
            if b["category_id"] != s["category_id"]:
                parts.append(f"category {self.category(b['category_id'], self.start)} → {self.category(s['category_id'])}")
            if (b["note"] or "") != (s["note"] or ""):
                parts.append(f"note {_quoted(s['note'])}" if s["note"] else "note removed")
            if b["status"] != s["status"]:
                parts.append({"cancelled": "marked cancelled", "active": "marked active again",
                              "ignored": "isn't recurring"}.get(s["status"], s["status"]))
            if parts == [f"renamed to {s['name']}"]:
                self.add("recurring_charges", f"Renamed recurring charge {b['name']} → {s['name']}", verb="edited")
            elif parts:
                self.add("recurring_charges", f"{b['name']}: {'; '.join(parts)}", app=app_price and len(parts) == 1,
                         verb="edited")

    # ── payees ──

    def _payees(self):
        start = self.start
        gone = self.p_gone
        moves = {}
        for b, s in self.t_both:
            was, now = b["payee_id"], s["payee_id"]
            if was is not None and now is not None and (was != now or was in gone):
                moves.setdefault((was, now), []).append(s["id"])
        places = {}  # where each gone payee's transactions went
        for was, now in moves:
            places.setdefault(was, set()).add(now)
        for (was, now), ids in sorted(moves.items(), key=lambda kv: (self.payee(kv[0][0], start).lower(), kv[0])):
            if was in gone and len(places[was]) == 1:
                self.add("payees", f"{self.payee(was, start)} merged into {self.payee(now)}", verb="merged")
            else:
                self.add("payees", f"{_plural(len(ids), 'transaction')} moved from {self.payee(was, start)} to "
                         f"{self.payee(now)}", verb="moved")
        for b, s in self.p_both:
            if b["name"] != s["name"]:
                self.add("payees", f"Renamed payee {b['name']} → {s['name']}", verb="renamed")
            if b["always_ask"] != s["always_ask"]:
                self.add("payees", f"Always ask {'on' if s['always_ask'] else 'off'} for {s['name']}", verb="always ask")

    # ── settings ──

    def _settings(self):
        start, side = self.start, self.side
        was, now = start.settings(), side.settings()
        if was["overspending"] != now["overspending"]:
            self.add("settings", f"Overspending {OVERSPENDING_LABELS[was['overspending']]} → "
                     f"{OVERSPENDING_LABELS[now['overspending']]}")
        if was["average_months"] != now["average_months"]:
            self.add("settings", f"Usual's months {was['average_months']} → {now['average_months']}")
        b, s = start.meta.get("budget_start"), side.meta.get("budget_start")
        if b != s and s:
            self.add("settings", f"Budget start {month_name(b)} → {month_name(s)}" if b else
                     f"The budget starts in {month_name(s)}")
        b, s = start.meta.get("currency"), side.meta.get("currency")
        if b != s and s:
            self.add("settings", f"Currency {b} → {s}" if b else f"The budget's currency is {s}")
        known = BOOKKEEPING_META | set(_SETTINGS)
        if any(start.meta.get(k) != side.meta.get(k) for k in set(start.meta) | set(side.meta) if k not in known):
            self.add("settings", "Something else in the budget changed")

    def _actual(self):
        n = len(self.side.examples - self.start.examples)
        if n:
            self.add("actual", f"Learnt categories from an Actual export ({_plural(n, 'example')})")

    # ── titles ──

    def _title(self, kind, items):
        n, verbs = len(items), self.verbs[kind]
        one = items[0]["text"] if n == 1 else None
        if kind == "imported":
            files, total = self.imported
            return f"{_plural(files, 'file')} imported ({_plural(total, 'transaction') if total else 'no new transactions'})"
        if kind == "import_removed":
            files, total = self.removed
            return f"{_plural(files, 'import')} removed ({_plural(total, 'transaction')})"
        if kind == "transfer":
            return f"{n} marked as {'a transfer' if n == 1 else 'transfers'}"
        simple = {"not_imported": f"{_plural(n, 'file')} you said not to import", "categorized": f"{n} categorized",
                  "split": f"{n} split", "sent_back": f"{n} sent back", "back_in_inbox": f"{n} back in the Inbox",
                  "edited": f"{n} edited", "added": f"{n} added by hand", "deleted": f"{n} deleted",
                  "actual": one}
        if kind in simple:
            return simple[kind]
        if kind in ("assigned", "budgeted", "settings"):
            return one or {"assigned": f"{n} Assigned amounts changed", "budgeted": f"{n} Budgeted amounts changed",
                           "settings": f"{n} settings changed"}[kind]
        if len(set(verbs)) == 1:
            for_one, for_many = _TITLES[(kind, verbs[0])]
            return (for_one or one) if n == 1 else for_many.format(n=n)
        return f"{n} changes to {_THINGS[kind]}"


_INBOX = ("inbox",)


def _budgeted(cents):
    return "none" if cents is None else money(cents)


def _rate(apr):
    return "none" if apr is None else f"{apr:g}%"


def _balance(account):
    """A balance in words: a card's or line of credit's is what you owe (kept negative) or what's in your favour."""
    cents = account["anchor_balance"]
    if account["kind"] in DEBT_KINDS:
        return f"{money(-cents)} owing" if cents <= 0 else f"{money(cents)} in your favour"
    return money(cents)
