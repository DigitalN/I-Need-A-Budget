# SPDX-License-Identifier: GPL-3.0-only
"""The SQLite database: schema, migrations and undo. Backups are backups.py's.

One connection, shared by every thread behind a lock (pywebview runs each call from
the page on its own thread, and the Downloads watcher has its own). Writes go through
`Database.write()`, which opens a transaction; give it a label and the change can be
undone with ⌘Z.

Undo uses the classic SQLite recipe: triggers on every table record the SQL that
reverses each insert, update and delete into `undo_log`, grouped by step. Undoing
runs a step's statements backwards. Updates only record the columns that changed, so
undoing one step never reverts an unrelated change made later to the same row. The log
is cleared at launch: undo is for this session.

A change made without a label (an import, the re-check) clears the log when it changes
anything: undoing an earlier step would replay its SQL over rows that change touched (deleting
an account takes the transactions an import just put in it). The triggers watch for that.
`keep_undo` is for a change no step can trip over: folding a group, a note as it's typed.

A big step (adding, deleting or merging an account, group or category, Remove import...)
says what undoing it does (`big_step`): ⌘Z asks before undoing it (`undo_step`). An undo
that fails is a UserError, and its step is dropped, so ⌘Z never gets stuck on it.

Money is stored in integer cents. Dates are ISO strings.
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import backups
from errors import UserError

log = logging.getLogger("inab")

SCHEMA_VERSION = 10

CANT_UNDO = "That can't be undone any more."

# undo_state.active: what the triggers do with a change.
_OFF, _RECORDING, _WATCHING, _CHANGED = 0, 1, 2, 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- chequing | savings | cash | credit_card | line_of_credit
    bank TEXT,                           -- the bank or credit union, as the person calls it
    last4 TEXT,                          -- card digits, shown next to the name
    anchor_date TEXT,                    -- the balance every other is worked out from, at the end of this day
    anchor_balance INTEGER,
    anchor_source TEXT,                  -- where it came from: file | typed (none: no balance yet)
    check_from INTEGER REFERENCES imports(id) ON DELETE SET NULL,  -- the import whose file balance later
                                         -- files' balances are checked against (ledger.balance_difference)
    apr REAL,                            -- debt: yearly interest rate (%), for the payoff estimate
    planned_payment INTEGER,             -- debt: what you plan to pay each month
    sort INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- How imported files find their account: 'number:rbc:1234' (bank and account number, from any
-- kind of file), 'tangerine-card:1234', 'tangerine-bank:chequing', 'csv:3:statement' (a set-up
-- CSV layout and the file's name), ...
CREATE TABLE IF NOT EXISTS account_sources (
    key TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE
);

-- CSV files the app was shown how to read ("set it up once"), found by their header row.
CREATE TABLE IF NOT EXISTS csv_layouts (
    id INTEGER PRIMARY KEY,
    signature TEXT NOT NULL UNIQUE,      -- importer.csv_signature(): the header, or the shape of a headerless file
    has_header INTEGER NOT NULL DEFAULT 1,
    date_col INTEGER NOT NULL,
    date_order TEXT NOT NULL DEFAULT 'mdy',  -- mdy | dmy | ymd
    desc_cols TEXT NOT NULL,             -- column numbers, comma-separated, joined in this order
    amount_col INTEGER,                  -- one signed amount column...
    out_col INTEGER,                     -- ...or money out and money in in two
    in_col INTEGER,
    negate INTEGER NOT NULL DEFAULT 0,   -- the amount column shows money out as positive
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Files in Downloads the person said aren't bank files: never asked about again.
CREATE TABLE IF NOT EXISTS ignored_files (
    sha256 TEXT PRIMARY KEY,
    filename TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'normal', -- normal | income | debt
    sort INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES category_groups(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    monthly_budget INTEGER,              -- what this category needs a month (cents), if set
    debt_account_id INTEGER UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,  -- a debt's payment envelope
    old_names TEXT NOT NULL DEFAULT '',  -- earlier names, '|'-separated, so other apps' exports still match
    note TEXT NOT NULL DEFAULT ''        -- the person's own notes about this category
);

CREATE TABLE IF NOT EXISTS payees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'merchant', -- what the bank's text first said (payees.Description.kind). A payee
                                           -- is just a name (docs/adr/0001): this only keeps bank wording
                                           -- out of similar-name matching
    always_ask INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payee_keys (
    key TEXT PRIMARY KEY,                -- payees.merchant_key() of a bank description
    payee_id INTEGER NOT NULL REFERENCES payees(id) ON DELETE CASCADE,
    how TEXT NOT NULL DEFAULT 'exact'    -- exact | brand | similar | user
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    format TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_new INTEGER NOT NULL DEFAULT 0,
    first_date TEXT,
    last_date TEXT,
    covers_from TEXT,                    -- the first day the file says it covers (a Quicken file's DTSTART;
                                         -- none: it doesn't say): see ledger.balance_difference
    archived_path TEXT,
    anchor_before_date TEXT,             -- the balance this import replaced (none: the account had none) ...
    anchor_before_balance INTEGER,
    anchor_before_source TEXT,
    anchor_after_date TEXT,              -- ... and the one it set (none: it didn't move the balance)
    anchor_after_balance INTEGER,
    balance_note TEXT NOT NULL DEFAULT ''  -- what the import said about the balance, shown with it
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    payee_id INTEGER REFERENCES payees(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL,             -- the usual charge, positive cents
    cadence TEXT NOT NULL DEFAULT 'monthly',   -- weekly | monthly | yearly
    kind TEXT NOT NULL DEFAULT 'subscription', -- subscription | bill
    status TEXT NOT NULL DEFAULT 'active',     -- active | cancelled | ignored
    pricing TEXT NOT NULL DEFAULT 'fixed',     -- fixed | currency (billed in US$, wobbles) | varies (a power bill)
    ends_on TEXT,                              -- optional: a charge after this date is flagged
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,  -- its charges are filed here
    note TEXT NOT NULL DEFAULT '',
    cancelled_on TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount INTEGER NOT NULL,             -- cents; negative = money out of the account
    payee_id INTEGER REFERENCES payees(id) ON DELETE SET NULL,
    raw_name TEXT NOT NULL DEFAULT '',   -- exactly what the bank wrote
    raw_memo TEXT NOT NULL DEFAULT '',
    bank_type TEXT NOT NULL DEFAULT '',  -- DEBIT / CREDIT / OTHER / ATM ...
    bank_category TEXT NOT NULL DEFAULT '',  -- the card's own category ("Restaurant")
    hint TEXT NOT NULL DEFAULT '',       -- transfer | interest | income | cash | fee | etransfer | adjustment
                                         -- | added_half (a transfer's other half the app added: payees.ADDED_HALF)
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    is_split INTEGER NOT NULL DEFAULT 0,
    transfer_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    transfer_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    refund_of INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    similar_payee_id INTEGER REFERENCES payees(id) ON DELETE SET NULL,  -- "same as this payee?" to ask
    status TEXT NOT NULL DEFAULT 'review',   -- review (in the inbox) | done
    by_you INTEGER NOT NULL DEFAULT 0,   -- you decided it (categorized, split, a transfer or not, sent back, added):
                                         -- nothing the app does on its own changes it (docs/adr/0005)
    reason TEXT NOT NULL DEFAULT '',     -- why it's where it is: only text to show, never read back
    note TEXT NOT NULL DEFAULT '',
    fingerprint TEXT,                    -- importer.fingerprint(); never change how it's computed
    fitid TEXT,                          -- OFX's own transaction ID
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    after_import INTEGER,                -- a balance adjustment from Check balance…: the newest import that set the
                                         -- account's balance when you checked (ledger.balance_difference); not a
                                         -- reference, so it outlives that import being removed
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS transactions_fingerprint
    ON transactions(account_id, fingerprint) WHERE fingerprint IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS transactions_fitid
    ON transactions(account_id, fitid) WHERE fitid IS NOT NULL;
CREATE INDEX IF NOT EXISTS transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS transactions_payee ON transactions(payee_id);
CREATE INDEX IF NOT EXISTS transactions_status ON transactions(status);

CREATE TABLE IF NOT EXISTS splits (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    amount INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS splits_transaction ON splits(transaction_id);

CREATE TABLE IF NOT EXISTS budget (
    month TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    assigned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, category_id)
);

-- Categorised examples learnt from another app's export (Actual Budget), used like
-- past transactions when guessing categories.
CREATE TABLE IF NOT EXISTS examples (
    id INTEGER PRIMARY KEY,
    payee_id INTEGER NOT NULL REFERENCES payees(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    amount INTEGER NOT NULL,
    date TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS undo_groups (
    grp INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    undoes TEXT                          -- a big step: what undoing it does ("Savings is deleted."); ⌘Z asks
);
CREATE TABLE IF NOT EXISTS undo_log (
    seq INTEGER PRIMARY KEY,
    grp INTEGER NOT NULL,
    sql TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS undo_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active INTEGER NOT NULL DEFAULT 0,
    grp INTEGER
);
INSERT OR IGNORE INTO undo_state (id, active) VALUES (1, 0);
"""

