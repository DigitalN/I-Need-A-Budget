"""Reading bank exports and adding their new transactions, never twice.

Formats (detected from the content, not the file name):
- OFX / QFX / QBO ("Microsoft Money", "Quicken" and "QuickBooks" downloads, which nearly
  every bank offers): every transaction has the bank's own ID (FITID), the file includes
  the balance, and it says which bank (<ORG>) and account it's for. The best kind of file.
  Tangerine calls every bank account SAVINGS in these, chequing included, so for Tangerine
  the file name (the account's nickname, as with its CSVs) decides which account it is.
  Its Mastercard comes as a bank statement too (CREDITLINE, the full card number): a
  credit line whose number is a card number is a card, from any bank.
- Tangerine credit card CSV: `Transaction date,Transaction,Name,Memo,Amount`. The file
  is named after the masked card number (four digits, x's, then the card's last four), which tells us
  which card. Memo holds the card's own category ("Rewards earned: 0.20 ~ Category: Restaurant").
- Tangerine bank account CSV: `Date,Transaction,Name,Memo,Amount`. Named after the
  account's nickname ("Chequing.CSV"). Name is cut at 32 characters; Memo has it in full.
- RBC CSV: `Account Type,Account Number,Transaction Date,Cheque Number,Description 1,
  Description 2,CAD$,USD$`, every account in one file, told apart by number.
- Any other bank's CSV, once the person has shown the app its columns (csvlayout.py).
  The layout is saved and found again by the file's header; which account a file is for
  is saved by its file name (dates taken out), and asked when the name is new.
- Actual Budget's transaction export: not imported as transactions, only learnt from
  (its categories and which payees went where). See `learn_from_actual()`.

Which account: from what the file says (see each format), else the one existing account
that already has most of the file's rows, else a new account (or, for a set-up CSV, a
question). A file never lands in an account just because it's the only one of its kind:
with more than one bank that would put one bank's file in another bank's account. Once the
budget is set up, the app asks before making a new account when you already have one it
could be (a replaced card has a new number): service.BudgetService._accounts_to_ask().

Duplicates: each row gets a fingerprint from its date, amount and description, plus
how many identical rows came before it in the same file. Exports overlap freely, so a
row whose fingerprint is already in the account is skipped, while two identical coffees
on the same day in one file are both kept. **Never change `fingerprint()`**: rows
already imported would stop matching and every overlapping file would import twice.
OFX rows are also matched on FITID. A bank's CSV and its Quicken file can describe the
same transaction differently, so a row is also the same as one from a *different kind*
of file in that account with the same date and amount (each matched once).
"""

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import banks
import csvlayout
import ledger
import subscriptions
import transfers
from learn import analyze
from payees import describe, transaction_hint
from util import parse_date, to_cents

log = logging.getLogger("inab")

CARD_HEADER = ["transaction date", "transaction", "name", "memo", "amount"]
BANK_HEADER = ["date", "transaction", "name", "memo", "amount"]
RBC_HEADER = ["account type", "account number", "transaction date", "cheque number", "description 1",
              "description 2", "cad$"]
ACTUAL_HEADER = ["account", "date", "payee", "notes", "category_group", "category", "amount"]
SUPPORTED_SUFFIXES = {".csv", ".ofx", ".qfx", ".qbo"}
BANK_FORMATS = ("ofx", "tangerine_card", "tangerine_bank", "rbc", "csv")


class ImportError_(Exception):
    """A file that isn't a bank export we can read."""


class NeedsSetup(ImportError_):
    """A CSV the app hasn't been shown how to read yet ('layout'), or whose account it
    doesn't know yet ('account'). The page asks, then calls the import again."""

    def __init__(self, what):
        super().__init__("This file needs setting up first." if what == "layout" else "Which account is this file for?")
        self.what = what


@dataclass
class Row:
    date: str
    amount: int
    name: str
    memo: str = ""
    bank_type: str = ""
    bank_category: str = ""
    fitid: str = None


@dataclass
class AccountHint:
    keys: list            # account_sources keys that identify it, most specific first
    kind: str
    name: str
    last4: str = None
    legacy_keys: list = field(default_factory=list)  # looked up after `keys`, never saved
    bank: str = None
    create: bool = True   # False: ask which account rather than make one


