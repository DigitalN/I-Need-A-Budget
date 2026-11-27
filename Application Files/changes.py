# SPDX-License-Identifier: GPL-3.0-only
"""What changed: a budget changed on two computers, each side compared with the save both
started from (docs/adr/0008, issue #184).

When two computers have each changed the budget since the save they both started from, the
app shows what each one changed and asks which to keep. This module works out what to show.
It only reads: no window, no network, no files written, the same on any system.

    compare(start, this, other) → {"this": <side>, "other": <side>}
    what_changed(start, side)   → <side>

Each budget is a path to a complete budget file (a save, the copy of the save a computer
started from, a backup: read through a read-only connection), an open `sqlite3.Connection`,
or a `db.Database` (read under its lock): this computer's working copy is passed as its
Database, so what's still in its `-wal` is counted. A side is:

    {"changed": bool,          # anything listed at all
     "summary": str,           # one line: "1 file imported (14 transactions) · 9 categorized · …"
     "sections": [{"kind": str, "title": str, "items": [{"text": str, "app": bool}, …]}, …]}

`summary` is the sections' titles joined by " · ", in the order below; "Show every change"
lists every section's items under its title. An item's `app` is true for what the app did on
its own (the re-check, an import from Downloads, the balance check), which is listed too and
says so in its text. The kinds, in order: imported, import_removed, not_imported,
categorized, split, transfer, sent_back, back_in_inbox, edited, added, deleted,
balance_adjustment, assigned, budgeted, categories, accounts, recurring_charges, payees,
settings, actual.

Rows are matched by id. An id one side added is new on that side whatever the other side did
with the same id: each side is only ever compared with the start. A transaction, account,
import or recurring charge under the same id but made at another time (SQLite gives the id of
a deleted newest row to the next one) is the old one deleted and a new one added.

Some differences are never changes: this session's undo, backups' own meta, what the budget
folder keeps about saves (`BOOKKEEPING_META`: the budget folder's piece adds its keys there),
the inbox's reasons (only text to show), a group folded on the Budget page, and what the app
keeps for itself (how bank wording finds a payee, CSV layouts, which file each transaction was
in, what a file's row replaced, remembered answers about files). Anything else in the budget's
meta that isn't one of its settings is listed as "Something else in the budget changed", so a
key nobody described is seen rather than lost.

Amounts are written as the page writes them ("−$4.50", "$1,234.00"), months as "December 2026"
and days as "Oct 3", in docs/CONTEXT.md's words.
"""

import sqlite3
from pathlib import Path

import backups
from ledger import DEBT_KINDS, KIND_LABELS
from payees import ADDED_HALF, ADJUSTMENT
from settings import BUDGET_RAN_ON, validate_budget
from subscriptions import PRICINGS
from util import month_name, short_day

# Meta keys that are the app's own bookkeeping, never a change: backups' (what the last backup
# was), the schema's version, flags for work done once at launch, and what the budget folder
# keeps about saves (which computer saved it, when, the save's id, the versions: issue #183 adds
# its keys here).
BOOKKEEPING_META = {*backups.BACKUP_META, "schema_version", "onboarded", "read_imported_files", "currency_read",
                    "last_onboarding_account", "interest_category_id"}

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
# "{n} changes to <its things>".
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

