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

import banks
import csvlayout
import importer
import ledger
import subscriptions
import transfers
from figures import Figures
from learn import analyze, guess
from payees import TRANSFER_OR_BANK_ENTRY_HINTS, describe
from settings import DASHBOARD_MONTHS
from util import add_months, first_day, last_day, month_of, months_between, parse_date, today

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
        self._file_status = {}            # path → ((mtime, size), what it needs or None): see _needs()

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
        except (UserError, importer.ImportError_, ValueError) as e:  # LayoutError is a ValueError
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
        pending = len(self.pending_files()) if self.db.get_meta("onboarded") == "1" else 0
        return {"inbox": review + pending, "accounts_without_balance": no_balance, "undo": self.db.undo_label()}

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
            with self.db.read() as c:
                parsed = importer.parse(path, c)
                needs = self._needs(c, path, parsed)
        except importer.NeedsSetup:
            needs, parsed = "layout", None
        except (importer.ImportError_, OSError) as e:
            return {**item, "format": None, "error": str(e)}
        if needs:
            return {**item, "format": "csv", "needs_setup": needs, **self._csv_summary(path)}
        item.update(format=parsed.format)
        if parsed.format == "actual":
            item.update(rows=len(parsed.actual_rows), account_name="Actual Budget export",
                        first_date=min((r["date"] for r in parsed.actual_rows), default=None),
                        last_date=max((r["date"] for r in parsed.actual_rows), default=None))
            return item
        names = []
        with self.db.read() as c:
            for part in parsed.parts:
                account_id = importer.find_account(c, part.account, part.rows, importer.fingerprints(part.rows))
                name = c.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()["name"] \
                    if account_id else part.account.name
                names.append(f"{name} ••{part.account.last4}" if part.account.last4 and
                             part.account.kind == "credit_card" and part.account.last4 not in name else name)
            seen = c.execute("SELECT 1 FROM imports WHERE sha256 = ?", (importer.sha256_of(path),)).fetchone()
        item.update(rows=len(parsed.rows), account_name=", ".join(names), kind=parsed.parts[0].account.kind
                    if parsed.parts else None, first_date=parsed.first_date, last_date=parsed.last_date,
                    already_imported=bool(seen))
        return item

    def _needs(self, c, path, parsed):
        """What a parsed file needs before it can be imported: 'account' for a set-up CSV
        whose account the app doesn't know yet, 'new_account' for any other bank file with
        an account the app should ask about (see _accounts_to_ask), else None."""
        if parsed.format == "actual":
            return None
        if parsed.format != "csv":
            return "new_account" if self._accounts_to_ask(c, parsed) else None
        part = parsed.parts[0]
        found = importer.find_account(c, part.account, part.rows, importer.fingerprints(part.rows))
        return None if found else "account"

    def _accounts_to_ask(self, c, parsed, chosen=()):
        """The parts of a bank file whose account the app hasn't seen, when you already have
        an account it could be (a card for a card's file, a bank account for a bank
        account's): a new account, or one you have under a new number? Not while setting
        up the budget, when every account is new. `chosen`: part indexes already answered."""
        if self.db.get_meta("onboarded") != "1":
            return []
        kinds = {ledger.is_debt(r["kind"]) for r in c.execute("SELECT kind FROM accounts WHERE closed = 0")}
        out = []
        for i, part in enumerate(parsed.parts):
            if i in chosen or ledger.is_debt(part.account.kind) not in kinds:
                continue
            if not importer.find_account(c, part.account, part.rows, importer.fingerprints(part.rows)):
                out.append(i)
        return out

    def _csv_summary(self, path):
        sniffed = csvlayout.sniff(importer.read_text(path))
        rows = []
        if sniffed:
            for r in sniffed["rows"]:
                try:
                    rows.append(parse_any_date(r[sniffed["guess"]["date_col"]]))
                except ValueError:
                    continue
        return {"rows": len(sniffed["rows"]) if sniffed else 0, "first_date": min(rows, default=None),
                "last_date": max(rows, default=None)}

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
        return self._import_files(paths, archive)

    def _import_files(self, paths, archive=True, chosen=None):
        """`chosen`: {path: {part index: account id}} for accounts the person picked."""
        chosen = {str(Path(k)): v for k, v in (chosen or {}).items()}
        downloads = self.settings.downloads_dir().resolve()
        results, learnt, errors, needs_setup = [], None, [], []
        parsed_files = []
        for p in paths:
            path = Path(p)
            try:
                with self.db.read() as c:
                    parsed_files.append((path, importer.parse(path, c)))
            except importer.NeedsSetup as e:
                needs_setup.append({"path": str(path), "filename": path.name, "needs": e.what})
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
            accounts = chosen.get(str(path), {})
            if parsed.format != "csv":
                with self.db.read() as c:
                    if self._accounts_to_ask(c, parsed, accounts):
                        needs_setup.append({"path": str(path), "filename": path.name, "needs": "new_account"})
                        continue
            try:
                file_results = self._import_one(path, parsed, archive and in_downloads, accounts)
            except importer.NeedsSetup as e:
                needs_setup.append({"path": str(path), "filename": path.name, "needs": e.what})
                continue
            results += file_results
        refiled = self.refile() if learnt or results else 0
        return {"results": results, "learnt": learnt, "errors": errors, "needs_setup": needs_setup,
                "refiled": refiled, "counts": self.counts()}

    def _import_one(self, path, parsed, archive, accounts=None):
        sha = importer.sha256_of(path)
        with self.db.write() as c:
            file_results = importer.import_parsed(c, parsed, path.name, sha, accounts=accounts)
            if archive:
                archived = self._archive(path)
                for result in file_results:
                    c.execute("UPDATE imports SET archived_path = ? WHERE id = ?", (archived, result.import_id))
        for result in file_results:
            log.info("Imported %s into %s: %d new, %d already there, %d filed, %d to review", path.name,
                     result.account_name, result.added, result.duplicates, result.filed, result.to_review)
        return [r.as_dict() for r in file_results]

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
        """Called every few seconds by the watcher: imports new bank files from Downloads
        (a CSV that needs setting up waits in the Inbox instead). Returns what
        import_files() did, or None if there was nothing new."""
        s = self.settings.get()
        if not s["auto_import"] or self.db.get_meta("onboarded") != "1":
            return None
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
            if fmt in importer.BANK_FORMATS:
                ready.append(str(path))
        if not ready:
            return None
        outcome = self.import_files(ready)
        with self.db.read() as c:
            ignored = {r["sha256"] for r in c.execute("SELECT sha256 FROM ignored_files")}
        outcome["needs_setup"] = [f for f in outcome["needs_setup"] if importer.sha256_of(f["path"]) not in ignored]
        outcome["auto"] = True  # the page offers to set files up rather than asking straight away
        return outcome if outcome["results"] or outcome["needs_setup"] else None

    @exposed
    def choose_and_import(self):
        chooser = self.hooks.get("choose_files")
        paths = chooser() if chooser else []
        if not paths:
            return None
        return self.import_files(paths, archive=True)

    # ── CSVs that need setting up ──

    def pending_files(self):
        """Files in Downloads that look like bank transactions but need setting up (or an
        account) first, oldest first. Files you said not to import are left out."""
        out = []
        with self.db.read() as c:
            ignored = {r["sha256"] for r in c.execute("SELECT sha256 FROM ignored_files")}
        for path in self._candidates(self.settings.downloads_dir()):
            try:
                st = path.stat()
            except OSError:
                continue
            signature = (st.st_mtime, st.st_size)
            cached = self._file_status.get(str(path))
            if cached and cached[0] == signature:
                needs, sha = cached[1], cached[2]
            else:
                needs, sha = self._file_needs(path), None
                try:
                    sha = importer.sha256_of(path)
                except OSError:
                    continue
                self._file_status[str(path)] = (signature, needs, sha)
            if needs and sha not in ignored:
                out.append({"path": str(path), "filename": path.name, "needs": needs, "modified": st.st_mtime})
        return sorted(out, key=lambda f: f["modified"])

    def _file_needs(self, path):
        try:
            text = importer.read_text(path)
            if importer.detect(text) not in importer.BANK_FORMATS:
                return None
            with self.db.read() as c:
                return self._needs(c, path, importer.parse(path, c))
        except importer.NeedsSetup as e:
            return e.what
        except (importer.ImportError_, OSError, ValueError):
            return None

    def _forget_file_status(self):
        """After a layout or account is set up, files waiting in Downloads are looked at again."""
        self._file_status.clear()
        self._seen_files = {}

    # ── bank files for an account the app hasn't seen ──

    @exposed
    def file_accounts(self, path):
        """For the "new account?" pop-up: each account in a bank file, whether the app
        knows it, and the accounts you have that it could be: every open one, the file's
        sort (card or bank account) first, since what a file says it is can be wrong."""
        path = Path(path)
        with self.db.read() as c:
            parsed = importer.parse(path, c)
            if parsed.format in ("actual", "csv"):
                raise UserError("That isn't a file the app asks this about.")
            accounts = [a for a in self._accounts(c) if not a["closed"]]
            parts = []
            for i, part in enumerate(parsed.parts):
                hint = part.account
                found = importer.find_account(c, hint, part.rows, importer.fingerprints(part.rows))
                parts.append({
                    "index": i, "account_id": found, "name": importer.suggested_name(c, hint), "kind": hint.kind,
                    "kind_label": ledger.KIND_LABELS.get(hint.kind, hint.kind), "bank": hint.bank,
                    "last4": hint.last4, "rows": len(part.rows), "first_date": part.first_date,
                    "last_date": part.last_date,
                    "choices": [{"id": a["id"], "name": a["name"]} for a in sorted(
                        accounts, key=lambda a: a["is_debt"] != ledger.is_debt(hint.kind))],
                })
        return {"path": str(path), "filename": path.name, "parts": parts}

    @exposed
    def set_file_accounts(self, path, choices):
        """Which account each new account in a bank file is, then imports it. `choices`:
        [{'index', 'account_id'}] for one you have (it remembers the file's account number,
        so the next file goes there on its own) or [{'index', 'name'}] for a new account."""
        path = Path(path)
        picked = {}
        with self.db.write() as c:
            parsed = importer.parse(path, c)
            for choice in choices or []:
                i = int(choice["index"])
                if not 0 <= i < len(parsed.parts):
                    continue
                hint = parsed.parts[i].account
                if choice.get("account_id"):
                    if not c.execute("SELECT 1 FROM accounts WHERE id = ?", (int(choice["account_id"]),)).fetchone():
                        raise UserError("That account no longer exists.")
                    picked[i] = int(choice["account_id"])
                else:
                    typed = str(choice.get("name") or "").strip()
                    name = _clean_name(typed) if typed else importer.suggested_name(c, hint)
                    if c.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
                        raise UserError(f"There's already an account called {name}.")
                    picked[i] = importer.create_account_for(c, hint, name)
        self._forget_file_status()
        downloads = self.settings.downloads_dir().resolve()
        return self._import_files([str(path)], archive=path.resolve().parent == downloads, chosen={str(path): picked})

    @exposed
    def csv_setup(self, path):
        """What the set-up pop-up shows for a CSV: its first rows, and the layout to start
        from (the saved one for files like it, else a guess)."""
        path = Path(path)
        sniffed = csvlayout.sniff(importer.read_text(path))
        if sniffed is None:
            raise UserError("This doesn't look like a bank file: no column of dates and amounts.")
        with self.db.read() as c:
            saved = importer.saved_layout(c, sniffed)
            suggested_account = None
            if saved:
                key = importer.csv_account_key(saved["id"], path.name)
                suggested_account = ledger.account_by_key(c, [key])
        width = sniffed["width"]
        header = sniffed["header"] or [f"Column {i + 1}" for i in range(width)]
        layout = {k: v for k, v in (saved or sniffed["guess"]).items() if k != "id"}
        return {"path": str(path), "filename": path.name, "header": header, "has_header": bool(sniffed["header"]),
                "sample": sniffed["rows"][:6], "rows": len(sniffed["rows"]), "layout": layout,
                "saved": saved is not None, "account_id": suggested_account,
                "suggested_name": importer.base_name(path.name)}

    @exposed
    def csv_preview(self, path, layout):
        """The first transactions as the app would read them with this layout."""
        text = importer.read_text(path)
        sniffed = csvlayout.sniff(text)
        clean = csvlayout.clean_layout(layout, sniffed["width"])
        rows = csvlayout.read(text, clean)
        return {"rows": [{"date": d, "amount": a, "description": t} for d, a, t in rows[:5]], "total": len(rows),
                "first_date": min((r[0] for r in rows), default=None),
                "last_date": max((r[0] for r in rows), default=None)}

    @exposed
    def setup_csv(self, path, layout, account):
        """Saves how to read files like this one and which account this one is for, then
        imports it. `account`: {'id'} for an existing account, or {'name', 'kind', 'bank'}."""
        path = Path(path)
        text = importer.read_text(path)
        sniffed = csvlayout.sniff(text)
        if sniffed is None:
            raise UserError("This doesn't look like a bank file.")
        csvlayout.read(text, csvlayout.clean_layout(layout, sniffed["width"]))  # every row must fit
        with self.db.write() as c:
            layout_id = importer.save_layout(c, sniffed, layout)
            if account.get("id"):
                account_id = int(account["id"])
                if not c.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
                    raise UserError("That account no longer exists.")
            else:
                name, bank = _name_with_bank(account.get("name"), account.get("bank"))
                if c.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
                    raise UserError("There's already an account with that name.")
                if account.get("kind") not in ledger.ACCOUNT_KINDS:
                    raise UserError("Pick the type of account.")
                account_id = ledger.create_account(c, name, account["kind"], bank=bank)
            c.execute("INSERT INTO account_sources (key, account_id) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                      "SET account_id = excluded.account_id", (importer.csv_account_key(layout_id, path.name), account_id))
        self._forget_file_status()
        downloads = self.settings.downloads_dir().resolve()
        return self.import_files([str(path)], archive=path.resolve().parent == downloads)

    @exposed
    def ignore_file(self, path):
        """Not a bank file (or not one to import): never ask about it again."""
        path = Path(path)
        with self.db.write() as c:
            c.execute("INSERT OR IGNORE INTO ignored_files (sha256, filename) VALUES (?, ?)",
                      (importer.sha256_of(path), path.name))
        return self.counts()

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
            return ledger.category_tree(c)

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
                raise UserError("A card's payment category goes away when you delete the account.")
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
            raise UserError("A budgeted amount can't be negative.")
        with self.db.write("Change budgeted amount") as c:
            c.execute("UPDATE categories SET monthly_budget = ? WHERE id = ?", (value, category_id))
        return True

    # ── the budget ──

    def _figures(self, c):
        """Every money figure, from one reading of the database (figures.Figures). Use it
        inside the same db.read() or db.write()."""
        s = self.settings.get()
        return Figures(c, self.today(), s["overspending"], s["average_months"])

    @exposed
    def month(self, month=None):
        month = month or self.current_month()
        current = self.current_month()
        with self.db.read() as c:
            f = self._figures(c)
            figures = f.month(month)
            tree, start, first_month, missing = f.tree, f.start, f.first_month or current, f.without_balance
            review = c.execute("SELECT COUNT(*) FROM transactions WHERE status = 'review' AND substr(date,1,7) = ?",
                               (month,)).fetchone()[0]
        groups = []
        for g in tree:
            if g["role"] == "income":
                continue
            cats = [{"id": cat["id"], "name": cat["name"], "hidden": cat["hidden"], "monthly_budget": cat["monthly_budget"],
                     "debt_account_id": cat["debt_account_id"], **figures["categories"][cat["id"]]}
                    for cat in g["categories"]]
            groups.append({"id": g["id"], "name": g["name"], "role": g["role"], "collapsed": g["collapsed"],
                           "categories": cats, **figures["groups"][g["id"]]})
        nav_from = min(first_month, start or current)
        nav_to = add_months(current, 3)
        return {
            "month": month, "current_month": current, "budget_start": start,
            "before_start": bool(start) and month < start, "is_past": month < current,
            "months": months_between(nav_from, max(nav_to, month)),
            "summary": figures["summary"], "groups": groups, "missing_balances": missing,
            "to_review": review, "future_short": figures["short_later"], "overspending": f.overspending,
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
        """How to share out money (a paycheque, or everything ready to assign): see
        Figures.assign_plan."""
        with self.db.read() as c:
            f = self._figures(c)
            plan = f.assign_plan(month, amount)
            tree = f.tree
        names = {cat["id"]: (cat["name"], g["name"]) for g in tree for cat in g["categories"]}
        for item in plan["plan"]:
            item["name"], item["group"] = names[item["category_id"]]
        plan["without_monthly_amount"] = [cat["name"] for g in tree if g["role"] != "income" for cat in g["categories"]
                                          if cat["monthly_budget"] is None and not cat["hidden"]
                                          and not cat["debt_account_id"]]
        return plan

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
            self._catch_up(c, [month])
        return self.month(month)

    def _catch_up(self, c, months):
        changes = self._figures(c).catch_up(months)
        if changes is None:
            raise UserError("That month is before the budget starts.")
        for (month, category_id), cents in changes.items():
            self._set_assigned(c, month, category_id, cents)

    @exposed
    def catch_up_all(self):
        """Catches up every month from the start of the budget to last month."""
        start = self.budget_start()
        if not start:
            raise UserError("Set a start month first.")
        with self.db.write("Catch up past months") as c:
            self._catch_up(c, months_between(start, add_months(self.current_month(), -1)))
        return self.month(self.current_month())

    @exposed
    def category_detail(self, category_id, month=None):
        month = month or self.current_month()
        with self.db.read() as c:
            cat = c.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
            if cat is None:
                raise UserError("That category no longer exists.")
            f = self._figures(c)
            history = f.history(category_id)
            spent = self._spent(c, f, [category_id], month)
            subs = _rows(c.execute(
                "SELECT DISTINCT s.id, s.name, s.amount, s.cadence FROM subscriptions s JOIN transactions t "
                "ON t.subscription_id = s.id WHERE t.category_id = ? AND s.status = 'active'", (category_id,)))
        return {"id": cat["id"], "name": cat["name"], "monthly_budget": cat["monthly_budget"],
                "debt_account_id": cat["debt_account_id"], "note": cat["note"], **history,
                "spent": spent, "subscriptions": subs, "month": month}

    @exposed
    def spent_detail(self, month, category_id=None, group_id=None):
        """What makes up a category's (or a group's) Spent on the Budget page in a month:
        every transaction and split line, adding up to that figure."""
        with self.db.read() as c:
            if group_id:
                group = c.execute("SELECT name FROM category_groups WHERE id = ?", (group_id,)).fetchone()
                if group is None:
                    raise UserError("That group no longer exists.")
                name = group["name"]
                ids = [r["id"] for r in c.execute("SELECT id FROM categories WHERE group_id = ?", (group_id,))]
            else:
                cat = c.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
                if cat is None:
                    raise UserError("That category no longer exists.")
                name, ids = cat["name"], [category_id]
            spent = self._spent(c, self._figures(c), ids, month)
        return {"name": name, "month": month, "group": bool(group_id), **spent}

    def _spent(self, c, f, category_ids, month):
        """The lines behind these categories' Spent in `month` (Figures.spent), with what
        the page shows for each: the payee and account, or a label for money that isn't a
        transaction."""
        if not category_ids:
            return {"lines": [], "total": 0, "transactions": []}
        spent = f.spent(category_ids, month)
        names = ledger.category_names(c)
        income = ledger.income_category_ids(c)
        lines = []
        for line in spent["lines"]:
            line = dict(line)
            if "moved_from" in line:
                source = line.pop("moved_from")
                line["label"] = ("Set aside for card spending not in a category yet" if source is None else
                                 f"{names.get(source, 'A deleted category')} on the card (you owe less)" if source in income
                                 else f"Set aside for card spending in {names.get(source, 'a deleted category')}")
            lines.append(line)
        txns = {t["id"]: t for t in self._transactions(c, {"ids": sorted({l["id"] for l in lines if l["id"]})},
                                                       limit=100000)} if lines else {}
        for line in lines:
            t = txns.get(line["id"])
            line["category_name"] = names.get(line["category_id"])
            if t:
                line.update(payee_name=t["payee_name"] or t["raw_name"], account_name=t["account_name"],
                            full_amount=t["amount"], status=t["status"], note=t["note"],
                            subscription_name=t["subscription_name"])
                if line.get("payment"):
                    line["label"] = f"Paid to the card from {t['account_name']}"
        lines.sort(key=lambda l: (l["date"] or "", l["id"] or 0), reverse=True)  # money moved in goes last
        return {"lines": lines, "total": spent["total"], "transactions": list(txns.values())}

    @exposed
    def set_category_note(self, category_id, note):
        """Saved as it's typed, so it isn't an undo step."""
        note = str(note or "")
        if len(note) > 5000:
            raise UserError("That note is too long.")
        with self.db.write() as c:
            c.execute("UPDATE categories SET note = ? WHERE id = ?", (note, category_id))
        return True

    # ── dashboard ──

    @exposed
    def dashboard(self, months=None):
        """The page the app opens on: Ready to assign, the inbox, this month's overspent
        categories, cash and what's owed; net worth at the end of each month; then what each
        category spent (the same figures as Spent). All over the last `months` whole months
        (3, 6, 12 or "all"; by default the Dashboard's saved choice) and this month so far.
        Each whole month is compared with the category's Usual for it (the whole months
        before it) and marked when it's above usual: at least $25 and 25% more."""
        months = self.settings.get()["dashboard_months"] if months is None else months
        if months not in DASHBOARD_MONTHS or isinstance(months, bool):
            raise UserError("Pick 3, 6 or 12 months, or All.")
        current = self.current_month()
        inbox = self.counts()["inbox"]
        with self.db.read() as c:
            f = self._figures(c)
            figures = f.month(current)
            tree, coverage, first_month = f.tree, f.coverage, f.first_month or current
            cols = months_between(first_month if months == "all" else max(first_month, add_months(current, -months)),
                                  current)
            spending = f.spending(cols)
            worth = f.worth([m for m in cols if m >= coverage])
            now = f.balances()
            missing = f.without_balance
        names = {cat["id"]: cat for g in tree for cat in g["categories"]}
        group_names = {g["id"]: g["name"] for g in tree}
        groups = [{"id": g["id"], "name": group_names[g["id"]], "spent": g["spent"], "above": g["above"],
                   "categories": [{**x, "name": names[x["id"]]["name"], "hidden": bool(names[x["id"]]["hidden"])}
                                  for x in g["categories"]]}
                  for g in spending["groups"]]
        overspent = sorted(({"id": cid, "name": names[cid]["name"], "amount": amount} for cid, amount in figures["overspent"]),
                           key=lambda x: -x["amount"])
        summary = figures["summary"]
        return {
            "month": current, "summary": summary, "overspending": f.overspending,
            "inbox": inbox, "waiting": summary["waiting"] if summary else 0, "overspent": overspent,
            "overspent_total": figures["overspent_total"],
            "cash": now["cash"], "owed": now["owed"], "has_debt": now["has_debt"],
            "missing_balances": missing, "net_worth": worth,
            "spending": {"range": months, "usual_months": f.usual_months, "income": spending["income"],
                         "total": spending["total"], "groups": groups,
                         "income_group": next((g["id"] for g in tree if g["role"] == "income"), None),
                         "months": [{"month": m, "so_far": m == current, "complete": m >= coverage} for m in cols]},
        }

    # ── inbox ──

    @exposed
    def inbox(self):
        with self.db.read() as c:
            items = self._transactions(c, {"status": "review"}, limit=1000, order="t.date DESC, t.id DESC")
            names = ledger.category_names(c)
            income_id = ledger.income_category(c)
            for item in items:
                txn = c.execute("SELECT * FROM transactions WHERE id = ?", (item["id"],)).fetchone()
                s = analyze(c, txn)
                item["suggestions"] = [{"id": cid, "name": names.get(cid, "?")} for cid in s.options]
                item["guess"] = guess(txn, s, income_id)
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
            alerts = [{"type": "duplicate", **d} for d in ledger.duplicate_accounts(c)]
            copies = {a["account_id"] for a in alerts}
            for a in c.execute("SELECT id, name FROM accounts WHERE anchor_date IS NULL AND closed = 0"):
                if a["id"] in copies:
                    continue  # merge it rather than give it a balance
                alerts.append({"type": "balance", "account_id": a["id"],
                               "text": f"What's the balance of {a['name']}? The app needs it once to show balances."})
        return {"items": items, "filed": filed, "alerts": alerts, "files": self.pending_files(), "counts": self.counts()}

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
        c.execute("UPDATE transactions SET category_id = ?, status = 'done', reason = 'Categorized by you', "
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
            why = f"You put it in {first['category_name']}"
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
                c.execute("UPDATE transactions SET reason = ? WHERE id = ?", (analyze(c, txn).reason, txn_id))
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
                                      pricing="fixed", ends_on=None, amount=None, note=""):
        """This charge is a new subscription: track it from here on (at its price, unless
        `amount` says otherwise)."""
        name = _clean_name(name)
        with self.db.write(f"Track {name}") as c:
            txn = c.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
            if txn is None or txn["amount"] >= 0 or txn["payee_id"] is None:
                raise UserError("Only a charge can start a subscription.")
            category_id = int(category_id) if category_id else txn["category_id"]
            price = abs(int(amount)) if amount else -txn["amount"]
            sub_id = subscriptions.create(c, txn["payee_id"], name, price, cadence, kind, category_id, pricing,
                                          _end_date(ends_on))
            if note:
                c.execute("UPDATE subscriptions SET note = ? WHERE id = ?", (str(note).strip(), sub_id))
            c.execute("UPDATE transactions SET subscription_id = ? WHERE id = ?", (sub_id, txn_id))
            subscriptions.file_pending(c, sub_id)
        return {"subscription_id": sub_id, "counts": self.counts()}

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
        """Switches a payee's Always ask on or off. Switched off, its waiting transactions
        the app is now sure about are categorized. Returns how many."""
        with self.db.write("Change payee setting") as c:
            c.execute("UPDATE payees SET always_ask = ? WHERE id = ?", (1 if always_ask else 0, payee_id))
            return 0 if always_ask else self._refile(c, [payee_id])

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
        if f.get("ids"):
            where.append(f"t.id IN ({','.join('?' * len(f['ids']))})"); params += [int(i) for i in f["ids"]]
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
            SELECT t.*, p.name AS payee_name, p.always_ask, a.name AS account_name,
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
                                   "payee_name", "always_ask", "category_id", "category_name", "status",
                                   "reason", "note", "is_split", "transfer_account_id", "transfer_account_name",
                                   "transfer_id", "raw_name", "raw_memo", "bank_category", "refund_of",
                                   "subscription_id", "subscription_name", "import_id")}
            d["manual"] = r["fingerprint"] is None and r["fitid"] is None
            # Bank entries are never asked about, and a transfer isn't categorized.
            d["can_always_ask"] = bool(r["payee_id"]) and r["hint"] not in TRANSFER_OR_BANK_ENTRY_HINTS
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
            c, name, old_row["kind"] if old_row else "merchant", always_ask=txn["hint"] == "etransfer")
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
            return _rows(c.execute("SELECT id, name, always_ask FROM payees ORDER BY name COLLATE NOCASE"))

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
                          (cid or None, "done" if cid else "review", "Categorized by you" if cid else "", txn_id))
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
        duplicates = {d["account_id"]: d for d in ledger.duplicate_accounts(c)}
        for a in c.execute("SELECT * FROM accounts ORDER BY closed, sort, id").fetchall():
            stats = c.execute("SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS n FROM transactions "
                              "WHERE account_id = ?", (a["id"],)).fetchone()
            last_import = c.execute("SELECT MAX(imported_at) FROM imports WHERE account_id = ?", (a["id"],)).fetchone()[0]
            out.append({**dict(a), "balance": ledger.account_balance(c, a["id"]), "is_debt": ledger.is_debt(a["kind"]),
                        "kind_label": ledger.KIND_LABELS.get(a["kind"], a["kind"]), "first_date": stats["first"],
                        "last_date": stats["last"], "transactions": stats["n"], "last_import": last_import,
                        "duplicate_of": duplicates.get(a["id"])})
        return out

    @exposed
    def accounts(self):
        with self.db.read() as c:
            return self._accounts(c)

    @exposed
    def bank_names(self):
        """Suggestions for an account's bank: the banks already used, then well-known ones."""
        with self.db.read() as c:
            used = [r["bank"] for r in c.execute("SELECT DISTINCT bank FROM accounts WHERE bank IS NOT NULL ORDER BY bank")]
        return used + [b for b in banks.SUGGESTED if b not in used]

    @exposed
    def add_account(self, name, kind, balance=None, date=None, bank=None):
        name, bank = _name_with_bank(name, bank)
        with self.db.write(f"Add account {name}") as c:
            if c.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
                raise UserError("There's already an account with that name.")
            account_id = ledger.create_account(c, name, kind, bank=bank)
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
                if c.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE AND id != ?",
                             (name, account_id)).fetchone():
                    raise UserError("There's already an account with that name.")
                c.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account_id))
                c.execute("UPDATE categories SET name = ? WHERE debt_account_id = ?", (f"{name} payment", account_id))
            if "bank" in changes:
                c.execute("UPDATE accounts SET bank = ? WHERE id = ?", (_clean_bank(changes["bank"]), account_id))
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
    def merge_accounts(self, from_id, into_id):
        """`from_id` is the same account as `into_id`: see ledger.merge_accounts()."""
        with self.db.write("Merge accounts") as c:
            name = c.execute("SELECT name FROM accounts WHERE id = ?", (into_id,)).fetchone()
            moved, dropped = ledger.merge_accounts(c, int(from_id), int(into_id))
        log.info("Merged account %s into %s: %d moved, %d duplicates dropped", from_id, into_id, moved, dropped)
        return {"into": name["name"] if name else "", "moved": moved, "dropped": dropped, "counts": self.counts()}

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
        """Each card / line of credit: what's owed, what's set aside, interest, and a payoff
        estimate (Figures.debts)."""
        with self.db.read() as c:
            debts = self._figures(c).debts()
            accounts = {r["id"]: r for r in c.execute("SELECT id, name, kind, apr, planned_payment FROM accounts")}
            payment_cats = ledger.payment_categories(c)
            interest_cat = ledger.interest_category(c) if payment_cats else None
        out = [{**d, "name": accounts[d["account_id"]]["name"], "kind": accounts[d["account_id"]]["kind"],
                "apr": accounts[d["account_id"]]["apr"], "planned_payment": accounts[d["account_id"]]["planned_payment"]}
               for d in debts]
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
        """open_data, open_imports, open_log, open_release_notes, check_updates, install_update,
        set_github_token"""
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


def _clean_bank(name):
    """A bank as typed: tidied, a known bank's usual name ("royal bank" → RBC), or None."""
    name = " ".join(str(name or "").split())
    if len(name) > 80:
        raise UserError("That bank name is too long.")
    return banks.canonical(name) or None


def _name_with_bank(name, bank):
    """(name, bank) for a new account. A name that starts with the bank as typed uses the
    bank's usual name instead: 'atb financial Chequing' → 'ATB Chequing'."""
    typed = " ".join(str(bank or "").split())
    bank = _clean_bank(typed)
    name = " ".join(str(name or "").split())
    if typed and bank and name.lower().startswith(typed.lower()):
        name = bank + name[len(typed):]
    return _clean_name(name), bank


def parse_any_date(text):
    try:
        return parse_date(text)
    except ValueError:
        return parse_date(text, "dmy")


def _clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise UserError("Enter a name.")
    if len(name) > 80:
        raise UserError("That name is too long.")
    return name