@dataclass
class Part:
    """One account's rows in a file (most files have one; RBC's CSV has every account)."""
    account: AccountHint
    rows: list
    balance: tuple = None  # (date, cents) when the file states one (OFX)

    @property
    def first_date(self):
        return min((r.date for r in self.rows), default=None)

    @property
    def last_date(self):
        return max((r.date for r in self.rows), default=None)


@dataclass
class Parsed:
    format: str           # one of BANK_FORMATS, or actual
    parts: list = field(default_factory=list)
    actual_rows: list = field(default_factory=list)

    @property
    def rows(self):
        return [r for part in self.parts for r in part.rows]

    @property
    def first_date(self):
        return min((r.date for r in self.rows), default=None)

    @property
    def last_date(self):
        return max((r.date for r in self.rows), default=None)


# ── reading files ────────────────────────────────────────────

def read_text(path):
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_("The file isn't text.")


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def base_name(filename):
    """'Chequing-2.CSV' / 'Chequing (1).csv' → 'Chequing': browsers number repeat downloads."""
    stem = Path(filename).stem.strip()
    stem = re.sub(r"(\s*\(\d+\)|-\d{1,3}|\s+\d{1,3})$", "", stem)
    return stem.strip()


def detect(text):
    """The file's format (see the module docstring), or None. 'csv' is a CSV that looks like
    bank transactions but isn't a layout the app knows by itself."""
    head = text.lstrip()[:400]
    if head.upper().startswith("OFXHEADER") or "<OFX>" in text[:5000].upper():
        return "ofx"
    try:
        first = next(csv.reader(io.StringIO(text)))
    except StopIteration:
        return None
    header = [h.strip().lower() for h in first]
    if header[:5] == CARD_HEADER:
        return "tangerine_card"
    if header[:5] == BANK_HEADER:
        return "tangerine_bank"
    if header[:7] == RBC_HEADER:
        return "rbc"
    if header[:7] == ACTUAL_HEADER:
        return "actual"
    if csvlayout.sniff(text):
        return "csv"
    return None


def parse(path, conn=None):
    """Reads one file. Raises ImportError_ if it isn't a format we know, NeedsSetup for a
    CSV whose layout hasn't been set up (looked up through `conn`)."""
    path = Path(path)
    text = read_text(path)
    fmt = detect(text)
    if fmt == "tangerine_card":
        return _parse_tangerine(text, path.name, card=True)
    if fmt == "tangerine_bank":
        return _parse_tangerine(text, path.name, card=False)
    if fmt == "ofx":
        return _parse_ofx(text, path.name)
    if fmt == "rbc":
        return _parse_rbc(text)
    if fmt == "csv":
        return _parse_csv(text, path.name, conn)
    if fmt == "actual":
        return _parse_actual(text)
    raise ImportError_("This doesn't look like a bank file: no column of dates and amounts.")


def _parse_tangerine(text, filename, card):
    reader = csv.reader(io.StringIO(text))
    next(reader)
    rows = []
    for line in reader:
        if len(line) < 5 or not any(cell.strip() for cell in line):
            continue
        date_text, kind, name, memo, amount = (cell.strip() for cell in line[:5])
        try:
            row = Row(parse_date(date_text), to_cents(amount), name, memo, kind)
        except ValueError as e:
            raise ImportError_(f"Couldn't read a row ({e}).") from None
        m = re.search(r"Category:\s*(.+?)\s*$", memo)
        if card and m:
            row.bank_category = m.group(1)
        rows.append(row)
    stem = base_name(filename)
    if card:
        digits = re.findall(r"\d{4}", stem)
        last4 = digits[-1] if digits else None
        keys = [f"tangerine-card:{last4}"] if last4 else []
        hint = AccountHint(keys + ["tangerine-card"], "credit_card", "Tangerine Mastercard", last4, bank="Tangerine")
    else:
        key, kind, name = _tangerine_bank_account(filename)
        hint = AccountHint([key], kind, name, bank="Tangerine")
    return Parsed("tangerine_card" if card else "tangerine_bank", [Part(hint, rows)])