# Tables whose changes can be undone.
UNDO_TABLES = ("meta", "accounts", "account_sources", "category_groups", "categories", "payees", "payee_keys",
               "imports", "subscriptions", "transactions", "splits", "budget", "examples", "csv_layouts",
               "ignored_files")


class Database:
    def __init__(self, path, backup_dir=None):
        """`backup_dir`: where backups go (main.py's BACKUP_DIR); by default a folder beside the
        database, so tests and the dev preview keep theirs in their own folder."""
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent / backups.FOLDER_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._migrate()
        self._install_undo_triggers()
        with self.lock:
            self.conn.execute("DELETE FROM undo_log")
            self.conn.execute("DELETE FROM undo_groups")
            self.conn.execute("UPDATE undo_state SET active = 0, grp = NULL")

    # ── schema ──

    def _migrate(self):
        """Creates the schema, or upgrades an older database step by step. An existing
        database is copied to the backups folder before any upgrade touches it."""
        with self.lock:
            has_meta = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
            version = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone() if has_meta else None
            version = int(version[0]) if version else None
            if version is not None and version < SCHEMA_VERSION:
                self._backup_before_upgrade(version)
            self.conn.executescript(SCHEMA)
            if version is not None and version < 2:
                columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(categories)")]
                if "note" not in columns:
                    self.conn.execute("ALTER TABLE categories ADD COLUMN note TEXT NOT NULL DEFAULT ''")
                columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(subscriptions)")]
                if "pricing" not in columns:
                    self.conn.execute("ALTER TABLE subscriptions ADD COLUMN pricing TEXT NOT NULL DEFAULT 'fixed'")
                if "ends_on" not in columns:
                    self.conn.execute("ALTER TABLE subscriptions ADD COLUMN ends_on TEXT")
            if version is not None and version < 3:
                columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(accounts)")]
                if "bank" not in columns:
                    self.conn.execute("ALTER TABLE accounts ADD COLUMN bank TEXT")
                # Until version 3 the app only knew Tangerine's files: every account that came
                # from a file is a Tangerine account.
                self.conn.execute("UPDATE accounts SET bank = 'Tangerine' WHERE bank IS NULL AND id IN "
                                  "(SELECT account_id FROM account_sources)")
            if version is not None and version < 4:
                # A payee is just a name (docs/adr/0001): being an e-transfer moves from the payee
                # (kind 'person') to its transactions, and anyone you e-transfer with gets Always ask,
                # which keeps what the app did before.
                self.conn.execute("UPDATE transactions SET hint = 'etransfer' WHERE hint = '' AND payee_id IN "
                                  "(SELECT id FROM payees WHERE kind = 'person')")
                self.conn.execute("UPDATE payees SET always_ask = 1 WHERE kind = 'person'")
            if version is not None and version < 5:
                # An inbox item counts in no category until it's categorized (docs/adr/0002):
                # the tentative category (its top suggestion) it used to carry goes.
                self.conn.execute("UPDATE transactions SET category_id = NULL WHERE status = 'review' AND is_split = 0")
            if version is not None and version < 6:
                # What you decided stays decided (docs/adr/0005): who decided becomes a fact. Until now
                # the only record was a reason ("Filed by you" is what versions before 1.0.8 wrote), and
                # sending a transaction back (no reason) or Not a transfer weren't recorded at all. A
                # transfer marked by hand can't be told from one the app paired: it stays the app's.
                columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(transactions)")]
                if "by_you" not in columns:
                    self.conn.execute("ALTER TABLE transactions ADD COLUMN by_you INTEGER NOT NULL DEFAULT 0")
                self.conn.execute(
                    "UPDATE transactions SET by_you = 1 WHERE reason IN ('Categorized by you', 'Filed by you', "
                    "'Split by you', 'Added by you') OR (status = 'review' AND reason IN ('No longer a transfer', ''))")
                self.conn.execute("UPDATE transactions SET reason = 'You sent this back' "
                                  "WHERE status = 'review' AND reason = '' AND by_you = 1")
            if version is not None and version < 7:
                # Removing an import puts back the balance it replaced. Imports made before this
                # remember nothing, so removing one leaves the balance where it is, as it did.
                columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(imports)")]
                for column, kind in (("anchor_before_date", "TEXT"), ("anchor_before_balance", "INTEGER"),
                                     ("anchor_after_date", "TEXT"), ("anchor_after_balance", "INTEGER")):
                    if column not in columns:
                        self.conn.execute(f"ALTER TABLE imports ADD COLUMN {column} {kind}")
            if version is not None and version < 8:
                self._upgrade_to_8()
            if version is not None and version < 9:
                # The first day each file says it covers, so the older history a file brings isn't counted by the
                # balance check (issue #136). Imports made before this don't know it: their first row's date stands in.
                columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(imports)")]
                if "covers_from" not in columns:
                    self.conn.execute("ALTER TABLE imports ADD COLUMN covers_from TEXT")
            if version is not None and version < 10:
                # Check balance… no longer ends the balance check (issue #137): its balance adjustment remembers the
                # newest import that set the balance then, so a later file's balance can take it over.
                columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(transactions)")]
                if "after_import" not in columns:
                    self.conn.execute("ALTER TABLE transactions ADD COLUMN after_import INTEGER")
            # Next upgrade: `if version is not None and version < 11: ...`, then raise SCHEMA_VERSION.
            self.conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
            # Which accounts onboarding made: the newest of them (service.finish_onboarding). A budget set up
            # before 1.4.0 didn't remember: every account it has counts as one, as before (issue #83). Only
            # ever written once, so accounts added after this are told apart.
            self.conn.execute("INSERT OR IGNORE INTO meta (key, value) SELECT 'last_onboarding_account', "
                              "(SELECT COALESCE(MAX(id), 0) FROM accounts) "
                              "WHERE EXISTS (SELECT 1 FROM meta WHERE key = 'onboarded' AND value = '1')")
            if version is not None and version < SCHEMA_VERSION:
                log.info("Upgraded the database from version %d to %d", version, SCHEMA_VERSION)

    def _upgrade_to_8(self):
        """Where each balance came from (a file's balance is checked against the next file's: issue
        #43), what each import said about its balance, and which undo steps ask first (#42)."""
        def add(table, column, kind):
            if column not in [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        add("accounts", "anchor_source", "TEXT")
        add("accounts", "check_from", "INTEGER REFERENCES imports(id) ON DELETE SET NULL")
        add("imports", "anchor_before_source", "TEXT")
        add("imports", "balance_note", "TEXT NOT NULL DEFAULT ''")
        add("undo_groups", "undoes", "TEXT")
        # A balance an import set (imports remember that since version 7) came from that file, and
        # later files are checked against it. One from an older import is from a file too, but which
        # one isn't known: the next file's balance is taken as it is, and checks start from it. An
        # account no Quicken file ever went into (the only files with a balance) has a typed one.
        self.conn.execute(
            "UPDATE accounts SET check_from = (SELECT MAX(i.id) FROM imports i WHERE i.account_id = accounts.id "
            "AND i.anchor_after_date = accounts.anchor_date AND i.anchor_after_balance = accounts.anchor_balance) "
            "WHERE anchor_date IS NOT NULL")
        self.conn.execute(
            "UPDATE accounts SET anchor_source = CASE WHEN check_from IS NOT NULL OR EXISTS (SELECT 1 FROM imports i "
            "WHERE i.account_id = accounts.id AND i.format = 'ofx') THEN 'file' ELSE 'typed' END "
            "WHERE anchor_date IS NOT NULL")

    def _backup_before_upgrade(self, version):
        target = backups.save(self, backups.upgrade_name(version, SCHEMA_VERSION, datetime.now()))
        log.info("Saved a copy of the database before upgrading it: %s", target)

    def _install_undo_triggers(self):
        """Recording a step: each change's reverse SQL goes into undo_log. Watching a change made
        without a label: the first change that really changes something marks it (_CHANGED), and
        write() then clears the log."""
        with self.lock:
            active = "(SELECT active FROM undo_state WHERE id = 1)"
            for table in UNDO_TABLES:
                columns = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]
                for suffix in ("ins", "upd", "del"):
                    self.conn.execute(f"DROP TRIGGER IF EXISTS undo_{table}_{suffix}")

                def trigger(suffix, when, timing, reverse_sql):
                    self.conn.execute(
                        f"CREATE TRIGGER undo_{table}_{suffix} {timing} ON {table} WHEN {when} BEGIN "
                        f"INSERT INTO undo_log (grp, sql) SELECT grp, {reverse_sql} FROM undo_state "
                        f"WHERE id = 1 AND active = {_RECORDING}; "
                        f"UPDATE undo_state SET active = {_CHANGED} WHERE id = 1 AND active = {_WATCHING}; END")

                either = f"{active} IN ({_RECORDING}, {_WATCHING})"
                trigger("ins", either, "AFTER INSERT", f"'DELETE FROM {table} WHERE rowid = ' || NEW.rowid")
                changed = " || ".join(
                    f"CASE WHEN OLD.{c} IS NOT NEW.{c} THEN ', {c} = ' || quote(OLD.{c}) ELSE '' END" for c in columns)
                differs = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in columns)
                trigger("upd", f"({active} = {_RECORDING} OR ({active} = {_WATCHING} AND ({differs})))", "AFTER UPDATE",
                        f"'UPDATE {table} SET rowid = rowid' || {changed} || ' WHERE rowid = ' || OLD.rowid")
                values = " || ', ' || ".join(f"quote(OLD.{c})" for c in columns)
                trigger("del", either, "BEFORE DELETE",
                        f"'INSERT INTO {table} (rowid, {', '.join(columns)}) VALUES (' || OLD.rowid || ', ' || {values} || ')'")

    # ── access ──

    @contextmanager
    def read(self):
        with self.lock:
            yield self.conn

    @contextmanager
    def write(self, label=None, keep_undo=False):
        """A transaction. Nested calls join the outer one. With a `label`, the whole
        change becomes one step of undo (the outermost label wins). Without one, a change
        it makes clears undo (an import, the re-check: see the module docstring), unless
        `keep_undo`: a change no step's undo can trip over."""
        with self.lock:
            outer = self._depth == 0
            self._depth += 1
            try:
                if outer:
                    self.conn.execute("BEGIN IMMEDIATE")
                    if label:
                        cur = self.conn.execute("INSERT INTO undo_groups (label) VALUES (?)", (label,))
                        self.conn.execute("UPDATE undo_state SET active = ?, grp = ?", (_RECORDING, cur.lastrowid))
                    elif not keep_undo:
                        self.conn.execute("UPDATE undo_state SET active = ?, grp = NULL", (_WATCHING,))
                yield self.conn
                if outer:
                    if label:
                        self.conn.execute("UPDATE undo_state SET active = ?", (_OFF,))
                        self.conn.execute(
                            "DELETE FROM undo_groups WHERE grp = (SELECT grp FROM undo_state) "
                            "AND NOT EXISTS (SELECT 1 FROM undo_log WHERE grp = undo_groups.grp)")
                        self.conn.execute(
                            "DELETE FROM undo_log WHERE grp NOT IN "
                            "(SELECT grp FROM undo_groups ORDER BY grp DESC LIMIT 100)")
                        self.conn.execute(
                            "DELETE FROM undo_groups WHERE grp NOT IN "
                            "(SELECT grp FROM undo_groups ORDER BY grp DESC LIMIT 100)")
                    elif not keep_undo:
                        changed = self.conn.execute("SELECT active FROM undo_state WHERE id = 1").fetchone()[0] == _CHANGED
                        self.conn.execute("UPDATE undo_state SET active = ?", (_OFF,))
                        if changed:
                            self._clear_undo()
                    self.conn.execute("COMMIT")
            except BaseException:
                if outer:
                    self.conn.execute("ROLLBACK")
                    self.conn.execute("UPDATE undo_state SET active = ?", (_OFF,))
                raise
            finally:
                self._depth -= 1

    def _clear_undo(self):
        self.conn.execute("DELETE FROM undo_log")
        self.conn.execute("DELETE FROM undo_groups")

    def big_step(self, undoes):
        """Inside a labelled write(): this step is a big one, and `undoes` says what undoing it
        does ("Savings is deleted."). ⌘Z asks before undoing it; the Undo in its toast doesn't."""
        self.conn.execute(f"UPDATE undo_groups SET undoes = ? WHERE grp = (SELECT grp FROM undo_state "
                          f"WHERE id = 1 AND active = {_RECORDING})", (undoes,))

    def undo_label(self):
        step = self.undo_step()
        return step["label"] if step else None

    def undo_step(self):
        """The step undo would reverse: {'step': its number, 'label', 'big', 'undoes'}, or None."""
        with self.read() as c:
            row = c.execute("SELECT grp, label, undoes FROM undo_groups ORDER BY grp DESC LIMIT 1").fetchone()
            if row is None:
                return None
            return {"step": row["grp"], "label": row["label"], "big": row["undoes"] is not None,
                    "undoes": row["undoes"] or ""}

    def undo(self, step=None):
        """Reverses the last step, or exactly `step` (undo_step()'s number), returning its label;
        None if there's nothing to undo. A UserError when `step` isn't the last step any more, or
        the undo fails (something since has changed what it would put back): that step is dropped."""
        with self.lock:
            row = self.conn.execute("SELECT grp, label FROM undo_groups ORDER BY grp DESC LIMIT 1").fetchone()
            if step is not None and (row is None or row["grp"] != step):
                raise UserError(CANT_UNDO)
            if row is None:
                return None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("PRAGMA defer_foreign_keys = ON")
                statements = self.conn.execute(
                    "SELECT sql FROM undo_log WHERE grp = ? ORDER BY seq DESC", (row["grp"],)).fetchall()
                for statement in statements:
                    self.conn.execute(statement["sql"])
                self.conn.execute("DELETE FROM undo_log WHERE grp = ?", (row["grp"],))
                self.conn.execute("DELETE FROM undo_groups WHERE grp = ?", (row["grp"],))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                log.warning("Couldn't undo %s: the step is dropped", row["label"], exc_info=True)
                self.conn.execute("DELETE FROM undo_log WHERE grp = ?", (row["grp"],))
                self.conn.execute("DELETE FROM undo_groups WHERE grp = ?", (row["grp"],))
                raise UserError(CANT_UNDO) from None
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return row["label"]

    # ── meta ──

    def get_meta(self, key, default=None):
        with self.read() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key, value):
        """A flag of the app's own (onboarded, the last backup's fingerprint): no undo step's
        SQL depends on one, so it leaves undo as it is."""
        with self.write(keep_undo=True) as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, None if value is None else str(value)))

    def close(self):
        with self.lock:
            self.conn.close()
