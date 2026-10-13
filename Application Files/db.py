"""The SQLite database: schema, migrations, undo and daily backups.

One connection, shared by every thread behind a lock (pywebview runs each call from
the page on its own thread, and the Downloads watcher has its own). Writes go through
`Database.write()`, which opens a transaction; give it a label and the change can be
undone with ⌘Z.

Undo uses the classic SQLite recipe: triggers on every table record the SQL that
reverses each insert, update and delete into `undo_log`, grouped by action. Undoing
runs a group's statements backwards. Updates only record the columns that changed, so
undoing one action never reverts an unrelated change made later to the same row (an
import pairing a transfer, say). The log is cleared at launch: undo is for this session.

Money is stored in integer cents. Dates are ISO strings.
"""

import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path

log = logging.getLogger("inab")

SCHEMA_VERSION = 2
BACKUPS_KEPT = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- chequing | savings | cash | credit_card | line_of_credit
    last4 TEXT,                          -- card digits, shown next to the name
    anchor_date TEXT,                    -- a balance the user gave us, at the end of this day
    anchor_balance INTEGER,
    apr REAL,                            -- debt: yearly interest rate (%), for the payoff estimate
    planned_payment INTEGER,             -- debt: what you plan to pay each month
    sort INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- How imported files find their account: 'tangerine-card:1234', 'tangerine-bank:chequing', 'ofx:...'
CREATE TABLE IF NOT EXISTS account_sources (
    key TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE
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
    kind TEXT NOT NULL DEFAULT 'merchant', -- merchant | person | organization | bank
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
    archived_path TEXT
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
    hint TEXT NOT NULL DEFAULT '',       -- transfer | interest | income | cash | fee
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    is_split INTEGER NOT NULL DEFAULT 0,
    transfer_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    transfer_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    refund_of INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE SET NULL,
    similar_payee_id INTEGER REFERENCES payees(id) ON DELETE SET NULL,  -- "same as this payee?" to ask
    status TEXT NOT NULL DEFAULT 'review',   -- review | done
    reason TEXT NOT NULL DEFAULT '',     -- why it was filed where it was, shown in the inbox
    note TEXT NOT NULL DEFAULT '',
    fingerprint TEXT,                    -- importer.fingerprint(); never change how it's computed
    fitid TEXT,                          -- OFX's own transaction ID
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
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
    label TEXT NOT NULL
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
               "imports", "subscriptions", "transactions", "splits", "budget", "examples")


class Database:
    def __init__(self, path):
        self.path = Path(path)
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
        database is copied to Backups/ before any upgrade touches it."""
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
            # Next upgrade: `if version is not None and version < 3: ...`, then raise SCHEMA_VERSION.
            self.conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))
            if version is not None and version < SCHEMA_VERSION:
                log.info("Upgraded the database from version %d to %d", version, SCHEMA_VERSION)

    def _backup_before_upgrade(self, version):
        folder = self.path.parent / "Backups"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"budget-before-upgrade-v{version}-to-v{SCHEMA_VERSION}-{date.today().isoformat()}.db"
        if target.exists():
            return
        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        log.info("Saved a copy of the database before upgrading it: %s", target)

    def _install_undo_triggers(self):
        with self.lock:
            for table in UNDO_TABLES:
                columns = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]
                for suffix in ("ins", "upd", "del"):
                    self.conn.execute(f"DROP TRIGGER IF EXISTS undo_{table}_{suffix}")
                when = "WHEN (SELECT active FROM undo_state WHERE id = 1) = 1"
                grp = "(SELECT grp FROM undo_state WHERE id = 1)"
                self.conn.execute(
                    f"CREATE TRIGGER undo_{table}_ins AFTER INSERT ON {table} {when} BEGIN "
                    f"INSERT INTO undo_log (grp, sql) VALUES ({grp}, "
                    f"'DELETE FROM {table} WHERE rowid = ' || NEW.rowid); END")
                changed = " || ".join(
                    f"CASE WHEN OLD.{c} IS NOT NEW.{c} THEN ', {c} = ' || quote(OLD.{c}) ELSE '' END" for c in columns)
                self.conn.execute(
                    f"CREATE TRIGGER undo_{table}_upd AFTER UPDATE ON {table} {when} BEGIN "
                    f"INSERT INTO undo_log (grp, sql) VALUES ({grp}, "
                    f"'UPDATE {table} SET rowid = rowid' || {changed} || ' WHERE rowid = ' || OLD.rowid); END")
                values = " || ', ' || ".join(f"quote(OLD.{c})" for c in columns)
                self.conn.execute(
                    f"CREATE TRIGGER undo_{table}_del BEFORE DELETE ON {table} {when} BEGIN "
                    f"INSERT INTO undo_log (grp, sql) VALUES ({grp}, "
                    f"'INSERT INTO {table} (rowid, {', '.join(columns)}) VALUES (' || OLD.rowid || ', ' || {values} || ')'); END")

    # ── access ──

    @contextmanager
    def read(self):
        with self.lock:
            yield self.conn

    @contextmanager
    def write(self, label=None):
        """A transaction. Nested calls join the outer one. With a `label`, the whole
        change becomes one step of undo (the outermost label wins)."""
        with self.lock:
            outer = self._depth == 0
            self._depth += 1
            try:
                if outer:
                    self.conn.execute("BEGIN IMMEDIATE")
                    if label:
                        cur = self.conn.execute("INSERT INTO undo_groups (label) VALUES (?)", (label,))
                        self.conn.execute("UPDATE undo_state SET active = 1, grp = ?", (cur.lastrowid,))
                yield self.conn
                if outer:
                    if label:
                        self.conn.execute("UPDATE undo_state SET active = 0")
                        self.conn.execute(
                            "DELETE FROM undo_groups WHERE grp = (SELECT grp FROM undo_state) "
                            "AND NOT EXISTS (SELECT 1 FROM undo_log WHERE grp = undo_groups.grp)")
                        self.conn.execute(
                            "DELETE FROM undo_log WHERE grp NOT IN "
                            "(SELECT grp FROM undo_groups ORDER BY grp DESC LIMIT 100)")
                        self.conn.execute(
                            "DELETE FROM undo_groups WHERE grp NOT IN "
                            "(SELECT grp FROM undo_groups ORDER BY grp DESC LIMIT 100)")
                    self.conn.execute("COMMIT")
            except BaseException:
                if outer:
                    self.conn.execute("ROLLBACK")
                    self.conn.execute("UPDATE undo_state SET active = 0")
                raise
            finally:
                self._depth -= 1

    def undo_label(self):
        with self.read() as c:
            row = c.execute("SELECT label FROM undo_groups ORDER BY grp DESC LIMIT 1").fetchone()
            return row["label"] if row else None

    def undo(self):
        """Reverses the last labelled change. Returns its label, or None if there's nothing to undo."""
        with self.lock:
            row = self.conn.execute("SELECT grp, label FROM undo_groups ORDER BY grp DESC LIMIT 1").fetchone()
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
        with self.write() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, None if value is None else str(value)))

    # ── backups ──

    def backup_daily(self, folder):
        """Keeps one copy of the database per day, for the last BACKUPS_KEPT days."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"budget-{date.today().isoformat()}.db"
        if target.exists():
            return None
        tmp = target.with_suffix(".tmp")
        with self.lock:
            dest = sqlite3.connect(str(tmp))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        shutil.move(str(tmp), str(target))
        for old in sorted(folder.glob("budget-*.db"))[:-BACKUPS_KEPT]:
            old.unlink(missing_ok=True)
        log.info("Backed up the database to %s", target)
        return target

    def close(self):
        with self.lock:
            self.conn.close()