def account_number_key(bank, last4):
    """The account_sources key for an account number, the same whichever kind of file
    from that bank it came in."""
    return f"number:{banks.slug(bank) or 'unknown'}:{last4}"


def _tangerine_bank_account(filename):
    """Tangerine names bank account downloads after the account's nickname ("Chequing.CSV",
    "Chequing.QFX"): (account_sources key, kind, name)."""
    stem = base_name(filename)
    lower = stem.lower()
    kind = "savings" if "saving" in lower else "line_of_credit" if "credit" in lower else "chequing"
    return f"tangerine-bank:{lower}", kind, stem.title() if stem.islower() or stem.isupper() else stem


def _kind_from_label(label):
    """An account type as a bank writes it ("MasterCard", "Line of Credit", "Savings") → kind."""
    t = str(label).lower()
    if re.search(r"line of credit|\bloc\b|credit line|creditline", t):
        return "line_of_credit"
    if re.search(r"visa|master ?card|amex|american express|credit|card", t):
        return "credit_card"
    if "saving" in t or "moneymrkt" in t:
        return "savings"
    return "chequing"


def _parse_rbc(text):
    """RBC's CSV: every account in one file. Description 1 is often what happened
    ("e-Transfer sent") and Description 2 who with; the name is both."""
    reader = csv.reader(io.StringIO(text))
    next(reader)
    parts = {}
    for line in reader:
        if len(line) < 7 or not any(cell.strip() for cell in line):
            continue
        acct_type, number, date_text, _cheque, d1, d2, cad = (cell.strip() for cell in line[:7])
        usd = line[7].strip() if len(line) > 7 else ""
        try:
            row = Row(parse_date(date_text), to_cents(cad or usd), " ".join(p for p in (d1, d2) if p))
        except ValueError as e:
            raise ImportError_(f"Couldn't read a row ({e}).") from None
        digits = re.sub(r"\D", "", number)
        last4 = digits[-4:] if len(digits) >= 4 else None
        if (acct_type.lower(), last4) not in parts:
            kind = _kind_from_label(acct_type)
            label = {"chequing": "Chequing", "savings": "Savings", "line_of_credit": "Line of Credit"}.get(kind) or \
                ("Mastercard" if re.search(r"master ?card", acct_type, re.I) else acct_type.title() or "Card")
            keys = [account_number_key("RBC", last4)] if last4 else []
            parts[(acct_type.lower(), last4)] = Part(AccountHint(keys, kind, f"RBC {label}", last4, bank="RBC"), [])
        parts[(acct_type.lower(), last4)].rows.append(row)
    return Parsed("rbc", list(parts.values()))