class _Budget:
    """Everything this module compares, read once from one budget."""

    def __init__(self, conn):
        def rows(sql):
            try:
                cur = conn.execute(sql)
            except sqlite3.OperationalError as e:  # a table an older budget doesn't have yet
                if "no such table" in str(e):
                    return []
                raise
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur]

        def by_id(table):
            return {r["id"]: r for r in rows(f"SELECT * FROM {table} ORDER BY id")}

        self.meta = {r["key"]: r["value"] for r in rows("SELECT key, value FROM meta")}
        self.accounts = by_id("accounts")
        self.groups = by_id("category_groups")
        self.categories = by_id("categories")
        self.payees = by_id("payees")
        self.imports = by_id("imports")
        self.subscriptions = by_id("subscriptions")
        self.transactions = by_id("transactions")
        self.splits = {}
        for r in rows("SELECT transaction_id, category_id, amount, note FROM splits ORDER BY id"):
            self.splits.setdefault(r["transaction_id"], []).append((r["category_id"], r["amount"], r["note"]))
        self.assigned = {(r["month"], r["category_id"]): r["assigned"]
                         for r in rows("SELECT month, category_id, assigned FROM budget")}
        self.examples = {r["id"] for r in rows("SELECT id FROM examples")}
        self.ignored = {r["sha256"]: r["filename"] for r in rows("SELECT sha256, filename FROM ignored_files")}

    def settings(self):
        """Overspending and Usual's months as the budget runs on them (settings.budget_settings)."""
        months = self.meta.get("average_months")
        return validate_budget({"overspending": self.meta.get("overspending"),
                                "average_months": int(months) if months and months.isdigit() else None}, BUDGET_RAN_ON)


def _read(source):
    if isinstance(source, _Budget):
        return source
    if isinstance(source, sqlite3.Connection):
        return _Budget(source)
    if hasattr(source, "read") and hasattr(source, "conn"):  # a db.Database: under its lock
        with source.read() as conn:
            return _Budget(conn)
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"No budget at {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return _Budget(conn)
    finally:
        conn.close()


