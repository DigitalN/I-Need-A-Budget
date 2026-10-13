"""Everything the page can ask for, in one place.

The page calls `window.pywebview.api.call(name, args)`; main.Api passes that to
`BudgetService.dispatch()`, which only runs methods marked `@exposed`. The dev
preview (scripts/preview.py) and the tests drive the same class, so whatever works
there works in the app.

Results are plain JSON-able values. Problems the user can fix raise UserError; the
page shows its message.
"""

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import budget as budget_math
import importer
import ledger
import subscriptions
import transfers
from learn import analyze
from payees import describe
from util import add_months, first_day, last_day, month_of, months_between, today

log = logging.getLogger("inab")

EXPOSED = {}


def exposed(fn):
    EXPOSED[fn.__name__] = fn
    return fn


class UserError(Exception):
    """Something the person can fix; the message is shown on the page."""


STARTER_CATEGORIES = [
    ("Bills", ["Rent / Mortgage", "Utilities", "Phone & Internet", "Insurance", "Subscriptions"]),
    ("Everyday", ["Groceries", "Eating Out", "Transportation", "Household", "Fun Money"]),
    ("Saving", ["Emergency Fund"]),
]


def _rows(cursor):
    return [dict(r) for r in cursor]


class BudgetService:
    def __init__(self, db, settings, data_dir, hooks=None):
        self.db = db
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.hooks = hooks or {}          # window features: choose_files, reveal, updates, app_info
        self._seen_files = {}             # Downloads watcher: path → (mtime, size) already looked at

    # ── plumbing ──

    def dispatch(self, name, args=None):
        fn = EXPOSED.get(name)
        if fn is None:
            return {"ok": False, "error": f"Unknown request: {name}"}
        try:
            if isinstance(args, dict):
                result = fn(self, **args)
            else:
                result = fn(self, *(args or []))
            return {"ok": True, "result": result}
        except (UserError, importer.ImportError_, ValueError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("%s failed", name)
            return {"ok": False, "error": f"Something went wrong ({type(e).__name__}). Details are in the log."}

    def today(self):
        return os.environ.get("INAB_TODAY") or today()

    def current_month(self):
        return month_of(self.today())

    def budget_start(self):
        return self.db.get_meta("budget_start")

    # ── app state ──

    @exposed
    def bootstrap(self):
        with self.db.read() as c:
            onboarded = self.db.get_meta("onboarded") == "1"
            has_categories = c.execute("SELECT COUNT(*) FROM categories WHERE debt_account_id IS NULL").fetchone()[0]
        return {
            "onboarded": onboarded, "has_categories": bool(has_categories),
            "today": self.today(), "current_month": self.current_month(), "budget_start": self.budget_start(),
            "settings": self.settings.get(), "app": self._app_info(), "counts": self.counts(),
            "undo": self.db.undo_label(),
        }

    def _app_info(self):
        info = self.hooks.get("app_info")
        return info() if info else {"version": "dev", "data_dir": str(self.data_dir)}

    @exposed
    def counts(self):
        with self.db.read() as c:
            review = c.execute("SELECT COUNT(*) FROM transactions WHERE status = 'review'").fetchone()[0]
            no_balance = c.execute("SELECT COUNT(*) FROM accounts WHERE anchor_date IS NULL AND closed = 0").fetchone()[0]
        return {"inbox": review, "accounts_without_balance": no_balance, "undo": self.db.undo_label()}

    @exposed
    def undo(self):
        label = self.db.undo()
        if label is None:
            raise UserError("Nothing to undo.")
        return {"undone": label, "undo": self.db.undo_label()}

    # ── files ──

    def _archive(self, path):
        """Moves an imported file out of Downloads into the app's Imported Files folder."""
        path = Path(path)
        folder = self.data_dir / "Imported Files" / datetime.now().strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{datetime.now().strftime('%Y-%m-%d %H%M%S')} {path.name}"
        try:
            shutil.move(str(path), str(target))
            return str(target)
        except OSError as e:
            log.warning("Couldn't move %s to %s: %s", path, target, e)
            return None

    def _describe_file(self, path):
        path = Path(path)
        item = {"path": str(path), "filename": path.name}
        try:
            parsed = importer.parse(path)
        except (importer.ImportError_, OSError) as e:
            return {**item, "format": None, "error": str(e)}
        item.update(format=parsed.format)
        if parsed.format == "actual":
            item.update(rows=len(parsed.actual_rows), account_name="Actual Budget export",
                        first_date=min((r["date"] for r in parsed.actual_rows), default=None),
                        last_date=max((r["date"] for r in parsed.actual_rows), default=None))
            return item
        with self.db.read() as c:
            account_id = ledger.account_by_key(c, parsed.account.keys)
            name = c.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()["name"] \
                if account_id else parsed.account.name
            seen = c.execute("SELECT 1 FROM imports WHERE sha256 = ?", (importer.sha256_of(path),)).fetchone()
        label = f"{name} ••{parsed.account.last4}" if parsed.account.last4 and parsed.account.kind == "credit_card" else name
        item.update(rows=len(parsed.rows), account_name=label, kind=parsed.account.kind,
                    first_date=parsed.first_date, last_date=parsed.last_date, already_imported=bool(seen))
        return item

    def _candidates(self, folder):
        folder = Path(folder)
        if not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() in importer.SUPPORTED_SUFFIXES and path.is_file() and not path.name.startswith("."):
                out.append(path)
        return out

    @exposed
    def scan_downloads(self):
        """Bank files and Actual Budget exports sitting in Downloads (for first-run setup)."""
        found = []
        for path in self._candidates(self.settings.downloads_dir()):
            item = self._describe_file(path)
            if item.get("format"):
                found.append(item)
        found.sort(key=lambda i: (i["format"] == "actual", i.get("account_name") or "", i.get("first_date") or ""))
        return {"folder": str(self.settings.downloads_dir()), "files": found}

    @exposed
    def import_files(self, paths, archive=True):
        """Imports bank files and learns from Actual exports. Files from Downloads are
        moved into the app's Imported Files folder afterwards."""
        downloads = self.settings.downloads_dir().resolve()
        results, learnt, errors = [], None, []
        parsed_files = []
        for p in paths:
            path = Path(p)
            try:
                parsed_files.append((path, importer.parse(path)))
            except (importer.ImportError_, OSError) as e:
                errors.append({"filename": path.name, "error": str(e)})
        # Learn from Actual first, then bank files oldest first, so refunds and transfers
        # find what they belong to.
        parsed_files.sort(key=lambda pf: (pf[1].format != "actual", pf[1].first_date or ""))
        for path, parsed in parsed_files:
            in_downloads = path.resolve().parent == downloads
            if parsed.format == "actual":
                with self.db.write() as c:
                    outcome = importer.learn_from_actual(c, parsed)
                learnt = {"filename": path.name, **outcome}
                continue
            sha = importer.sha256_of(path)
            with self.db.write() as c:
                result = importer.import_parsed(c, parsed, path.name, sha)
                if archive and in_downloads:
                    archived = self._archive(path)
                    c.execute("UPDATE imports SET archived_path = ? WHERE id = ?", (archived, result.import_id))
            results.append(result.as_dict())
            log.info("Imported %s: %d new, %d already there, %d filed, %d to review", path.name, result.added,
                     result.duplicates, result.filed, result.to_review)
        refiled = self.refile() if learnt or results else 0
        return {"results": results, "learnt": learnt, "errors": errors, "refiled": refiled, "counts": self.counts()}

    @exposed
    def refile(self, payee_ids=None):
        """Looks at what's waiting for review again (all of it, or just these payees'),
        filing what the app is now sure about after learning something new."""
        with self.db.write() as c:
            return self._refile(c, payee_ids)

    def _refile(self, c, payee_ids=None):
        where = ""
        params = []
        if payee_ids is not None:
            payee_ids = [p for p in payee_ids if p]
            if not payee_ids:
                return 0
            where = f"AND payee_id IN ({','.join('?' * len(payee_ids))})"
            params = payee_ids
        ids = [r["id"] for r in c.execute(
            f"SELECT id FROM transactions WHERE status = 'review' AND similar_payee_id IS NULL "
            f"AND reason NOT LIKE '%charged you again%' {where} ORDER BY date, id", params)]
        filed, _ = importer.file_transactions(c, ids)
        return filed

    def watch_downloads(self):
        """Called every few seconds by the watcher: imports new Tangerine files from
        Downloads. Returns the import results (empty if nothing new)."""
        s = self.settings.get()
        if not s["auto_import"] or self.db.get_meta("onboarded") != "1":
            return []
        ready = []
        for path in self._candidates(self.settings.downloads_dir()):
            try:
                st = path.stat()
            except OSError:
                continue
            signature = (st.st_mtime, st.st_size)
            if self._seen_files.get(str(path)) == signature:
                continue
            if datetime.now().timestamp() - st.st_mtime < 2:
                continue  # still being written
            self._seen_files[str(path)] = signature
            try:
                fmt = importer.detect(importer.read_text(path))
            except (importer.ImportError_, OSError):
                continue
            if fmt in ("tangerine_card", "tangerine_bank", "ofx"):
                ready.append(str(path))
        if not ready:
            return []
        outcome = self.import_files(ready)
        return outcome["results"]

    @exposed
    def choose_and_import(self):
        chooser = self.hooks.get("choose_files")
        paths = chooser() if chooser else []
        if not paths:
            return None
        return self.import_files(paths, archive=True)

    @exposed
    def imports(self):
        with self.db.read() as c:
            return _rows(c.execute(
                "SELECT i.*, a.name AS account_name FROM imports i LEFT JOIN accounts a ON a.id = i.account_id "
                "ORDER BY i.id DESC LIMIT 50"))

    @exposed
    def undo_import(self, import_id):
        with self.db.write("Remove import") as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM transactions WHERE import_id = ?", (import_id,))]
            for txn_id in ids:
                transfers.unlink(c, txn_id)
            c.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,))
            c.execute("DELETE FROM imports WHERE id = ?", (import_id,))
        return {"removed": len(ids), "counts": self.counts()}

    # ── onboarding ──

    @exposed
    def onboarding_state(self):
        with self.db.read() as c:
            accounts = self._accounts(c)
            first = c.execute("SELECT MIN(date) FROM transactions").fetchone()[0]
            per_account = [r[0] for r in c.execute(
                "SELECT MIN(date) FROM transactions GROUP BY account_id")]
        current = self.current_month()
        earliest = month_of(first) if first else current
        suggested = max(month_of(d) for d in per_account) if per_account else current
        return {"accounts": accounts, "months": months_between(earliest, current), "suggested_start": suggested,
                "today": self.today()}

    @exposed
    def use_starter_categories(self):
        with self.db.write("Add starter categories") as c:
            if c.execute("SELECT COUNT(*) FROM categories WHERE debt_account_id IS NULL").fetchone()[0]:
                return self.categories()
            for i, (group, names) in enumerate(STARTER_CATEGORIES):
                gid = c.execute("INSERT INTO category_groups (name, sort) VALUES (?, ?)", (group, i + 1)).lastrowid
                for j, name in enumerate(names):
                    c.execute("INSERT INTO categories (group_id, name, sort) VALUES (?, ?, ?)", (gid, name, j + 1))
            ledger.income_category(c)
        return self.categories()

    @exposed
    def finish_onboarding(self, start_month):
        self.set_budget_start(start_month)
        with self.db.write() as c:
            ledger.income_category(c)
            # Files already in Downloads count as looked at; only new downloads auto-import.
        for path in self._candidates(self.settings.downloads_dir()):
            try:
                st = path.stat()
                self._seen_files[str(path)] = (st.st_mtime, st.st_size)
            except OSError:
                pass
        self.db.set_meta("onboarded", "1")
        return self.bootstrap()

    @exposed
    def set_budget_start(self, month):
        if not (isinstance(month, str) and len(month) == 7):
            raise UserError("Pick a month to start the budget in.")
        self.db.set_meta("budget_start", month)
        return month

    # ── categories ──

    @exposed
    def categories(self):
        with self.db.read() as c:
            return self._category_tree(c)

    def _category_tree(self, c):
        groups = []
        for g in c.execute("SELECT * FROM category_groups ORDER BY sort, id"):
            cats = _rows(c.execute(
                "SELECT id, name, hidden, monthly_budget, debt_account_id, sort FROM categories "
                "WHERE group_id = ? ORDER BY sort, id", (g["id"],)))
            groups.append({**dict(g), "categories": cats})
        return groups

    def _next_sort(self, c, table, where="", params=()):
        return c.execute(f"SELECT COALESCE(MAX(sort), 0) + 1 FROM {table} {where}", params).fetchone()[0]

    @exposed
    def add_group(self, name):
        name = _clean_name(name)
        with self.db.write(f"Add group {name}") as c:
            income_sort = c.execute("SELECT MIN(sort) FROM category_groups WHERE role = 'income'").fetchone()[0]
            sort = self._next_sort(c, "category_groups", "WHERE role != 'income'")
            if income_sort is not None and sort >= income_sort:
                sort = income_sort - 1
            gid = c.execute("INSERT INTO category_groups (name, sort) VALUES (?, ?)", (name, sort)).lastrowid
        return gid

    @exposed
    def rename_group(self, group_id, name):
        name = _clean_name(name)
        with self.db.write(f"Rename group to {name}") as c:
            c.execute("UPDATE category_groups SET name = ? WHERE id = ?", (name, group_id))
        return True

    @exposed
    def delete_group(self, group_id):
        with self.db.write("Delete group") as c:
            g = c.execute("SELECT role FROM category_groups WHERE id = ?", (group_id,)).fetchone()
            if g is None:
                return False
            if g["role"] != "normal":
                raise UserError("This group is needed by the app and can't be deleted.")
            for cat in c.execute("SELECT id FROM categories WHERE group_id = ?", (group_id,)).fetchall():
                self._uncategorize(c, cat["id"])
            c.execute("DELETE FROM category_groups WHERE id = ?", (group_id,))
        return True

    @exposed
    def move_group(self, group_id, direction):
        with self.db.write("Move group") as c:
            groups = [r["id"] for r in c.execute(
                "SELECT id FROM category_groups WHERE role != 'income' ORDER BY sort, id")]
            if group_id not in groups:
                return False
            i = groups.index(group_id)
            j = i + (1 if direction > 0 else -1)
            if 0 <= j < len(groups):
                groups[i], groups[j] = groups[j], groups[i]
            for sort, gid in enumerate(groups, start=1):
                c.execute("UPDATE category_groups SET sort = ? WHERE id = ?", (sort, gid))
        return True

    @exposed
    def set_group_collapsed(self, group_id, collapsed):
        with self.db.write() as c:
            c.execute("UPDATE category_groups SET collapsed = ? WHERE id = ?", (1 if collapsed else 0, group_id))
        return True

    @exposed
    def add_category(self, group_id, name):
        name = _clean_name(name)
        with self.db.write(f"Add category {name}") as c:
            role = c.execute("SELECT role FROM category_groups WHERE id = ?", (group_id,)).fetchone()
            if role is None:
                raise UserError("That group no longer exists.")
            cid = c.execute("INSERT INTO categories (group_id, name, sort) VALUES (?, ?, ?)",
                            (group_id, name, self._next_sort(c, "categories", "WHERE group_id = ?", (group_id,)))).lastrowid
        return cid

    @exposed
    def rename_category(self, category_id, name):
        name = _clean_name(name)
        with self.db.write(f"Rename category to {name}") as c:
            row = c.execute("SELECT name, old_names FROM categories WHERE id = ?", (category_id,)).fetchone()
            if row is None:
                raise UserError("That category no longer exists.")
            old = [n for n in row["old_names"].split("|") if n]
            if row["name"] != name and row["name"] not in old:
                old.append(row["name"])
            c.execute("UPDATE categories SET name = ?, old_names = ? WHERE id = ?", (name, "|".join(old), category_id))
        return True

    def _uncategorize(self, c, category_id):
        c.execute("UPDATE transactions SET category_id = NULL, status = 'review', reason = 'Its category was deleted' "
                  "WHERE category_id = ?", (category_id,))
        c.execute("UPDATE splits SET category_id = NULL WHERE category_id = ?", (category_id,))
        c.execute("UPDATE transactions SET status = 'review', reason = 'A category in its split was deleted' "
                  "WHERE id IN (SELECT transaction_id FROM splits WHERE category_id IS NULL)")

    @exposed
    def delete_category(self, category_id, move_to=None):
        with self.db.write("Delete category") as c:
            row = c.execute("SELECT debt_account_id FROM categories WHERE id = ?", (category_id,)).fetchone()
            if row is None:
                return False
            if row["debt_account_id"]:
                raise UserError("A card's payment envelope goes away when you delete the account.")
            if category_id == ledger.income_category(c) or category_id == ledger.interest_category(c):
                raise UserError("This category is needed by the app and can't be deleted.")
            if move_to:
                c.execute("UPDATE transactions SET category_id = ? WHERE category_id = ?", (move_to, category_id))
                c.execute("UPDATE splits SET category_id = ? WHERE category_id = ?", (move_to, category_id))
                c.execute("UPDATE examples SET category_id = ? WHERE category_id = ?", (move_to, category_id))
                for b in c.execute("SELECT month, assigned FROM budget WHERE category_id = ?", (category_id,)).fetchall():
                    c.execute("INSERT INTO budget (month, category_id, assigned) VALUES (?, ?, ?) "
                              "ON CONFLICT(month, category_id) DO UPDATE SET assigned = assigned + excluded.assigned",
                              (b["month"], move_to, b["assigned"]))
            else:
                self._uncategorize(c, category_id)
            c.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        return True

    @exposed
    def move_category(self, category_id, group_id, before_id=None):
        with self.db.write("Move category") as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM categories WHERE group_id = ? AND id != ? ORDER BY sort, id", (group_id, category_id))]
            index = ids.index(before_id) if before_id in ids else len(ids)
            ids.insert(index, category_id)
            c.execute("UPDATE categories SET group_id = ? WHERE id = ?", (group_id, category_id))
            for sort, cid in enumerate(ids, start=1):
                c.execute("UPDATE categories SET sort = ? WHERE id = ?", (sort, cid))
        return True

    @exposed
    def set_category_hidden(self, category_id, hidden):
        with self.db.write("Hide category" if hidden else "Show category") as c:
            c.execute("UPDATE categories SET hidden = ? WHERE id = ?", (1 if hidden else 0, category_id))
        return True

    @exposed
    def set_monthly_budget(self, category_id, cents):
        value = None if cents in (None, "") else int(cents)
        if value is not None and value < 0:
            raise UserError("A monthly amount can't be negative.")
        with self.db.write("Change monthly amount") as c:
            c.execute("UPDATE categories SET monthly_budget = ? WHERE id = ?", (value, category_id))
        return True

    # ── the budget ──

    def _roles(self, c):
        income = ledger.income_category_ids(c)
        out = []
        for r in c.execute("SELECT id, debt_account_id FROM categories"):
            role = "income" if r["id"] in income else "payment" if r["debt_account_id"] else "normal"
            out.append({"id": r["id"], "role": role, "debt_account_id": r["debt_account_id"]})
        return out

    def _figures(self, c, end):
        start = self.budget_start()
        if not start or end < start:
            return {}, start
        lines = [(month_of(r["date"]), r["account_id"], ledger.is_debt(r["kind"]), r["category_id"], r["amount"])
                 for r in c.execute(
                     """
                     SELECT t.date, t.account_id, a.kind, t.category_id, t.amount FROM transactions t
                       JOIN accounts a ON a.id = t.account_id
                      WHERE t.transfer_account_id IS NULL AND t.is_split = 0 AND t.date BETWEEN ? AND ?
                     UNION ALL
                     SELECT t.date, t.account_id, a.kind, s.category_id, s.amount FROM splits s
                       JOIN transactions t ON t.id = s.transaction_id JOIN accounts a ON a.id = t.account_id
                      WHERE t.transfer_account_id IS NULL AND t.is_split = 1 AND t.date BETWEEN ? AND ?
                     """, (first_day(start), last_day(end), first_day(start), last_day(end)))]
        payments = [(month_of(r["date"]), r["transfer_account_id"], r["amount"]) for r in c.execute(
            """
            SELECT t.date, t.transfer_account_id, t.amount FROM transactions t
              JOIN accounts a ON a.id = t.account_id JOIN accounts b ON b.id = t.transfer_account_id
             WHERE a.kind IN ('chequing', 'savings', 'cash') AND b.kind IN ('credit_card', 'line_of_credit')
               AND t.date BETWEEN ? AND ?
            """, (first_day(start), last_day(end)))]
        assigned = {(r["month"], r["category_id"]): r["assigned"] for r in c.execute(
            "SELECT month, category_id, assigned FROM budget WHERE month BETWEEN ? AND ?", (start, end))}
        opening, _ = ledger.opening_cash(c, start)
        return budget_math.compute(start, end, self._roles(c), lines, payments, assigned, opening), start

    def _coverage_start(self, c):
        """The first month every account has data for (for averages)."""
        firsts = [r[0] for r in c.execute("SELECT MIN(date) FROM transactions GROUP BY account_id")]
        return max(month_of(d) for d in firsts) if firsts else self.current_month()

    def _spending_by_month(self, c, category_id=None):
        """{category_id: {month: net amount}} from every transaction, budget or not."""
        out = {}
        where = "AND t.category_id = ?" if category_id else ""
        where_split = "AND s.category_id = ?" if category_id else ""
        params = (category_id, category_id) if category_id else ()
        for r in c.execute(
                f"""
                SELECT t.category_id AS cid, substr(t.date, 1, 7) AS m, SUM(t.amount) AS total FROM transactions t
                 WHERE t.transfer_account_id IS NULL AND t.is_split = 0 AND t.category_id IS NOT NULL {where}
                 GROUP BY cid, m
                UNION ALL
                SELECT s.category_id, substr(t.date, 1, 7), SUM(s.amount) FROM splits s
                  JOIN transactions t ON t.id = s.transaction_id
                 WHERE t.transfer_account_id IS NULL AND t.is_split = 1 AND s.category_id IS NOT NULL {where_split}
                 GROUP BY s.category_id, substr(t.date, 1, 7)
                """, params):
            months = out.setdefault(r["cid"], {})
            months[r["m"]] = months.get(r["m"], 0) + r["total"]
        return out

    @exposed
    def month(self, month=None):
        month = month or self.current_month()
        current = self.current_month()
        with self.db.read() as c:
            start = self.budget_start()
            end = max(month, current)
            figures, start = self._figures(c, end)
            tree = self._category_tree(c)
            coverage = self._coverage_start(c)
            spending = self._spending_by_month(c)
            n = self.settings.get()["average_months"]
            avg_months = budget_math.complete_months_before(current, coverage, n)
            accounts = {a["id"]: a for a in self._accounts(c)}
            missing = [a["name"] for a in accounts.values() if a["balance"] is None and not a["closed"]]
            review = c.execute("SELECT COUNT(*) FROM transactions WHERE status = 'review' AND substr(date,1,7) = ?",
                               (month,)).fetchone()[0]
            data_first = c.execute("SELECT MIN(date) FROM transactions").fetchone()[0]
        month_figures = figures.get(month)
        groups = []
        for g in tree:
            if g["role"] == "income":
                continue
            cats = []
            for cat in g["categories"]:
                f = (month_figures or {}).get("categories", {}).get(cat["id"])
                history = spending.get(cat["id"], {})
                row = {
                    "id": cat["id"], "name": cat["name"], "hidden": cat["hidden"],
                    "monthly_budget": cat["monthly_budget"],
                    "assigned": f["assigned"] if f else 0,
                    "activity": f["activity"] if f else history.get(month, 0),
                    "available": f["available"] if f else None,
                    "carried": f["carried"] if f else 0,
                    "overspent_cash": f["overspent_cash"] if f else 0,
                    "overspent_credit": f["overspent_credit"] if f else 0,
                    "needed": budget_math.average_spending(history, avg_months) if not cat["debt_account_id"] else None,
                    "debt_account_id": cat["debt_account_id"],
                }
                if cat["debt_account_id"] and cat["debt_account_id"] in accounts:
                    owing = -(accounts[cat["debt_account_id"]]["balance"] or 0)
                    row["owing"] = owing
                    row["not_covered"] = max(0, owing - max(row["available"] or 0, 0))
                cats.append(row)
            groups.append({"id": g["id"], "name": g["name"], "role": g["role"], "collapsed": g["collapsed"],
                           "categories": cats,
                           "assigned": sum(x["assigned"] for x in cats), "activity": sum(x["activity"] for x in cats),
                           "available": sum((x["available"] or 0) for x in cats)})
        first_month = month_of(data_first) if data_first else current
        nav_from = min(first_month, start or current)
        nav_to = add_months(current, 3)
        future_short = [m for m, f in figures.items() if m > month and f["ready_to_assign"] < 0]
        return {
            "month": month, "current_month": current, "budget_start": start,
            "before_start": bool(start) and month < start, "is_past": month < current,
            "months": months_between(nav_from, max(nav_to, month)),
            "summary": {k: v for k, v in (month_figures or {}).items() if k != "categories"} or None,
            "groups": groups, "average_months": avg_months, "missing_balances": missing,
            "to_review": review, "future_short": future_short,
        }

    def _assigned(self, c, month, category_id):
        row = c.execute("SELECT assigned FROM budget WHERE month = ? AND category_id = ?", (month, category_id)).fetchone()
        return row["assigned"] if row else 0

    def _set_assigned(self, c, month, category_id, cents):
        c.execute("INSERT INTO budget (month, category_id, assigned) VALUES (?, ?, ?) "
                  "ON CONFLICT(month, category_id) DO UPDATE SET assigned = excluded.assigned",
                  (month, category_id, int(cents)))

    @exposed
    def set_assigned(self, month, category_id, cents):
        with self.db.write("Assign money") as c:
            self._set_assigned(c, month, category_id, cents)
        return self.month(month)

    @exposed
    def move_money(self, month, from_id, to_id, cents):
        cents = int(cents)
        if cents <= 0:
            raise UserError("Enter an amount to move.")
        with self.db.write("Move money") as c:
            if from_id:
                self._set_assigned(c, month, from_id, self._assigned(c, month, from_id) - cents)
            if to_id:
                self._set_assigned(c, month, to_id, self._assigned(c, month, to_id) + cents)
        return self.month(month)

    @exposed
    def assign_plan(self, month, amount=None):
        """How to share out money (a paycheque, or everything ready to assign): fills each
        category up to its monthly amount, in budget order (bills first if they're first)."""
        view = self.month(month)
        available = view["summary"]["ready_to_assign"] if view["summary"] else 0
        amount = available if amount in (None, "") else int(amount)
        remaining = max(0, amount)
        plan = []
        for g in view["groups"]:
            for cat in g["categories"]:
                if cat["hidden"]:
                    continue
                target = cat["monthly_budget"]
                if target is None:
                    continue
                need = max(0, target - cat["assigned"])
                give = min(need, remaining)
                remaining -= give
                plan.append({"category_id": cat["id"], "name": cat["name"], "group": g["name"], "need": need,
                             "monthly_budget": target, "assigned": cat["assigned"], "amount": give})
        return {"month": month, "amount": amount, "ready_to_assign": available, "plan": plan, "left_over": remaining,
                "without_monthly_amount": [cat["name"] for g in view["groups"] for cat in g["categories"]
                                           if cat["monthly_budget"] is None and not cat["hidden"]
                                           and not cat["debt_account_id"]]}

    @exposed
    def apply_plan(self, month, allocations):
        with self.db.write("Assign paycheque") as c:
            for a in allocations:
                cents = int(a["amount"])
                if cents:
                    cid = int(a["category_id"])
                    self._set_assigned(c, month, cid, self._assigned(c, month, cid) + cents)
        return self.month(month)

    @exposed
    def catch_up(self, month):
        """Assigns exactly what was spent, so no category ends the month overspent. For
        catching up on months gone by."""
        with self.db.write("Catch up month") as c:
            self._catch_up(c, month)
        return self.month(month)

    def _catch_up(self, c, month):
        for role in ("normal", "payment"):
            figures, _ = self._figures(c, month)
            f = figures.get(month)
            if f is None:
                raise UserError("That month is before the budget starts.")
            roles = {r["id"]: r["role"] for r in self._roles(c)}
            for cid, cat in f["categories"].items():
                if roles.get(cid) == role and cat["available"] < 0:
                    self._set_assigned(c, month, cid, cat["assigned"] - cat["available"])

    @exposed
    def catch_up_all(self):
        """Catches up every month from the start of the budget to last month."""
        start = self.budget_start()
        if not start:
            raise UserError("Set a start month first.")
        last = add_months(self.current_month(), -1)
        with self.db.write("Catch up past months") as c:
            for m in months_between(start, last):
                self._catch_up(c, m)
        return self.month(self.current_month())

    @exposed
    def category_detail(self, category_id, month=None):
        month = month or self.current_month()
        current = self.current_month()
        with self.db.read() as c:
            cat = c.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
            if cat is None:
                raise UserError("That category no longer exists.")
            coverage = self._coverage_start(c)
            history = self._spending_by_month(c, category_id).get(category_id, {})
            first = c.execute("SELECT MIN(date) FROM transactions").fetchone()[0]
            months = months_between(month_of(first) if first else current, current)
            averages = {}
            for n in (3, 6, 12):
                ms = budget_math.complete_months_before(current, coverage, n)
                if ms and (n == 3 or len(ms) == n):
                    averages[n] = {"months": len(ms), "amount": budget_math.average_spending(history, ms)}
            txns = self._transactions(c, {"category_id": category_id, "month": month}, limit=200)
            subs = _rows(c.execute(
                "SELECT DISTINCT s.id, s.name, s.amount, s.cadence FROM subscriptions s JOIN transactions t "
                "ON t.subscription_id = s.id WHERE t.category_id = ? AND s.status = 'active'", (category_id,)))
        last = add_months(current, -1)
        return {"id": cat["id"], "name": cat["name"], "monthly_budget": cat["monthly_budget"],
                "debt_account_id": cat["debt_account_id"], "note": cat["note"],
                "history": [{"month": m, "spent": -history.get(m, 0), "complete": m >= coverage and m < current}
                            for m in months],
                "last_month": {"month": last, "amount": max(0, -history.get(last, 0))},
                "averages": averages, "transactions": txns, "subscriptions": subs, "month": month}

    @exposed
    def set_category_note(self, category_id, note):
        """Saved as it's typed, so it isn't an undo step."""
        note = str(note or "")
        if len(note) > 5000:
            raise UserError("That note is too long.")
        with self.db.write() as c:
            c.execute("UPDATE categories SET note = ? WHERE id = ?", (note, category_id))
        return True

    @exposed
    def fill_monthly_from_averages(self, months=3, only_empty=True):
        """Sets each category's monthly amount to what it actually needed on average."""
        current = self.current_month()
        changed = 0
        with self.db.write("Fill monthly amounts") as c:
            coverage = self._coverage_start(c)
            spending = self._spending_by_month(c)
            ms = budget_math.complete_months_before(current, coverage, int(months))
            for cat in c.execute("SELECT c.id, c.monthly_budget FROM categories c JOIN category_groups g "
                                 "ON g.id = c.group_id WHERE g.role = 'normal' AND c.hidden = 0").fetchall():
                if only_empty and cat["monthly_budget"] is not None:
                    continue
                avg = budget_math.average_spending(spending.get(cat["id"], {}), ms)
                if avg > 0:
                    c.execute("UPDATE categories SET monthly_budget = ? WHERE id = ?", (avg, cat["id"]))
                    changed += 1
        return {"changed": changed}

    # ── inbox ──

    @exposed
    def inbox(self):
        with self.db.read() as c:
            items = self._transactions(c, {"status": "review"}, limit=1000, order="t.date DESC, t.id DESC")
            names = ledger.category_names(c)
            for item in items:
                txn = c.execute("SELECT * FROM transactions WHERE id = ?", (item["id"],)).fetchone()
                s = analyze(c, txn)
                item["suggestions"] = [{"id": cid, "name": names.get(cid, "?")} for cid in s.options]
                item["mixed"] = s.mixed
                item["group"] = s.group
                # Today's reasoning, not what was thought at import (the rules and what the app
                # has learnt both change). Only a warning, or a similar-name match, is kept.
                saved = item["reason"] or ""
                if "charged you again" not in saved:
                    matched = re.match(r"Matched to .*? \(similar name\)", saved)
                    item["reason"] = f"{matched.group(0)}. {s.reason}" if matched else s.reason
                if txn["similar_payee_id"]:
                    p = c.execute("SELECT id, name FROM payees WHERE id = ?", (txn["similar_payee_id"],)).fetchone()
                    item["similar_payee"] = dict(p) if p else None
                choices = subscriptions.price_change_choices(c, txn)
                if choices:
                    item["subscription_choices"] = choices
                    item["reason"] = subscriptions.unmatched_question(c, txn)
                    item["group"] = ""  # each one is its own question
            recent_imports = [r["id"] for r in c.execute("SELECT id FROM imports ORDER BY id DESC LIMIT 10")]
            filed = self._transactions(
                c, {"status": "done", "import_ids": recent_imports}, limit=300, order="t.date DESC, t.id DESC") \
                if recent_imports else []
            alerts = []
            for a in c.execute("SELECT id, name FROM accounts WHERE anchor_date IS NULL AND closed = 0"):
                alerts.append({"type": "balance", "account_id": a["id"],
                               "text": f"What's the balance of {a['name']}? The app needs it once to show balances."})
        return {"items": items, "filed": filed, "alerts": alerts, "counts": self.counts()}

    @exposed
    def approve(self, txn_id, category_id, refile=True):
        """Files one transaction. With `refile`, others from the same payee that the app is
        now sure about are filed too; "One at a time" in the inbox turns that off."""
        with self.db.write("File transaction") as c:
            payee = self._approve(c, txn_id, category_id)
            also = self._refile(c, [payee]) if refile else 0
            prompt = self._subscription_prompt(c, [txn_id])
        return {**self.counts(), "also_filed": also, "subscription_prompt": prompt}

    def _approve(self, c, txn_id, category_id):
        txn = c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if txn is None:
            raise UserError("That transaction no longer exists.")
        if txn["is_split"] or txn["transfer_account_id"]:
            c.execute("UPDATE transactions SET status = 'done', similar_payee_id = NULL WHERE id = ?", (txn_id,))
            return txn["payee_id"]
        if category_id in (None, ""):
            raise UserError("Pick a category first.")
        refund_of = None
        if txn["amount"] > 0:
            s = analyze(c, txn)
            if s.refund_of and s.options and s.options[0] == int(category_id):
                refund_of = s.refund_of
        c.execute("UPDATE transactions SET category_id = ?, status = 'done', reason = 'Filed by you', "
                  "similar_payee_id = NULL, refund_of = ? WHERE id = ?", (int(category_id), refund_of, txn_id))
        if txn["subscription_id"]:
            # A subscription without a category learns it from the first charge you file.
            c.execute("UPDATE subscriptions SET category_id = ? WHERE id = ? AND category_id IS NULL",
                      (int(category_id), txn["subscription_id"]))
            subscriptions.file_pending(c, txn["subscription_id"])
        return txn["payee_id"]

    @exposed
    def approve_many(self, items, refile=True):
        with self.db.write(f"File {len(items)} transactions") as c:
            payees = {self._approve(c, item["id"], item.get("category_id")) for item in items}
            also = self._refile(c, list(payees)) if refile else 0
            prompt = self._subscription_prompt(c, [item["id"] for item in items]) if len(
                {item.get("category_id") for item in items}) == 1 else None
        return {**self.counts(), "also_filed": also, "subscription_prompt": prompt}

    def _subscription_prompt(self, c, txn_ids):
        """After filing: when the charges look like a subscription or bill that isn't tracked
        yet (filed under a subscriptions category, or charging on a steady rhythm), the
        question to ask: what's it for? None otherwise."""
        rows = []
        for txn_id in txn_ids:
            r = c.execute("SELECT t.*, p.name AS payee_name, p.kind AS payee_kind, cat.name AS category_name "
                          "FROM transactions t JOIN payees p ON p.id = t.payee_id "
                          "LEFT JOIN categories cat ON cat.id = t.category_id WHERE t.id = ?", (txn_id,)).fetchone()
            if r and r["amount"] < 0 and not r["transfer_account_id"] and not r["subscription_id"] \
                    and r["category_id"] and r["payee_kind"] in ("merchant", "organization"):
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
        candidate = next((cand for cand in subscriptions.detect(c, self.today())
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
            why = f"You filed it under {first['category_name']}"
        return {"txn_id": first["id"], "payee": first["payee_name"], "amount": price, "count": count,
                "category_id": first["category_id"], "category": first["category_name"], "cadence": cadence,
                "kind": candidate["kind"] if candidate else "subscription", "candidate": candidate,
                "pricing": pricing, "why": why}

    @exposed
    def same_payee(self, txn_id, same):
        with self.db.write("Payee match") as c:
            txn = c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
            if txn is None or not txn["similar_payee_id"]:
                return self.counts()
            if same:
                target = c.execute("SELECT name FROM payees WHERE id = ?", (txn["similar_payee_id"],)).fetchone()
                self._set_payee(c, txn_id, target["name"])
                txn = c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
                s = analyze(c, txn)
                c.execute("UPDATE transactions SET category_id = ?, reason = ? WHERE id = ?",
                          (s.options[0] if s.options else txn["category_id"], s.reason, txn_id))
            c.execute("UPDATE transactions SET similar_payee_id = NULL WHERE similar_payee_id = ? AND payee_id = ?",
                      (txn["similar_payee_id"], txn["payee_id"]))
        return self.counts()

    @exposed
    def link_subscription(self, txn_id, sub_id):
        """This charge is a subscription you track, at a new price."""
        with self.db.write("New subscription price") as c:
            subscriptions.link_to(c, int(txn_id), int(sub_id))
        return self.counts()

    @exposed
    def subscription_from_transaction(self, txn_id, name, category_id=None, cadence="monthly", kind="subscription",
                                      pricing="fixed", ends_on=None):
        """This charge is a new subscription: track it from here on."""
        name = _clean_name(name)
        with self.db.write(f"Track {name}") as c:
            txn = c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
            if txn is None or txn["amount"] >= 0 or txn["payee_id"] is None:
                raise UserError("Only a charge can start a subscription.")
            category_id = int(category_id) if category_id else txn["category_id"]
            sub_id = subscriptions.create(c, txn["payee_id"], name, -txn["amount"], cadence, kind, category_id, pricing,
                                          _end_date(ends_on))
            c.execute("UPDATE transactions SET subscription_id = ? WHERE id = ?", (sub_id, txn_id))
            subscriptions.file_pending(c, sub_id)
        return self.counts()

    @exposed
    def not_a_subscription(self, txn_id):
        """The person says this charge isn't recurring: don't ask about this payee at this
        price again, and don't suggest it on the Subscriptions page."""
        with self.db.write("Not a subscription") as c:
            txn = c.execute("SELECT t.*, p.name AS payee_name FROM transactions t JOIN payees p ON p.id = t.payee_id "
                            "WHERE t.id = ?", (txn_id,)).fetchone()
            if txn is None or txn["amount"] >= 0:
                return False
            price = -txn["amount"]
            ids = [t["id"] for t in c.execute(
                "SELECT id, amount FROM transactions WHERE payee_id = ? AND amount < 0 AND subscription_id IS NULL",
                (txn["payee_id"],)) if subscriptions.same_price(-t["amount"], price)]
            subscriptions.dismiss(c, {"name": txn["payee_name"], "payee_id": txn["payee_id"], "amount": price,
                                      "cadence": "monthly", "account_id": txn["account_id"], "transaction_ids": ids})
        return True

    @exposed
    def set_always_ask(self, payee_id, always_ask):
        with self.db.write("Change payee setting") as c:
            c.execute("UPDATE payees SET always_ask = ? WHERE id = ?", (1 if always_ask else 0, payee_id))
        return True

    # ── transactions ──

    def _transactions(self, c, filters, limit=500, order="t.date DESC, t.id DESC"):
        where, params = ["1 = 1"], []
        f = filters or {}
        if f.get("account_id"):
            where.append("t.account_id = ?"); params.append(int(f["account_id"]))
        if f.get("category_id"):
            where.append("(t.category_id = ? OR t.id IN (SELECT transaction_id FROM splits WHERE category_id = ?))")
            params += [int(f["category_id"])] * 2
        if f.get("month"):
            where.append("t.date BETWEEN ? AND ?"); params += [first_day(f["month"]), last_day(f["month"])]
        if f.get("status"):
            where.append("t.status = ?"); params.append(f["status"])
        if f.get("import_ids"):
            where.append(f"t.import_id IN ({','.join('?' * len(f['import_ids']))})"); params += list(f["import_ids"])
        if f.get("uncategorized"):
            where.append("t.category_id IS NULL AND t.is_split = 0 AND t.transfer_account_id IS NULL")
        if f.get("search"):
            q = f"%{f['search'].strip()}%"
            where.append("(p.name LIKE ? OR t.raw_name LIKE ? OR t.raw_memo LIKE ? OR t.note LIKE ? "
                         "OR sub.name LIKE ? OR printf('%.2f', abs(t.amount) / 100.0) LIKE ?)")
            params += [q, q, q, q, q, q]
        rows = c.execute(
            f"""
            SELECT t.*, p.name AS payee_name, p.kind AS payee_kind, p.always_ask, a.name AS account_name,
                   a.kind AS account_kind, cat.name AS category_name, ta.name AS transfer_account_name,
                   sub.name AS subscription_name
              FROM transactions t
              LEFT JOIN payees p ON p.id = t.payee_id
              JOIN accounts a ON a.id = t.account_id
              LEFT JOIN categories cat ON cat.id = t.category_id
              LEFT JOIN accounts ta ON ta.id = t.transfer_account_id
              LEFT JOIN subscriptions sub ON sub.id = t.subscription_id AND sub.status != 'ignored'
             WHERE {' AND '.join(where)}
             ORDER BY {order} LIMIT {int(limit)}
            """, params).fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in ("id", "date", "amount", "account_id", "account_name", "account_kind", "payee_id",
                                   "payee_name", "payee_kind", "always_ask", "category_id", "category_name", "status",
                                   "reason", "note", "is_split", "transfer_account_id", "transfer_account_name",
                                   "transfer_id", "raw_name", "raw_memo", "bank_category", "refund_of",
                                   "subscription_id", "subscription_name", "import_id")}
            d["manual"] = r["fingerprint"] is None and r["fitid"] is None
            if r["is_split"]:
                d["splits"] = _rows(c.execute(
                    "SELECT s.id, s.category_id, s.amount, s.note, cat.name AS category_name FROM splits s "
                    "LEFT JOIN categories cat ON cat.id = s.category_id WHERE s.transaction_id = ? ORDER BY s.id",
                    (r["id"],)))
            out.append(d)
        return out

    @exposed
    def transactions(self, filters=None):
        with self.db.read() as c:
            items = self._transactions(c, filters or {}, limit=(filters or {}).get("limit", 500))
        return {"items": items, "total": sum(i["amount"] for i in items)}

    def _set_payee(self, c, txn_id, name):
        """This bank description belongs to payee `name` (an existing one, or a new one)."""
        name = _clean_name(name)
        txn = c.execute("SELECT t.*, a.kind FROM transactions t JOIN accounts a ON a.id = t.account_id "
                        "WHERE t.id = ?", (txn_id,)).fetchone()
        old = txn["payee_id"]
        old_row = c.execute("SELECT name, kind FROM payees WHERE id = ?", (old,)).fetchone() if old else None
        if old_row and old_row["name"].lower() == name.lower():
            return old
        others = c.execute("SELECT COUNT(*) FROM transactions WHERE payee_id = ? AND id != ?", (old, txn_id)).fetchone()[0] \
            if old else 0
        exists = c.execute("SELECT id FROM payees WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if old and not exists and others == 0:
            return ledger.rename_payee(c, old, name)  # its only transaction: just rename it
        target = exists["id"] if exists else ledger.get_or_create_payee(
            c, name, old_row["kind"] if old_row else "merchant")
        key = describe(txn["raw_name"], txn["raw_memo"], txn["kind"]).key if txn["raw_name"] else None
        if key:
            ledger.add_payee_key(c, key, target, "user")
            # Every transaction with the same bank description moves too.
            for other in c.execute("SELECT t.id, t.raw_name, t.raw_memo, a.kind FROM transactions t JOIN accounts a "
                                   "ON a.id = t.account_id WHERE t.payee_id = ?", (old,)).fetchall():
                if describe(other["raw_name"], other["raw_memo"], other["kind"]).key == key:
                    c.execute("UPDATE transactions SET payee_id = ? WHERE id = ?", (target, other["id"]))
        c.execute("UPDATE transactions SET payee_id = ? WHERE id = ?", (target, txn_id))
        if old and not c.execute("SELECT 1 FROM transactions WHERE payee_id = ?", (old,)).fetchone() \
                and not c.execute("SELECT 1 FROM payee_keys WHERE payee_id = ?", (old,)).fetchone():
            c.execute("DELETE FROM payees WHERE id = ?", (old,))
        return target

    @exposed
    def rename_payee(self, payee_id, name):
        with self.db.write("Rename payee") as c:
            return ledger.rename_payee(c, payee_id, name)

    @exposed
    def payees(self):
        with self.db.read() as c:
            return _rows(c.execute("SELECT id, name, kind, always_ask FROM payees ORDER BY name COLLATE NOCASE"))

    @exposed
    def update_transaction(self, txn_id, changes):
        changes = dict(changes or {})
        with self.db.write("Edit transaction") as c:
            txn = c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
            if txn is None:
                raise UserError("That transaction no longer exists.")
            manual = txn["fingerprint"] is None and txn["fitid"] is None
            if "payee" in changes and changes["payee"] is not None:
                self._set_payee(c, txn_id, changes["payee"])
            if "category_id" in changes:
                cid = changes["category_id"]
                if txn["transfer_account_id"]:
                    transfers.unlink(c, txn_id)
                c.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
                c.execute("UPDATE transactions SET category_id = ?, is_split = 0, status = ?, reason = ? WHERE id = ?",
                          (cid or None, "done" if cid else "review", "Filed by you" if cid else "", txn_id))
            if "note" in changes:
                c.execute("UPDATE transactions SET note = ? WHERE id = ?", (str(changes["note"] or ""), txn_id))
            if manual:
                if changes.get("date"):
                    c.execute("UPDATE transactions SET date = ? WHERE id = ?", (changes["date"], txn_id))
                if changes.get("amount") is not None:
                    c.execute("UPDATE transactions SET amount = ? WHERE id = ?", (int(changes["amount"]), txn_id))
            if "transfer_account_id" in changes:
                target = changes["transfer_account_id"]
                if target:
                    transfers.make_transfer(c, txn_id, int(target))
                else:
                    transfers.unlink(c, txn_id)
        return True

    @exposed
    def split_transaction(self, txn_id, parts):
        with self.db.write("Split transaction") as c:
            txn = c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
            if txn is None:
                raise UserError("That transaction no longer exists.")
            parts = [p for p in parts if int(p.get("amount") or 0) != 0]
            if len(parts) < 2:
                raise UserError("A split needs at least two parts.")
            if sum(int(p["amount"]) for p in parts) != txn["amount"]:
                raise UserError("The parts have to add up to the transaction's amount.")
            if any(not p.get("category_id") for p in parts):
                raise UserError("Every part needs a category.")
            if txn["transfer_account_id"]:
                transfers.unlink(c, txn_id)
            c.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
            for p in parts:
                c.execute("INSERT INTO splits (transaction_id, category_id, amount, note) VALUES (?, ?, ?, ?)",
                          (txn_id, int(p["category_id"]), int(p["amount"]), str(p.get("note") or "")))
            c.execute("UPDATE transactions SET is_split = 1, category_id = NULL, status = 'done', "
                      "reason = 'Split by you' WHERE id = ?", (txn_id,))
        return True

    @exposed
    def unsplit_transaction(self, txn_id):
        with self.db.write("Unsplit transaction") as c:
            c.execute("DELETE FROM splits WHERE transaction_id = ?", (txn_id,))
            c.execute("UPDATE transactions SET is_split = 0, status = 'review', reason = '' WHERE id = ?", (txn_id,))
        return True

    @exposed
    def add_transaction(self, account_id, date, payee, amount, category_id=None, note=""):
        if not date:
            raise UserError("Enter a date.")
        amount = int(amount)
        if amount == 0:
            raise UserError("Enter an amount.")
        with self.db.write("Add transaction") as c:
            payee_id = ledger.get_or_create_payee(c, _clean_name(payee)) if payee else None
            txn_id = c.execute(
                "INSERT INTO transactions (account_id, date, amount, payee_id, category_id, status, reason, note) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Added by you', ?)",
                (int(account_id), date, amount, payee_id, category_id or None, "done" if category_id else "review",
                 str(note or ""))).lastrowid
        return txn_id

    @exposed
    def delete_transaction(self, txn_id):
        with self.db.write("Delete transaction") as c:
            transfers.unlink(c, txn_id)
            c.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
        return self.counts()

    # ── accounts ──

    def _accounts(self, c):
        out = []
        for a in c.execute("SELECT * FROM accounts ORDER BY closed, sort, id").fetchall():
            stats = c.execute("SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS n FROM transactions "
                              "WHERE account_id = ?", (a["id"],)).fetchone()
            last_import = c.execute("SELECT MAX(imported_at) FROM imports WHERE account_id = ?", (a["id"],)).fetchone()[0]
            out.append({**dict(a), "balance": ledger.account_balance(c, a["id"]), "is_debt": ledger.is_debt(a["kind"]),
                        "kind_label": ledger.KIND_LABELS.get(a["kind"], a["kind"]), "first_date": stats["first"],
                        "last_date": stats["last"], "transactions": stats["n"], "last_import": last_import})
        return out

    @exposed
    def accounts(self):
        with self.db.read() as c:
            return self._accounts(c)

    @exposed
    def add_account(self, name, kind, balance=None, date=None):
        name = _clean_name(name)
        with self.db.write(f"Add account {name}") as c:
            if c.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
                raise UserError("There's already an account with that name.")
            account_id = ledger.create_account(c, name, kind)
            if balance not in (None, ""):
                cents = int(balance)
                c.execute("UPDATE accounts SET anchor_date = ?, anchor_balance = ? WHERE id = ?",
                          (date or self.today(), -abs(cents) if ledger.is_debt(kind) else cents, account_id))
        return account_id

    @exposed
    def update_account(self, account_id, changes):
        changes = dict(changes or {})
        with self.db.write("Edit account") as c:
            if "name" in changes:
                name = _clean_name(changes["name"])
                c.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))
                c.execute("UPDATE categories SET name = ? WHERE debt_account_id = ?", (f"{name} payment", account_id))
            if "apr" in changes:
                apr = changes["apr"]
                c.execute("UPDATE accounts SET apr = ? WHERE id = ?", (None if apr in (None, "") else float(apr), account_id))
            if "planned_payment" in changes:
                p = changes["planned_payment"]
                c.execute("UPDATE accounts SET planned_payment = ? WHERE id = ?",
                          (None if p in (None, "") else int(p), account_id))
            if "closed" in changes:
                c.execute("UPDATE accounts SET closed = ? WHERE id = ?", (1 if changes["closed"] else 0, account_id))
        return True

    @exposed
    def set_balance(self, account_id, cents, date=None):
        """The balance the bank shows (for a card or line of credit: what you owe, as a
        positive number). Every other balance is worked out from this one."""
        with self.db.write("Set balance") as c:
            acct = c.execute("SELECT kind FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if acct is None:
                raise UserError("That account no longer exists.")
            value = int(cents)
            if ledger.is_debt(acct["kind"]):
                value = -abs(value)
            c.execute("UPDATE accounts SET anchor_date = ?, anchor_balance = ? WHERE id = ?",
                      (date or self.today(), value, account_id))
        return self.accounts()

    @exposed
    def reconcile(self, account_id, cents):
        """The bank shows a different balance: add an adjustment for the difference today."""
        with self.db.write("Balance adjustment") as c:
            acct = c.execute("SELECT kind FROM accounts WHERE id = ?", (account_id,)).fetchone()
            target = int(cents)
            if ledger.is_debt(acct["kind"]):
                target = -abs(target)
            ours = ledger.account_balance(c, account_id)
            if ours is None:
                raise UserError("Set this account's balance first.")
            diff = target - ours
            if diff == 0:
                return {"adjusted": 0}
            payee = ledger.get_or_create_payee(c, "Balance adjustment", "bank")
            c.execute("INSERT INTO transactions (account_id, date, amount, payee_id, category_id, status, reason) "
                      "VALUES (?, ?, ?, ?, NULL, 'review', 'To match the balance the bank shows')",
                      (account_id, self.today(), diff, payee))
        return {"adjusted": diff}

    @exposed
    def delete_account(self, account_id):
        with self.db.write("Delete account") as c:
            for t in c.execute("SELECT id FROM transactions WHERE account_id = ? AND transfer_id IS NOT NULL",
                               (account_id,)).fetchall():
                transfers.unlink(c, t["id"])
            c.execute("UPDATE transactions SET transfer_account_id = NULL, status = 'review' "
                      "WHERE transfer_account_id = ?", (account_id,))
            c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return True

    @exposed
    def debt(self):
        """Each card / line of credit: what's owed, what's set aside, interest, and a payoff estimate."""
        current = self.current_month()
        with self.db.read() as c:
            figures, _ = self._figures(c, current)
            f = figures.get(current, {}).get("categories", {})
            payment_cats = ledger.payment_categories(c)
            interest_cat = ledger.interest_category(c) if payment_cats else None
            out = []
            for a in self._accounts(c):
                if not a["is_debt"] or a["closed"]:
                    continue
                owing = -(a["balance"] or 0)
                set_aside = max(0, f.get(payment_cats.get(a["id"]), {}).get("available", 0))
                months = [add_months(current, -i) for i in range(5, -1, -1)]
                interest = []
                for m in months:
                    total = c.execute(
                        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ? AND hint IN "
                        "('interest', 'fee') AND date BETWEEN ? AND ?", (a["id"], first_day(m), last_day(m))).fetchone()[0]
                    interest.append({"month": m, "amount": -total})
                balances = [{"month": m, "owing": -(ledger.account_balance(c, a["id"], last_day(m)) or 0)} for m in months]
                paid = c.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account_id = ? AND transfer_account_id "
                    "IS NOT NULL AND amount > 0 AND date >= ?", (a["id"], first_day(add_months(current, -3)))).fetchone()[0]
                payment = a["planned_payment"] or (round(paid / 3) if paid else None)
                out.append({"account_id": a["id"], "name": a["name"], "kind": a["kind"], "owing": owing,
                            "set_aside": set_aside, "not_covered": max(0, owing - set_aside), "apr": a["apr"],
                            "planned_payment": a["planned_payment"], "estimate_payment": payment,
                            "interest": interest, "balances": balances, "has_balance": a["balance"] is not None,
                            "payoff": payoff(owing, a["apr"], payment)})
        return {"accounts": out, "interest_category_id": interest_cat}

    # ── subscriptions ──

    @exposed
    def subscriptions(self):
        with self.db.read() as c:
            return subscriptions.listing(c, self.today())

    @exposed
    def track_subscription(self, candidate, kind=None, name=None, category_id=None, pricing=None,
                           ends_on=None):
        name = _clean_name(name) if name else None
        with self.db.write("Track subscription") as c:
            return subscriptions.track(c, candidate, kind, name, int(category_id) if category_id else None, pricing,
                                       _end_date(ends_on))

    @exposed
    def add_subscription(self, payee, name, amount, cadence="monthly", kind="subscription", category_id=None,
                         pricing="fixed", ends_on=None):
        """Tracks a subscription by hand: "Apple Music, $11.99 a month, from Apple"."""
        name = _clean_name(name)
        amount = abs(int(amount))
        if not amount:
            raise UserError("Enter the price.")
        if cadence not in ("weekly", "monthly", "yearly") or kind not in ("subscription", "bill"):
            raise UserError("Pick how often it charges.")
        with self.db.write(f"Add subscription {name}") as c:
            payee_id = payee if isinstance(payee, int) else ledger.get_or_create_payee(c, _clean_name(payee))
            return subscriptions.create(c, payee_id, name, amount, cadence, kind,
                                        int(category_id) if category_id else None, pricing, _end_date(ends_on))

    @exposed
    def dismiss_subscription(self, candidate):
        with self.db.write("Not a subscription") as c:
            return subscriptions.dismiss(c, candidate)

    @exposed
    def update_subscription(self, sub_id, changes):
        changes = dict(changes or {})
        with self.db.write("Edit subscription") as c:
            for field in ("name", "note", "kind", "cadence"):
                if field in changes:
                    value = str(changes[field] or "").strip()
                    if field == "name" and not value:
                        raise UserError("A subscription needs a name.")
                    if field == "kind" and value not in ("subscription", "bill"):
                        continue
                    if field == "cadence" and value not in ("weekly", "monthly", "yearly"):
                        continue
                    c.execute(f"UPDATE subscriptions SET {field} = ? WHERE id = ?", (value, sub_id))
            if changes.get("amount"):
                c.execute("UPDATE subscriptions SET amount = ? WHERE id = ?", (abs(int(changes["amount"])), sub_id))
            if "category_id" in changes:
                cid = int(changes["category_id"]) if changes["category_id"] else None
                c.execute("UPDATE subscriptions SET category_id = ? WHERE id = ?", (cid, sub_id))
                subscriptions.file_pending(c, sub_id)
            if changes.get("pricing") in subscriptions.PRICINGS:
                subscriptions.set_pricing(c, sub_id, changes["pricing"])
            if "ends_on" in changes:
                c.execute("UPDATE subscriptions SET ends_on = ? WHERE id = ?", (_end_date(changes["ends_on"]), sub_id))
        return True

    @exposed
    def cancel_subscription(self, sub_id, cancelled_on=None):
        with self.db.write("Mark subscription cancelled") as c:
            c.execute("UPDATE subscriptions SET status = 'cancelled', cancelled_on = ? WHERE id = ?",
                      (cancelled_on or self.today(), sub_id))
        return True

    @exposed
    def reactivate_subscription(self, sub_id):
        with self.db.write("Mark subscription active") as c:
            c.execute("UPDATE subscriptions SET status = 'active', cancelled_on = NULL WHERE id = ?", (sub_id,))
        return True

    @exposed
    def delete_subscription(self, sub_id):
        with self.db.write("Stop tracking subscription") as c:
            c.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
        return True

    # ── settings and the app ──

    @exposed
    def get_settings(self):
        return self.settings.get()

    @exposed
    def save_settings(self, changes):
        return self.settings.update(changes)

    @exposed
    def app_info(self):
        return self._app_info()

    @exposed
    def window_action(self, action, argument=None):
        """reveal_data, reveal_imports, reveal_log, check_updates, install_update, set_github_token"""
        hook = self.hooks.get(action)
        if hook is None:
            raise UserError("That only works in the app, not in the preview.")
        return hook(argument) if argument is not None else hook()

    @exposed
    def learn_actual_file(self):
        chooser = self.hooks.get("choose_files")
        paths = chooser() if chooser else []
        if not paths:
            return None
        return self.import_files(paths, archive=False)


def _end_date(value):
    """An optional end date: '' or None for none, else YYYY-MM-DD."""
    if value in (None, ""):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        raise UserError("That end date isn't a date.")
    return str(value)


def _clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise UserError("Enter a name.")
    if len(name) > 80:
        raise UserError("That name is too long.")
    return name


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