def _ofx_value(block, tag):
    m = re.search(rf"<{tag}>\s*([^<\r\n]*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_ofx(text, filename):
    bank = _ofx_bank(text)
    statements = [m.group(1) for m in re.finditer(
        r"<(?:CC)?STMTRS>(.*?)(?:</(?:CC)?STMTRS>|(?=<(?:CC)?STMTRS>)|$)", text, re.IGNORECASE | re.DOTALL)]
    parts = [_ofx_statement(block, bank, filename if len(statements) == 1 else None)
             for block in statements or [text]]
    return Parsed("ofx", parts)


def _ofx_statement(text, bank, filename):
    """One account's statement in an OFX file. `filename` only when it's the file's only
    statement (Tangerine's file name is the account's nickname)."""
    rows = []
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", text,
                            re.IGNORECASE | re.DOTALL):
        posted, amount = _ofx_value(block, "DTPOSTED"), _ofx_value(block, "TRNAMT")
        if not posted or amount is None:
            continue
        name = _ofx_value(block, "NAME") or _ofx_value(block, "PAYEE") or ""
        memo = _ofx_value(block, "MEMO") or ""
        try:
            row = Row(parse_date(posted), to_cents(amount), _unescape(name), _unescape(memo),
                      _ofx_value(block, "TRNTYPE") or "", fitid=_ofx_value(block, "FITID"))
        except ValueError as e:
            raise ImportError_(f"Couldn't read a transaction ({e}).") from None
        m = re.search(r"Category:\s*(.+?)\s*$", row.memo)
        if m:
            row.bank_category = m.group(1)
        rows.append(row)
    acct_block = re.search(r"<(?:CC|BANK)ACCTFROM>(.*?)</(?:CC|BANK)ACCTFROM>", text, re.IGNORECASE | re.DOTALL)
    acct_id = _ofx_value(acct_block.group(1) if acct_block else text, "ACCTID") or ""
    acct_type = ((_ofx_value(acct_block.group(1), "ACCTTYPE") if acct_block else None) or "").upper()
    digits = re.sub(r"\D", "", acct_id)
    last4 = digits[-4:] if len(digits) >= 4 else None
    # Tangerine sends its Mastercard as a bank statement: a credit line whose number is a
    # card number. Any bank could, so it's the card number that decides.
    is_card = re.search(r"<CCACCTFROM>", text, re.IGNORECASE) is not None or \
        (acct_type == "CREDITLINE" and _is_card_number(digits))
    tangerine = bank == "Tangerine"
    number_keys = [account_number_key(bank, last4)] if last4 else []
    if is_card:
        keys = number_keys + (([f"tangerine-card:{last4}"] if last4 else []) + ["tangerine-card"] if tangerine else [])
        hint = AccountHint(keys, "credit_card", "Tangerine Mastercard" if tangerine else f"{bank} card" if bank else "Credit card",
                           last4, bank=bank)
    else:
        kind = "savings" if acct_type in ("SAVINGS", "MONEYMRKT") else \
            "line_of_credit" if acct_type == "CREDITLINE" else "chequing"
        legacy = [f"ofx:{kind}:{last4}"] if last4 else []  # how version 1.0 keyed OFX accounts
        if tangerine and filename:
            # The file name only tells chequing from savings (both say SAVINGS).
            key, named, name = _tangerine_bank_account(filename)
            kind = kind if kind == "line_of_credit" else named
            hint = AccountHint(number_keys + [key], kind, name, last4, legacy, bank=bank)
        else:
            label = kind.replace("_", " ").title()
            hint = AccountHint(number_keys, kind, f"{bank} {label}" if bank else label, last4, legacy, bank=bank)
    balance = None
    ledger_block = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|$)", text, re.IGNORECASE | re.DOTALL)
    if ledger_block:
        amount, as_of = _ofx_value(ledger_block.group(1), "BALAMT"), _ofx_value(ledger_block.group(1), "DTASOF")
        try:
            if amount is not None and as_of:
                balance = (parse_date(as_of), to_cents(amount))
        except ValueError:
            balance = None
    return Part(hint, rows, balance)


def _is_card_number(digits):
    """A whole card number (13 to 19 digits passing the Luhn check), not an account number."""
    if not 13 <= len(digits) <= 19:
        return False
    total = sum(int(c) if i % 2 == 0 else sum(divmod(int(c) * 2, 10)) for i, c in enumerate(reversed(digits)))
    return total % 10 == 0


def _ofx_bank(text):
    """The bank an OFX file comes from (its <ORG>), or None if it doesn't say."""
    org = (_ofx_value(text, "ORG") or "").strip()
    if "tangerine" in org.lower() or (_ofx_value(text, "BANKID") or "").lstrip("0") == "614":
        return "Tangerine"  # 614 is Tangerine's institution number
    return banks.canonical(_unescape(org)) or None


def _unescape(text):
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&apos;", "'").replace("&quot;", '"'))


def csv_name_key(filename):
    """What a set-up CSV's file name says about its account, dates left out:
    'Visa transactions 2026-10-13 (1).csv' → 'visa transactions'."""
    stem = base_name(filename).lower()
    stem = re.sub(r"\d{4}[-_.]?\d{2}[-_.]?\d{2}|\d{1,2}[-_.]\d{1,2}[-_.]\d{2,4}", " ", stem)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", stem).split()) or "file"


def csv_account_key(layout_id, filename):
    return f"csv:{layout_id}:{csv_name_key(filename)}"


def saved_layout(conn, sniffed):
    """The layout saved for files like this one, or None."""
    row = conn.execute("SELECT * FROM csv_layouts WHERE signature = ?", (csvlayout.signature(sniffed),)).fetchone()
    if row is None:
        return None
    layout = {k: row[k] for k in csvlayout.LAYOUT_KEYS if k != "desc_cols"}
    layout.update(id=row["id"], has_header=bool(row["has_header"]), negate=bool(row["negate"]),
                  desc_cols=[int(c) for c in row["desc_cols"].split(",") if c != ""])
    return layout