def _split_rows(start, side, same=("created_at",)):
    """(gone, new, both) between two tables of rows by id: rows only the start has, rows only the
    side has, and (start's, side's) pairs. A row under the same id that differs in a column of
    `same` (when it was made, what never changes about it) is two rows: SQLite gives a deleted newest
    row's id to the next one."""
    def one(b, s):
        return all(b.get(column) == s.get(column) for column in same)

    gone, new, both = {}, {}, []
    for row_id, b in start.items():
        s = side.get(row_id)
        if s is not None and one(b, s):
            both.append((b, s))
        else:
            gone[row_id] = b
    for row_id, s in side.items():
        b = start.get(row_id)
        if b is None or not one(b, s):
            new[row_id] = s
    return gone, new, both


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
        self.a_gone, self.a_new, self.a_both = _split_rows(start.accounts, side.accounts, ("created_at", "kind"))
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

    # Names: from this side, else from the start (what this side deleted). `gone`: for something this side
    # deleted, from the start first (its id may be another row's here now).

    def _name(self, table, row_id, missing, gone=False):
        for budget in ((self.start, self.side) if gone else (self.side, self.start)):
            row = getattr(budget, table).get(row_id)
            if row is not None:
                return row["name"]
        return missing

    def category(self, category_id):
        return self._name("categories", category_id, "no category") if category_id else "no category"

    def account(self, account_id, gone=False):
        return self._name("accounts", account_id, "an account no longer there", gone)

    def group(self, group_id):
        return self._name("groups", group_id, "a group no longer there")

    def payee(self, payee_id, gone=False):
        return self._name("payees", payee_id, "a payee no longer there", gone)

    def label(self, t, gone=False):
        """A transaction as a person finds it: "Sample Cafe, Oct 3, −$4.50"."""
        who = self.payee(t["payee_id"], gone) if t["payee_id"] else (t.get("raw_name") or "A transaction")
        return f"{who}, {_day(t['date'])}, {money(t['amount'])}"

    # ── files ──

    def _imports(self):
        start, side = self.start, self.side
        self.gone_imports, self.new_imports, _ = _split_rows(start.imports, side.imports, ("imported_at", "sha256"))
        t_gone, t_new, t_both = _split_rows(start.transactions, side.transactions)
        self.t_gone, self.t_new, self.t_both = t_gone, t_new, t_both

        files, total = set(), 0
        for i in self.new_imports.values():
            n = sum(1 for t in t_new.values() if t["import_id"] == i["id"] and t["hint"] != ADJUSTMENT)
            files.add(i["sha256"])
            total += n
            what = _plural(n, "new transaction") if n else "no new transactions"
            self.add("imported", f"{i['filename']} into {self.account(i['account_id'])}: {what}")
        self.imported = (len(files), total)

        files, total = set(), 0
        for i in self.gone_imports.values():
            n = sum(1 for t in t_gone.values() if t["import_id"] == i["id"] and t["hint"] != ADJUSTMENT)
            stay = sum(1 for b, s in t_both if b["import_id"] == i["id"])
            files.add(i["sha256"])
            total += n
            text = f"{i['filename']} ({self.account(i['account_id'], gone=True)}): {_plural(n, 'transaction')} removed"
            if stay:
                text += f"; {stay} {'stays' if stay == 1 else 'stay'}: another file has {'it' if stay == 1 else 'them'}"
            self.add("import_removed", text)
        self.removed = (len(files), total)

        for sha, filename in side.ignored.items():
            if sha not in start.ignored:
                self.add("not_imported", filename)

    # ── transactions ──

    def _transactions(self):
        """Every transaction this side deleted, added or changed, oldest first."""
        rows = ([(t["date"], t["id"], t, None) for t in self.t_gone.values()]
                + [(t["date"], t["id"], None, t) for t in self.t_new.values()]
                + [(s["date"], s["id"], b, s) for b, s in self.t_both])
        transfers, self.payee_moves = [], {}
        for _, _, b, s in sorted(rows, key=lambda r: (r[0], r[1], r[2] is None)):
            if s is None:
                self._gone(b)
            elif b is None:
                self._new(s, transfers)
            else:
                self._changed(b, s, transfers)
        self._transfers(transfers)

    def _gone(self, t):
        if t["hint"] == ADJUSTMENT:
            self.add("balance_adjustment", f"{self.adjustment(t, gone=True)}: removed", verb="removed")
        elif t["import_id"] in self.gone_imports or t["account_id"] in self.a_gone or t["hint"] == ADDED_HALF:
            return  # its import was removed, its account merged away, or the transfer it was half of ended
        else:
            self.add("deleted", f"{self.label(t, gone=True)} in {self.account(t['account_id'], gone=True)}: deleted")

    def _new(self, t, transfers):
        if t["hint"] == ADJUSTMENT:
            by = "Check balance…" if t["import_id"] is None else "the app"
            self.add("balance_adjustment", f"{self.adjustment(t)}: added by {by}", app=t["import_id"] is not None,
                     verb="added")
            self._decided(_INBOX, t, transfers)
        elif t["fingerprint"] is not None or t["fitid"] is not None or t["import_id"] is not None:
            self._decided(_INBOX, t, transfers)  # its import says it came; this, what was decided about it
        elif t["hint"] != ADDED_HALF:  # a transfer's other half the app added: the transfer says so
            state, as_what = self.decision(t), ""
            if state[0] == "category":
                as_what = f" ({self.category(state[1])})"
            elif state[0] == "split":
                as_what = " (split)"
            elif state[0] == "transfer":
                as_what = f" (a transfer {'to' if t['amount'] < 0 else 'from'} {self.account(state[1])})"
            self.add("added", f"{self.label(t)} in {self.account(t['account_id'])}: added by hand{as_what}")

    def _changed(self, b, s, transfers):
        self._decided(self.decision(b, self.start), s, transfers)
        if b["payee_id"] and s["payee_id"] and b["payee_id"] != s["payee_id"]:
            self.payee_moves.setdefault((b["payee_id"], s["payee_id"]), []).append(s["id"])  # the payees say so
        if b["hint"] == ADJUSTMENT and s["hint"] == ADJUSTMENT:
            if (b["amount"], b["date"]) != (s["amount"], s["date"]):
                self.add("balance_adjustment", f"Balance adjustment in {self.account(s['account_id'])}: "
                         f"{money(b['amount'])} on {_day(b['date'])} → {money(s['amount'])} on {_day(s['date'])}",
                         app=not s["by_you"], verb="changed")
            return
        parts = []
        # Only one added by hand has a date and an amount of your own: a file's changes with the files that have it.
        by_hand = all(x["fingerprint"] is None and x["fitid"] is None and x["import_id"] is None for x in (b, s))
        if by_hand and b["date"] != s["date"]:
            parts.append(f"date {_day(b['date'])} → {_day(s['date'])}")
        if by_hand and b["amount"] != s["amount"]:
            parts.append(f"amount {money(b['amount'])} → {money(s['amount'])}")
        if (b["note"] or "") != (s["note"] or ""):
            parts.append(f"note {_quoted(s['note'])}" if s["note"] else "note removed")
        if parts:
            self.add("edited", f"{self.label(b)}: {'; '.join(parts)}")

    def adjustment(self, t, gone=False):
        return f"Balance adjustment in {self.account(t['account_id'], gone)}, {_day(t['date'])}, {money(t['amount'])}"

    def decision(self, t, budget=None):
        """What was decided about a transaction: in the inbox, a category, split (and how), or a transfer."""
        if t["transfer_account_id"] is not None:
            return ("transfer", t["transfer_account_id"])
        if t["is_split"]:
            return ("split", tuple((budget or self.side).splits.get(t["id"], ())))
        if t["status"] == "review":
            return _INBOX
        return ("category", t["category_id"]) if t["category_id"] else ("none",)

    def _decided(self, was, t, transfers):
        now = self.decision(t)
        if now == was or now == ("none",):
            return
        app = not t["by_you"]
        if now[0] == "transfer":
            transfers.append(t)
        elif now == _INBOX:
            if app:
                self.add("back_in_inbox", f"{self.label(t)}: back in the Inbox to decide again", app=True)
            else:
                self.add("sent_back", f"{self.label(t)}: sent back to the Inbox")
        elif now[0] == "split":
            lines = " + ".join(f"{self.category(c)} {money(a)}" for c, a, _ in now[1])
            self.add("split", f"{self.label(t)}: split into {lines}" + (" by the app" if app else ""), app=app)
        elif was[0] == "category":
            self.add("categorized", f"{self.label(t)}: {self.category(was[1])} → {self.category(now[1])}"
                     + (" by the app" if app else ""), app=app)
        else:
            self.add("categorized", f"{self.label(t)}: categorized as {self.category(now[1])}"
                     + (" by the app" if app else ""), app=app)

    def _transfers(self, changed):
        """A transfer's two halves are one transfer."""
        ids = {t["id"] for t in changed}
        done = set()
        for t in changed:
            if t["id"] in done:
                continue
            partner = self.side.transactions.get(t["transfer_id"]) if t["transfer_id"] else None
            if partner is not None and partner["id"] in ids:
                done.add(partner["id"])
                if t["amount"] > 0:
                    t, partner = partner, t
            done.add(t["id"])
            here, there = self.account(t["account_id"]), self.account(t["transfer_account_id"])
            route = f"from {here} to {there}" if t["amount"] < 0 else f"from {there} to {here}"
            app = not t["by_you"] and not (partner and partner["by_you"])
            how = "paired as a transfer by the app" if app else "marked as a transfer"
            self.add("transfer", f"{money(abs(t['amount']))} {route}, {_day(t['date'])}: {how}", app=app)

    # ── the budget ──

    def _assigned(self):
        start, side = self.start, self.side
        merged_into = {self._merged_into(a) for a in self.a_gone}
        order = self._category_order(side)
        changes = []
        for key in set(start.assigned) | set(side.assigned):
            month, cid = key
            if cid not in side.categories:
                continue  # the category went (its Assigned with it): it says so
            if side.categories[cid]["debt_account_id"] in merged_into - {None}:
                continue  # a merged card's payment category took the copy's: the merge says so
            was, now = start.assigned.get(key, 0), side.assigned.get(key, 0)
            if was != now:
                changes.append((month, order.get(cid, (1 << 30,)), cid, was, now))
        for month, _, cid, was, now in sorted(changes):
            self.add("assigned", f"{_owner(self.category(cid))} Assigned for {month_name(month)} {money(was)} → {money(now)}")

        for cid, s in sorted(side.categories.items(), key=lambda kv: order.get(kv[0], (1 << 30,))):
            b = start.categories.get(cid)
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
        own = self._own(start) | self._own(side)
        g_gone, g_new, g_both = _split_rows(start.groups, side.groups, ("role",))
        for g in g_new.values():
            if g["role"] == "normal":
                self.add("categories", f"Added category group {g['name']}", verb="group added")
        for g in g_gone.values():
            if g["role"] == "normal":
                self.add("categories", f"Deleted category group {g['name']}", verb="group deleted")
        for b, s in g_both:
            if b["name"] != s["name"]:
                self.add("categories", f"Renamed category group {b['name']} → {s['name']}", verb="group renamed")
        movable = lambda budget: [g["id"] for g in sorted(budget.groups.values(), key=lambda g: (g["sort"], g["id"]))
                                  if g["role"] != "income"]
        for gid in _moved(movable(start), movable(side)):
            self.add("categories", f"Moved category group {self.group(gid)}", verb="group moved")

        c_gone, c_new, c_both = _split_rows(start.categories, side.categories, ("debt_account_id",))
        for c in c_new.values():
            if c["debt_account_id"] is None and c["id"] not in own:
                self.add("categories", f"Added category {c['name']} in {self.group(c['group_id'])}", verb="added")
        for c in c_gone.values():
            if c["debt_account_id"] is None:
                self.add("categories", f"Deleted category {c['name']}", verb="deleted")
        stayed = set()
        for b, s in c_both:
            payment = s["debt_account_id"] is not None  # named after its card, shown while the card is open
            if b["name"] != s["name"] and not payment:
                self.add("categories", f"Renamed category {b['name']} → {s['name']}", verb="renamed")
            if b["hidden"] != s["hidden"] and not payment:
                self.add("categories", f"Hid category {s['name']}" if s["hidden"] else f"Showed category {s['name']} again",
                         verb="hidden" if s["hidden"] else "shown")
            if b["group_id"] != s["group_id"]:
                self.add("categories", f"Moved category {s['name']} from {self.group(b['group_id'])} to "
                         f"{self.group(s['group_id'])}", verb="moved")
            else:
                stayed.add(s["id"])
            if (b["note"] or "") != (s["note"] or ""):
                self.add("categories", f"Changed the notes for {s['name']}", verb="notes")
        for gid in side.groups:
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

    def _merged_into(self, account_id):
        """The account a merge put `account_id`'s transactions in, or None (it was deleted)."""
        for t in self.start.transactions.values():
            if t["account_id"] == account_id:
                s = self.side.transactions.get(t["id"])
                if s is not None and s["account_id"] not in self.a_gone and s.get("created_at") == t.get("created_at"):
                    return s["account_id"]
        for i in self.start.imports.values():
            if i["account_id"] == account_id:
                s = self.side.imports.get(i["id"])
                if s is not None and s["account_id"] is not None and s["account_id"] not in self.a_gone:
                    return s["account_id"]
        return None

    def _accounts(self):
        start, side = self.start, self.side
        gone, new, both = self.a_gone, self.a_new, self.a_both
        copies = {}  # account id → the accounts merged into it here
        for a in new.values():
            self.add("accounts", f"Added account {a['name']} ({KIND_LABELS.get(a['kind'], a['kind'])})", verb="added")
        for a in gone.values():
            into = self._merged_into(a["id"])
            if into is None:
                self.add("accounts", f"Deleted account {a['name']}", verb="deleted")
            else:
                copies.setdefault(into, []).append(a)
                self.add("accounts", f"Merged {a['name']} into {self.account(into)}", verb="merged")
        anchor = lambda a: (a["anchor_date"], a["anchor_balance"])
        for b, s in both:
            merged = copies.get(s["id"], [])
            if b["name"] != s["name"]:
                self.add("accounts", f"Renamed account {b['name']} → {s['name']}", verb="renamed")
            parts = []
            # A merge gives the account its copy's bank when it had none, and the newer balance: the merge says so.
            if (b["bank"] or "") != (s["bank"] or "") and not (not b["bank"] and s["bank"] in [a["bank"] for a in merged]):
                parts.append(f"bank {b['bank'] or 'none'} → {s['bank'] or 'none'}")
            if b["apr"] != s["apr"]:
                parts.append(f"interest rate {_rate(b['apr'])} → {_rate(s['apr'])}")
            if b["planned_payment"] != s["planned_payment"]:
                parts.append(f"planned payment {_budgeted(b['planned_payment'])} → {_budgeted(s['planned_payment'])}")
            if parts:
                self.add("accounts", f"{s['name']}: {'; '.join(parts)}", verb="edited")
            if b["closed"] != s["closed"]:
                self.add("accounts", f"Closed account {s['name']}" if s["closed"] else f"Reopened account {s['name']}",
                         verb="closed" if s["closed"] else "reopened")
            # A balance you typed (Set balance, or one typed when the account was added): a file's comes with its
            # import.
            if anchor(b) != anchor(s) and s["anchor_source"] == "typed" and anchor(s) not in map(anchor, merged):
                self.add("accounts", f"{_owner(s['name'])} balance set: {_balance(s)} on {_long_day(s['anchor_date'])}",
                         verb="balance")
        for a in new.values():
            if a["anchor_date"] is not None and a["anchor_source"] == "typed":
                self.add("accounts", f"{_owner(a['name'])} balance set: {_balance(a)} on {_long_day(a['anchor_date'])}",
                         verb="balance")

    # ── recurring charges ──

    def _recurring_charges(self):
        start, side = self.start, self.side
        gone, new, both = _split_rows(start.subscriptions, side.subscriptions)
        for s in new.values():
            if s["status"] == "ignored":
                self.add("recurring_charges", f"{s['name']} at {money(s['amount'])} isn't recurring", verb="not recurring")
            else:
                kind = " (a bill)" if s["kind"] == "bill" else ""
                self.add("recurring_charges", f"Started tracking {s['name']}{kind}: {money(s['amount'])} "
                         f"{_EVERY.get(s['cadence'], s['cadence'])}", verb="added")
        for s in gone.values():
            if s["status"] != "ignored":
                self.add("recurring_charges", f"Stopped tracking {s['name']}", verb="stopped")
        for b, s in both:
            if b["name"] != s["name"]:
                self.add("recurring_charges", f"Renamed recurring charge {b['name']} → {s['name']}", verb="edited")
            parts = []
            if b["amount"] != s["amount"]:
                parts.append(f"price {money(b['amount'])} → {money(s['amount'])}")
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
                parts.append(f"category {self.category(b['category_id'])} → {self.category(s['category_id'])}")
            if (b["note"] or "") != (s["note"] or ""):
                parts.append(f"note {_quoted(s['note'])}" if s["note"] else "note removed")
            if b["status"] != s["status"]:
                parts.append({"cancelled": "marked cancelled", "active": "marked active again",
                              "ignored": "isn't recurring"}.get(s["status"], s["status"]))
            if parts:
                self.add("recurring_charges", f"{s['name']}: {'; '.join(parts)}", verb="edited")

    # ── payees ──

    def _payees(self):
        start, side = self.start, self.side
        for (was, now), ids in sorted(self.payee_moves.items(), key=lambda kv: (self.payee(kv[0][0]).lower(), kv[0])):
            if was not in side.payees:
                self.add("payees", f"{self.payee(was)} merged into {self.payee(now)}", verb="merged")
            else:
                self.add("payees", f"{_plural(len(ids), 'transaction')} moved from {self.payee(was)} to "
                         f"{self.payee(now)}", verb="moved")
        for pid, b in start.payees.items():
            s = side.payees.get(pid)
            if s is None:
                continue
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