def save_layout(conn, sniffed, layout):
    """Saves (or replaces) the layout for files like this one. Returns its id."""
    layout = csvlayout.clean_layout(layout, sniffed["width"])
    values = (int(layout["has_header"]), layout["date_col"], layout["date_order"],
              ",".join(str(c) for c in layout["desc_cols"]), layout["amount_col"], layout["out_col"],
              layout["in_col"], int(layout["negate"]))
    signature = csvlayout.signature(sniffed)
    conn.execute("INSERT INTO csv_layouts (signature, has_header, date_col, date_order, desc_cols, amount_col, out_col, "
                 "in_col, negate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(signature) DO UPDATE SET "
                 "has_header = excluded.has_header, date_col = excluded.date_col, date_order = excluded.date_order, "
                 "desc_cols = excluded.desc_cols, amount_col = excluded.amount_col, out_col = excluded.out_col, "
                 "in_col = excluded.in_col, negate = excluded.negate", (signature, *values))
    return conn.execute("SELECT id FROM csv_layouts WHERE signature = ?", (signature,)).fetchone()["id"]


def _parse_csv(text, filename, conn):
    sniffed = csvlayout.sniff(text)
    layout = saved_layout(conn, sniffed) if conn is not None else None
    if layout is None:
        raise NeedsSetup("layout")
    try:
        rows = [Row(day, cents, description) for day, cents, description in csvlayout.read(text, layout)]
    except csvlayout.LayoutError as e:
        raise ImportError_(f"{e}. Set the file up again from the Inbox.") from None
    hint = AccountHint([csv_account_key(layout["id"], filename)], "chequing", base_name(filename), create=False)
    return Parsed("csv", [Part(hint, rows)])


def _parse_actual(text):
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for r in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        try:
            out.append({"date": parse_date(r["date"]), "amount": to_cents(r["amount"]), "payee": r.get("payee", ""),
                        "notes": r.get("notes", ""), "group": r.get("category_group", ""),
                        "category": r.get("category", ""), "account": r.get("account", "")})
        except (ValueError, KeyError):
            continue
    return Parsed("actual", actual_rows=out)


# ── fingerprints ─────────────────────────────────────────────

def fingerprint(date, amount, name, occurrence):
    """Stable identity of an imported row. NEVER change this (see the module docstring)."""
    norm = re.sub(r"[^A-Z0-9]", "", str(name).upper())
    return hashlib.sha1(f"{date}|{amount}|{norm}|{occurrence}".encode()).hexdigest()[:20]


def fingerprints(rows):
    seen = {}
    out = []
    for r in rows:
        norm = re.sub(r"[^A-Z0-9]", "", r.name.upper())
        k = (r.date, r.amount, norm)
        out.append(fingerprint(r.date, r.amount, r.name, seen.get(k, 0)))
        seen[k] = seen.get(k, 0) + 1
    return out


# ── adding to the database ───────────────────────────────────

@dataclass
class Result:
    filename: str
    format: str
    account_id: int = None
    account_name: str = ""
    new_account: bool = False
    total: int = 0
    added: int = 0
    duplicates: int = 0
    filed: int = 0            # filed without asking
    to_review: int = 0
    first_date: str = None
    last_date: str = None
    balance_note: str = ""
    import_id: int = None

    def as_dict(self):
        return dict(self.__dict__)


def find_account(conn, hint, rows=(), fps=()):
    """The existing account a file's rows belong to, or None: by what the file says
    (`hint.keys`), else the one account that already has the file's rows."""
    return ledger.account_by_key(conn, hint.keys) or ledger.account_by_key(conn, hint.legacy_keys) \
        or account_with_rows(conn, hint.kind, rows, fps)


def find_or_create_account(conn, hint, rows=(), fps=()):
    """The account a file's rows belong to (see find_account), else a new one (raises
    NeedsSetup instead when `hint.create` is off). A found account learns the file's keys,
    so the next file from it is recognised straight away."""
    account_id = find_account(conn, hint, rows, fps)
    if account_id:
        learn_keys(conn, hint, account_id)
        return account_id, False
    if not hint.create:
        raise NeedsSetup("account")
    return create_account_for(conn, hint), True


def learn_keys(conn, hint, account_id, chosen=False):
    """The account remembers what this file calls it (and its bank, if it had none). A key
    another account already has stays with it, unless the person chose this account."""
    for key in hint.keys:
        conn.execute("INSERT INTO account_sources (key, account_id) VALUES (?, ?) ON CONFLICT(key) DO " +
                     ("UPDATE SET account_id = excluded.account_id" if chosen else "NOTHING"), (key, account_id))
    if hint.bank:
        conn.execute("UPDATE accounts SET bank = ? WHERE id = ? AND bank IS NULL", (hint.bank, account_id))


def suggested_name(conn, hint):
    """The name a new account for this file gets: what the file calls it, made unique."""
    name = hint.name
    if conn.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        name = f"{name} {hint.last4}" if hint.last4 else f"{name} 2"
    return name


def create_account_for(conn, hint, name=None):
    """A new account for a file's rows, knowing the file's keys."""
    name = name or suggested_name(conn, hint)
    keys = [k for k in hint.keys if not conn.execute("SELECT 1 FROM account_sources WHERE key = ?", (k,)).fetchone()]
    account_id = ledger.create_account(conn, name, hint.kind, keys=keys, last4=hint.last4, bank=hint.bank)
    log.info("Created account %s (%s)", name, hint.kind)
    return account_id


def account_with_rows(conn, kind, rows, fps):
    """The one existing account that already has nearly all of these rows (by fingerprint),
    or None. Rows in another account with the same date, amount and description are the
    same transactions, so it's the same account, whatever the file calls it."""
    if not fps:
        return None
    found = {}
    for i in range(0, len(fps), 500):
        chunk = list(fps[i:i + 500])
        for r in conn.execute(
                f"SELECT account_id, fingerprint FROM transactions WHERE fingerprint IN ({','.join('?' * len(chunk))})",
                chunk):
            found.setdefault(r["account_id"], set()).add(r["fingerprint"])
    matches = []
    for account_id, shared in found.items():
        acct = conn.execute("SELECT a.kind, MIN(t.date) AS first, MAX(t.date) AS last FROM accounts a "
                            "JOIN transactions t ON t.account_id = a.id WHERE a.id = ?", (account_id,)).fetchone()
        if ledger.is_debt(acct["kind"]) != ledger.is_debt(kind):
            continue
        overlapping = [fp for row, fp in zip(rows, fps) if acct["first"] <= row.date <= acct["last"]]
        hits = sum(1 for fp in overlapping if fp in shared)
        if hits >= min(3, len(rows)) and hits >= 0.8 * len(overlapping):
            matches.append(account_id)
    return matches[0] if len(matches) == 1 else None


def import_parsed(conn, parsed, filename, sha, archived_path=None, accounts=None):
    """Adds the new rows of a parsed bank file and files what it can, one Result per
    account in the file. `accounts`: {part index: account id} for parts whose account the
    person chose. Call inside db.write()."""
    if parsed.format == "actual":
        raise ImportError_("Actual Budget exports are learnt from, not imported.")
    accounts = accounts or {}
    return [_import_part(conn, parsed.format, part, filename, sha, archived_path, accounts.get(i))
            for i, part in enumerate(parsed.parts)]


def _import_part(conn, fmt, part, filename, sha, archived_path, account_id=None):
    result = Result(filename, fmt, total=len(part.rows), first_date=part.first_date, last_date=part.last_date)
    fps = fingerprints(part.rows)
    if account_id is None:
        account_id, created = find_or_create_account(conn, part.account, part.rows, fps)
    else:
        learn_keys(conn, part.account, account_id, chosen=True)
        created = False
    acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    result.account_id, result.account_name, result.new_account = account_id, acct["name"], created
    import_id = conn.execute(
        "INSERT INTO imports (filename, sha256, account_id, format, rows_total, first_date, last_date, archived_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (filename, sha, account_id, fmt, len(part.rows), part.first_date, part.last_date, archived_path)).lastrowid
    result.import_id = import_id

    # Rows from a different kind of file (this bank's CSV, when this is its Quicken file):
    # the same date and amount is the same transaction, whatever each file called it.
    others = {}
    for r in conn.execute("SELECT t.id, t.date, t.amount FROM transactions t JOIN imports i ON i.id = t.import_id "
                          "WHERE t.account_id = ? AND i.format != ? AND t.date BETWEEN ? AND ? ORDER BY t.id",
                          (account_id, fmt, part.first_date or "", part.last_date or "")):
        others.setdefault((r["date"], r["amount"]), []).append(r["id"])

    new_ids = []
    for row, fp in zip(part.rows, fps):
        same = others.get((row.date, row.amount), [])
        existing = conn.execute("SELECT id, fitid FROM transactions WHERE account_id = ? AND fingerprint = ?",
                                (account_id, fp)).fetchone()
        if existing is None and row.fitid:
            existing = conn.execute("SELECT id, fitid FROM transactions WHERE account_id = ? AND fitid = ?",
                                    (account_id, row.fitid)).fetchone()
        if existing is None and same:
            existing = conn.execute("SELECT id, fitid FROM transactions WHERE id = ?", (same[0],)).fetchone()
        if existing is not None:
            if existing["id"] in same:
                same.remove(existing["id"])
            if row.fitid and not existing["fitid"] and not conn.execute(
                    "SELECT 1 FROM transactions WHERE account_id = ? AND fitid = ?", (account_id, row.fitid)).fetchone():
                conn.execute("UPDATE transactions SET fitid = ? WHERE id = ?", (row.fitid, existing["id"]))
            result.duplicates += 1
            continue
        desc = describe(row.name, row.memo, acct["kind"])
        payee_id, note, similar = ledger.resolve_payee(conn, desc)
        new_ids.append(conn.execute(
            "INSERT INTO transactions (account_id, date, amount, payee_id, raw_name, raw_memo, bank_type, "
            "bank_category, hint, fingerprint, fitid, import_id, reason, similar_payee_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, row.date, row.amount, payee_id, row.name, row.memo, row.bank_type, row.bank_category,
             transaction_hint(desc), fp, row.fitid, import_id, note, similar)).lastrowid)
    result.added = len(new_ids)
    conn.execute("UPDATE imports SET rows_new = ? WHERE id = ?", (result.added, import_id))

    filed, review = file_transactions(conn, new_ids)
    result.filed, result.to_review = filed, review

    if part.balance:
        # The bank's own balance beats one typed in by hand: a balance typed during the day
        # misses what the bank adds later that day, and every balance is worked out from
        # this one. So a file's balance replaces any as old as it, or older.
        as_of, balance = part.balance
        ours = ledger.account_balance(conn, account_id, as_of)
        if acct["anchor_date"] is None or as_of >= acct["anchor_date"]:
            conn.execute("UPDATE accounts SET anchor_date = ?, anchor_balance = ? WHERE id = ?",
                         (as_of, balance, account_id))
            if ours != balance:
                result.balance_note = "Balance taken from the file"
                if ours is not None:
                    log.info("%s: balance on %s corrected from the file by %+d cents", acct["name"], as_of,
                             balance - ours)
        elif ours is not None and ours != balance:
            result.balance_note = (f"The file says the balance was {balance / 100:,.2f} on {as_of}, "
                                   f"but the app has {ours / 100:,.2f}")
    return result


def file_transactions(conn, ids):
    """For each transaction, oldest first: is it a transfer? A charge for a tracked
    subscription (filed in that subscription's category)? Otherwise refunds and learnt
    categories. What's left for review has no category until it's categorized
    (docs/adr/0002). Returns (filed without asking, left for review)."""
    filed = review = 0
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(ids))}) ORDER BY date, id", ids).fetchall() \
        if ids else []
    for txn in rows:
        txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn["id"],)).fetchone()
        if transfers.pair(conn, txn):
            filed += 1
            continue
        sub, alert = subscriptions.link_transaction(conn, txn["id"])
        if alert:
            conn.execute("UPDATE transactions SET category_id = NULL, status = 'review', reason = ? WHERE id = ?",
                         (alert, txn["id"]))
            review += 1
            continue
        if sub is not None and sub["category_id"] and sub["status"] == "active" and not txn["is_split"]:
            conn.execute("UPDATE transactions SET category_id = ?, status = 'done', reason = ? WHERE id = ?",
                         (sub["category_id"], f"{sub['name']} subscription", txn["id"]))
            filed += 1
            continue
        question = subscriptions.unmatched_question(conn, txn) if sub is None else None
        if question:
            # A subscription payee at a new price: always ask (price rise? new? one-off?).
            conn.execute("UPDATE transactions SET category_id = NULL, status = 'review', reason = ? WHERE id = ?",
                         (question, txn["id"]))
            review += 1
            continue
        matched = re.match(r"Matched to .*? \(similar name\)", txn["reason"] or "")
        suggestion = analyze(conn, txn)
        reason = suggestion.reason
        status = "done" if suggestion.sure and suggestion.options and not txn["similar_payee_id"] else "review"
        if matched:
            reason = f"{matched.group(0)}. {reason}"
        category = suggestion.options[0] if status == "done" else None
        conn.execute("UPDATE transactions SET category_id = ?, status = ?, reason = ?, refund_of = ? WHERE id = ?",
                     (category, status, reason, suggestion.refund_of if suggestion.sure else None, txn["id"]))
        if status == "done":
            filed += 1
        else:
            review += 1
    return filed, review


def learn_from_actual(conn, parsed):
    """Takes categories and payee → category examples from an Actual Budget export.

    Categories are matched by name, including names they used to have (so renaming
    "Eating Out" to "Food" here still matches Actual's "Eating Out"); missing ones are
    created. Returns {'examples': n, 'categories_created': [...]}."""
    created, examples = [], 0
    income_id = ledger.income_category(conn)
    income_group = conn.execute("SELECT group_id FROM categories WHERE id = ?", (income_id,)).fetchone()["group_id"]
    for r in parsed.actual_rows:
        group_name, cat_name = r["group"], r["category"]
        if not cat_name or cat_name.lower() == "starting balances":
            continue
        category_id = ledger.find_category_by_name(conn, cat_name)
        if category_id is None:
            if group_name.lower() == "income":
                group_id = income_group
            else:
                row = conn.execute("SELECT id FROM category_groups WHERE name = ? COLLATE NOCASE", (group_name,)).fetchone()
                group_id = row["id"] if row else conn.execute(
                    "INSERT INTO category_groups (name, sort) VALUES (?, (SELECT COALESCE(MAX(sort),0)+1 "
                    "FROM category_groups WHERE role != 'income'))", (group_name or "Imported",)).lastrowid
            sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM categories WHERE group_id = ?",
                                (group_id,)).fetchone()[0]
            category_id = conn.execute("INSERT INTO categories (group_id, name, sort) VALUES (?, ?, ?)",
                                       (group_id, cat_name, sort)).lastrowid
            created.append(cat_name)
        raw = _actual_description(r["notes"]) or r["payee"]
        desc = describe(raw, "", "credit_card" if "card" in r["account"].lower() or "master" in r["account"].lower()
                        or "visa" in r["account"].lower() else "chequing")
        if desc.hint == "transfer":
            continue
        payee_id, _, _ = ledger.resolve_payee(conn, desc)
        conn.execute("INSERT INTO examples (payee_id, category_id, amount, date, source) VALUES (?, ?, ?, ?, 'actual')",
                     (payee_id, category_id, r["amount"], r["date"]))
        examples += 1
    return {"examples": examples, "categories_created": created}


def _actual_description(notes):
    """Actual's Notes hold the bank's text. Rows that came through SimpleFIN look like
    "2%  SQ *CORNER BAKERY CALGARY AB Fast Food (Quick Service)": drop the reward
    rate in front and the aggregator's category after the province."""
    text = re.sub(r"^\d+(\.\d+)?%\s+", "", str(notes or "").strip())
    tokens = text.split()
    for i in range(len(tokens) - 1, 0, -1):
        if tokens[i] in ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT") \
                and i < len(tokens) - 1:
            return " ".join(tokens[:i + 1])
    return text
