# SPDX-License-Identifier: GPL-3.0-only
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
  is saved by its file name (dates taken out), and asked when the name is new, or when the
  file doesn't look like that account (some banks name every download the same: csv_fit).
- Actual Budget's transaction export: not imported as transactions, only learnt from
  (its categories and which payees went where). See `learn_from_actual()`.

Which account: from what the file says (see each format; an account number counts by its whole
number first, then its last four digits, and only for an account of the same sort (card or bank
account) at the same bank: two accounts can end in the same four digits, and a file that doesn't
name its bank never joins an account at a named one by number alone), else the one existing account
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
OFX rows are also matched on FITID, as a hint only: banks repeat them (one per day, 1, 2, 3… in
every download), so a FITID counts for the same amount within FITID_DAYS days, one that repeats
within a file counts for nothing, and one already on another row of the account isn't stored
again. A bank's CSV and its Quicken file can describe the
same transaction differently, so a row is also the same as one from a *different kind*
of file in that account (a CSV set up again counts as another kind: imports.format is
`csv:<layout id>:<its shape>`; plain `csv`, from before that, is the same kind as any of
them) with the same amount and a date up to OTHER_KIND_DAYS either side, each matched once.
It's matched over the whole file, not row by row: every row's exact twin first (fingerprint,
then FITID), then the rest by nearest date first, so a new row never takes the place of an old
one's twin (ledger.pair_nearest). Only where one of the two rows falls within the other's file
dates, though: the rows at the edges of two downloads (one ends Oct 15, the next starts Oct 16, or
they overlap by a day) are different transactions. Accounts holding the same transactions that way are
flagged and merged like those with shared fingerprints (ledger.cross_kind_pairs). So is one
you added by hand (no fingerprint, ID or import) with the same amount within MANUAL_DAYS days:
it takes the bank's date, text and IDs and keeps what you decided.

New rows are only added here: each Result's `new_ids` then arrive in the inbox
(inbox.Inbox.arrive), which categorizes what it can.
"""

import csv
import hashlib
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

import banks
import csvlayout
import ledger
from payees import ADJUSTMENT, describe, transaction_hint
from util import day_before, money, parse_date, short_day, to_cents

log = logging.getLogger("inab")

CARD_HEADER = ["transaction date", "transaction", "name", "memo", "amount"]
BANK_HEADER = ["date", "transaction", "name", "memo", "amount"]
RBC_HEADER = ["account type", "account number", "transaction date", "cheque number", "description 1",
              "description 2", "cad$"]
ACTUAL_HEADER = ["account", "date", "payee", "notes", "category_group", "category", "amount"]
FITID_DAYS = 7  # a bank's transaction ID matches a row of the same amount this many days either side
MANUAL_DAYS = 3  # a transaction added by hand is the bank's row of the same amount this many days either side
SUPPORTED_SUFFIXES = {".csv", ".ofx", ".qfx", ".qbo"}
BANK_FORMATS = ("ofx", "tangerine_card", "tangerine_bank", "rbc", "csv")


class ImportError_(Exception):
    """A file that isn't a bank export we can read."""


class NeedsSetup(ImportError_):
    """A CSV the app hasn't been shown how to read yet ('layout'), or whose account it
    doesn't know yet ('account'). The page asks, then calls the import again."""

    def __init__(self, what, message=None):
        super().__init__(message or ("This file needs setting up first." if what == "layout"
                                     else "Which account is this file for?"))
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
    account_id: int = None  # the account the file is known to be for (found by looking at the file)
    number_key: str = None  # the key made of the account number's last four digits (see find_account)
    full_key: str = None    # the key made of the whole number, when the file has it: tried first


@dataclass
class Part:
    """One account's rows in a file (most files have one; RBC's CSV has every account)."""
    account: AccountHint
    rows: list
    balance: tuple = None  # (date, cents) when the file states one (OFX), as the file writes it
    notes: list = field(default_factory=list)  # what was changed about the file to read it (shown with the import)
    card_file: bool = False  # a card or credit line's file: its signs are settled when the account is known (_flip, _positive_balance)
    flip_guess: bool = None  # from the rows alone: do they write purchases as positive? None: too few purchases to tell

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
    kind: str = None      # the kind of file, to tell it from another kind's: `format`, or a CSV's layout

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
    """The file's text. UTF-16 (Excel's "Unicode text") is told by its byte order mark, and a
    lone carriage return (Excel's "CSV (Macintosh)") ends a line like any other."""
    data = Path(path).read_bytes()
    encodings = ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1")
    for encoding in encodings:
        try:
            return re.sub(r"\r(?!\n)", "\n", data.decode(encoding))
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
    except (StopIteration, csv.Error):  # empty, or not a table the csv module can read
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
    """Reads one file. Raises ImportError_ if it isn't a format we know or can read,
    NeedsSetup for a CSV whose layout hasn't been set up, or no longer fits (looked up
    through `conn`)."""
    try:
        return _parse(Path(path), conn)
    except csv.Error as e:  # a cell over the size limit, a stray quote...
        raise ImportError_(f"This file can't be read as a table of transactions ({e}).") from None


def _parse(path, conn):
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
    """The account_sources key for an account number's last four digits, the same whichever
    kind of file from that bank it came in."""
    return f"number:{banks.slug(bank) or 'unknown'}:{last4}"


def full_number_key(bank, digits):
    """The key for the whole account number (more than its last four digits), which keeps two
    accounts that end in the same four digits apart."""
    return f"number-full:{banks.slug(bank) or 'unknown'}:{digits}"


def _number_keys(bank, digits):
    """(last-four key, whole-number key or None, the last four digits) for an account number."""
    last4 = digits[-4:] if len(digits) >= 4 else None
    return (account_number_key(bank, last4) if last4 else None,
            full_number_key(bank, digits) if len(digits) > 4 else None, last4)


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
        number_key, full_key, last4 = _number_keys("RBC", digits)
        if (acct_type.lower(), digits) not in parts:
            kind = _kind_from_label(acct_type)
            label = {"chequing": "Chequing", "savings": "Savings", "line_of_credit": "Line of Credit"}.get(kind) or \
                ("Mastercard" if re.search(r"master ?card", acct_type, re.I) else acct_type.title() or "Card")
            keys = [k for k in (full_key, number_key) if k]
            parts[(acct_type.lower(), digits)] = Part(
                AccountHint(keys, kind, f"RBC {label}", last4, bank="RBC", number_key=number_key, full_key=full_key), [])
        parts[(acct_type.lower(), digits)].rows.append(row)
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
    number_key, full_key, last4 = _number_keys(bank, digits)
    # Tangerine sends its Mastercard as a bank statement: a credit line whose number is a
    # card number. Any bank could, so it's the card number that decides.
    is_card = re.search(r"<CCACCTFROM>", text, re.IGNORECASE) is not None or \
        (acct_type == "CREDITLINE" and _is_card_number(digits))
    tangerine = bank == "Tangerine"
    number_keys = [k for k in (full_key, number_key) if k]
    numbers = {"number_key": number_key, "full_key": full_key}
    if is_card:
        keys = number_keys + (([f"tangerine-card:{last4}"] if last4 else []) + ["tangerine-card"] if tangerine else [])
        hint = AccountHint(keys, "credit_card", "Tangerine Mastercard" if tangerine else f"{bank} card" if bank else "Credit card",
                           last4, bank=bank, **numbers)
    else:
        # Tangerine says SAVINGS for every bank account, chequing too: ACCTTYPE isn't trusted
        # for it (the file name decides below; without one it's a bank account, told by its number).
        kind = "savings" if acct_type in ("SAVINGS", "MONEYMRKT") and not tangerine else \
            "line_of_credit" if acct_type == "CREDITLINE" else "chequing"
        legacy = [f"ofx:{kind}:{last4}"] if last4 else []  # how version 1.0 keyed OFX accounts
        if tangerine and filename:
            # The file name only tells chequing from savings (both say SAVINGS).
            key, named, name = _tangerine_bank_account(filename)
            kind = kind if kind == "line_of_credit" else named
            hint = AccountHint(number_keys + [key], kind, name, last4, legacy, bank=bank, **numbers)
        else:
            # Tangerine calls chequing and savings alike, and without a file name there's nothing to tell them by.
            label = "account" if tangerine and kind == "chequing" else kind.replace("_", " ").title()
            hint = AccountHint(number_keys, kind, f"{bank} {label}" if bank else label, last4, legacy, bank=bank, **numbers)
    balance = None
    ledger_block = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|$)", text, re.IGNORECASE | re.DOTALL)
    if ledger_block:
        amount, as_of = _ofx_value(ledger_block.group(1), "BALAMT"), _ofx_value(ledger_block.group(1), "DTASOF")
        try:
            if amount is not None and as_of:
                balance = (parse_date(as_of), to_cents(amount))
        except ValueError:
            balance = None
    if is_card or hint.kind in ("credit_card", "line_of_credit"):
        return Part(hint, rows, balance, card_file=True, flip_guess=_flip_guess(rows))
    return Part(hint, rows, balance)


_PAYMENT_WORDS = re.compile(r"PAYMENT|PAIEMENT|THANK YOU|MERCI|REFUND|RETURN|REVERSAL|CREDIT|REWARD|CASH ?BACK|INTEREST PAID",
                            re.IGNORECASE)


def _flip_guess(rows):
    """From a card or credit line file's rows alone: does it write purchases as positive? True or False
    when there are enough purchases to tell (TRNTYPE says which rows are purchases when it can), else None."""
    typed = [r for r in rows if r.bank_type.upper() in ("DEBIT", "CREDIT")]
    if typed:
        purchases = [r for r in typed if r.bank_type.upper() == "DEBIT"]
    else:
        purchases = [r for r in rows if not _PAYMENT_WORDS.search(r.name)]
    if len(purchases) < 2:
        return None
    return sum(r.amount > 0 for r in purchases) >= 0.7 * len(purchases)


# What the app remembers about how an account's files are signed, kept in account_sources under keys
# of its own (they go with the account if it's merged away or deleted): signs:<account id>:<topic>:<value>.
def _remembered(conn, account_id, topic):
    row = conn.execute("SELECT key FROM account_sources WHERE account_id = ? AND key LIKE ?",
                       (account_id, f"signs:{account_id}:{topic}:%")).fetchone()
    return row["key"].rsplit(":", 1)[1] if row else None


def _remember(conn, account_id, topic, value):
    conn.execute("DELETE FROM account_sources WHERE account_id = ? AND key LIKE ?",
                 (account_id, f"signs:{account_id}:{topic}:%"))
    conn.execute("INSERT OR IGNORE INTO account_sources (key, account_id) VALUES (?, ?)",
                 (f"signs:{account_id}:{topic}:{value}", account_id))


def _flip(conn, part, account_id):
    """(flip, worth remembering): does this card file write purchases as positive, so its amounts and
    balance are flipped? Decided once per account. First by the rows the account already holds (the
    file's row with the same date and amount, or with the opposite sign), then by what was decided
    before, then by how the file looks."""
    if account_id is not None and part.rows:
        have = Counter((r["date"], r["amount"]) for r in conn.execute(
            "SELECT date, amount FROM transactions WHERE account_id = ? AND date BETWEEN ? AND ?",
            (account_id, part.first_date, part.last_date)))
        same = opposite = 0
        for r in part.rows:
            if have[(r.date, r.amount)] > 0:
                have[(r.date, r.amount)] -= 1
                same += 1
            elif have[(r.date, -r.amount)] > 0:
                have[(r.date, -r.amount)] -= 1
                opposite += 1
        if opposite >= 2 and opposite > same:  # one pair could be a purchase and its refund
            return True, True
        if same > opposite:
            return False, True
        kept = _remembered(conn, account_id, "flip")
        if kept:
            return kept == "yes", False
    return bool(part.flip_guess), part.flip_guess is not None


def _positive_balance(conn, account_id, balance, ours, answer):
    """A card file whose charges are negative and whose balance is above zero: what you owe written
    as positive, or a card in your favour (overpaid, or a refund bigger than the balance)? What
    you answered, else the sign that agrees with the balance the app has for that date (`ours`, with
    this file's rows counted), else what the account's earlier files were. With none of those, as
    the file writes it: a real difference between the file and the app."""
    if answer in ("owed", "favour"):
        _remember(conn, account_id, "positive", answer)
        return -balance if answer == "owed" else balance
    if ours is not None:
        if ours == balance:
            return balance
        if ours == -balance:
            return -balance
    return -balance if _remembered(conn, account_id, "positive") == "owed" else balance


def _match_rows(part):
    """The rows to find a card file's account by: as the app will have them if the file is flipped."""
    return [replace(r, amount=-r.amount) for r in part.rows] if part.flip_guess else part.rows


def balance_questions(conn, parsed, chosen=None):
    """[{index, name, amount}]: the card or credit line balances in this file the app can't read the
    sign of. Charges negative and a balance above zero is a card in your favour for most banks, and
    what you owe for some, so for an account with no balance yet (and no earlier answer) it asks:
    a balance the app already has settles it (see _positive_balance)."""
    out = []
    for i, part in enumerate(parsed.parts):
        if not (part.card_file and part.balance and part.balance[1] > 0):
            continue
        rows = _match_rows(part)
        account_id = (chosen or {}).get(i) or find_account(conn, part.account, rows, fingerprints(rows))
        name = suggested_name(conn, part.account)
        if account_id is not None:
            acct = conn.execute("SELECT name, kind, anchor_date FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if acct is None or not ledger.is_debt(acct["kind"]) or acct["anchor_date"] is not None or \
                    _remembered(conn, account_id, "positive"):
                continue
            name = acct["name"]
        if _flip(conn, part, account_id)[0]:
            continue
        out.append({"index": i, "name": name, "amount": part.balance[1]})
    return out


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


def layout_kind(layout):
    """The kind of file a set-up CSV is, kept on its import: a bank that changes its columns, or a
    layout set up again, gives files worded differently, which count as another kind of file
    (the same date and amount is the same transaction: see _import_part)."""
    shape = repr([layout.get(k) for k in csvlayout.LAYOUT_KEYS])
    return f"csv:{layout['id']}:{hashlib.sha1(shape.encode()).hexdigest()[:8]}"


def csv_account_key(layout_id, filename):
    return f"csv:{layout_id}:{csv_name_key(filename)}"


def remember_csv_account(conn, layout_id, filename, account_id):
    """Files with this name (dates taken out) go to this account from now on. The account is
    also kept on record, under a key of its own, as one that gets files with this layout:
    banks reuse a file name for every account, so the name will point at whichever account
    answered last, and csv_account() looks for the others."""
    key = csv_account_key(layout_id, filename)
    conn.execute("INSERT INTO account_sources (key, account_id) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                 "SET account_id = excluded.account_id", (key, account_id))
    conn.execute("INSERT OR IGNORE INTO account_sources (key, account_id) VALUES (?, ?)", (f"{key}#{account_id}", account_id))


def saved_layout(conn, sniffed):
    """The layout saved for files like this one, or None."""
    row = conn.execute("SELECT * FROM csv_layouts WHERE signature = ?", (csvlayout.signature(sniffed),)).fetchone() \
        or conn.execute("SELECT * FROM csv_layouts WHERE signature = ?", (csvlayout.legacy_signature(sniffed),)).fetchone()
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
        balance = csvlayout.running_balance(text, layout)
    except csvlayout.LayoutError as e:  # the bank changed its file: show the app its columns again
        raise NeedsSetup("layout", f"{e}. Set the file up again from the Inbox.") from None
    key = csv_account_key(layout["id"], filename)
    account_id = csv_account(conn, layout["id"], key, rows, balance)
    note = csv_balance_note(conn, account_id or ledger.account_by_key(conn, [key]), balance)
    hint = AccountHint([key], "chequing", base_name(filename), create=False, account_id=account_id)
    return Parsed("csv", [Part(hint, rows, notes=[note] if note else [])], kind=layout_kind(layout))


# How well a set-up CSV fits an account (csv_fit), from -1 to 1.
CONTRADICTS, UNLIKELY, UNKNOWN, FITS = -1, -0.5, 0, 1
OVERLAP_ROWS = 3  # rows inside an account's dates before a file is judged by whether they match


def csv_fit(conn, account_id, rows, balance, trusted=False):
    """Whether a set-up CSV looks like this account's file: FITS, CONTRADICTS, UNLIKELY (maybe
    another sort of account) or UNKNOWN. Banks reuse one file name for every account, and a
    wrong account is silent wrong money. The evidence: rows inside the account's dates that
    are (or aren't) in it, a running-balance column that moves the way this sort of account's
    does (a bank account's rises with money in; a card's can go either way, by bank), and one
    that continues the balance the app has. `trusted`: the person already said this file name
    is this account, so a balance that doesn't continue (a missed download, a balance typed a
    little wrong) isn't held against it: csv_balance_note says so instead."""
    acct = conn.execute("SELECT kind FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if acct is None:
        return UNKNOWN
    scores = []
    if balance is not None:
        if ledger.is_debt(acct["kind"]):
            scores.append(FITS if balance["sign"] == -1 else UNLIKELY)
        else:
            scores.append(FITS if balance["sign"] == 1 else CONTRADICTS)
        ours = ledger.account_balance(conn, account_id, day_before(balance["date"]))
        if ours is not None and not trusted:
            scores.append(FITS if ours == balance["sign"] * balance["opening"] else CONTRADICTS)
    span = conn.execute("SELECT MIN(date) AS first, MAX(date) AS last FROM transactions WHERE account_id = ?",
                        (account_id,)).fetchone()
    inside = [r for r in rows if span["first"] and span["first"] <= r.date <= span["last"]]
    if len(inside) >= OVERLAP_ROWS:
        have = Counter((t["date"], t["amount"]) for t in conn.execute(
            "SELECT date, amount FROM transactions WHERE account_id = ? AND date BETWEEN ? AND ?",
            (account_id, min(r.date for r in inside), max(r.date for r in inside))))
        matched = 0
        for r in inside:
            if have[(r.date, r.amount)] > 0:
                have[(r.date, r.amount)] -= 1
                matched += 1
        scores.append(FITS if matched >= 0.5 * len(inside) else CONTRADICTS)
    if CONTRADICTS in scores:
        return CONTRADICTS
    if FITS in scores:
        return FITS
    return UNLIKELY if UNLIKELY in scores else UNKNOWN


def csv_balance_note(conn, account_id, balance):
    """What's off when a set-up CSV's running balance doesn't continue the account's: 'This file's
    balance on Oct 3 was $1,070.00; the app has $1,060.00'. '' when it continues, or there's
    nothing to compare."""
    if account_id is None or balance is None:
        return ""
    day = day_before(balance["date"])
    ours, theirs = ledger.account_balance(conn, account_id, day), balance["sign"] * balance["opening"]
    if ours is None or ours == theirs:
        return ""
    def money(cents):
        return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"
    month, number = int(day[5:7]), int(day[8:])
    when = f"{('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')[month - 1]} {number}"
    return f"This file's balance on {when} was {money(theirs)}; the app has {money(ours)}"


def csv_account(conn, layout_id, key, rows, balance):
    """The account a set-up CSV is for when looking at the file settles it, else None (the
    name, `key`, and the rows decide, as for any file). The account the name points to,
    unless the file contradicts it, or doesn't fit it while it fits exactly one other
    account that files with this layout go to; raises NeedsSetup('account') when the name's
    account is contradicted and no other fits: ask which account."""
    named = ledger.account_by_key(conn, [key])
    if named is None:
        return None
    fit = csv_fit(conn, named, rows, balance, trusted=True)
    if fit >= UNKNOWN:
        return None
    others = [r["account_id"] for r in conn.execute(
        "SELECT DISTINCT account_id FROM account_sources WHERE key LIKE ? AND account_id != ?",
        (f"csv:{layout_id}:%", named))]
    fitting = [a for a in others if csv_fit(conn, a, rows, balance) == FITS]
    if len(fitting) == 1:
        return fitting[0]
    if fit == CONTRADICTS:
        raise NeedsSetup("account")
    return None


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
    categorized: int = 0      # without asking (set once the new rows arrive in the inbox)
    to_review: int = 0
    first_date: str = None
    last_date: str = None
    balance_note: str = ""
    import_id: int = None
    new_ids: list = field(default_factory=list)  # for the inbox

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != "new_ids"}


def find_account(conn, hint, rows=(), fps=()):
    """The existing account a file's rows belong to, or None: by what the file says
    (`hint.keys`), else the one account that already has the file's rows."""
    return hint.account_id or _account_by_keys(conn, hint) or ledger.account_by_key(conn, hint.legacy_keys) \
        or account_with_rows(conn, hint.kind, rows, fps)


def _account_by_keys(conn, hint):
    """The account a file's keys find, most specific first. One found by the last four digits
    of its number alone has to be an account this file can be: the same sort (a card isn't a
    chequing account), at the same bank (a file that doesn't name its bank doesn't join an
    account at a named one), and without another whole number on record (two accounts can
    end in the same four digits)."""
    for key in hint.keys:
        found = ledger.account_by_key(conn, [key])
        if found is None:
            continue
        if key != hint.number_key or _same_number_account(conn, hint, found):
            return found
    return None


def _same_number_account(conn, hint, account_id):
    acct = conn.execute("SELECT kind, bank FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if acct is None:
        return False
    if ledger.is_debt(acct["kind"]) != ledger.is_debt(hint.kind):
        return False
    if hint.bank is None and acct["bank"] is not None:
        return False
    if hint.full_key:
        prefix = hint.full_key.rsplit(":", 1)[0] + ":"
        if conn.execute("SELECT 1 FROM account_sources WHERE account_id = ? AND key LIKE ? AND key != ?",
                        (account_id, prefix + "%", hint.full_key)).fetchone():
            return False
    return True


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


def import_parsed(conn, parsed, filename, sha, archived_path=None, accounts=None, answers=None):
    """Adds the new rows of a parsed bank file, one Result per account in the file; their
    `new_ids` are for the inbox. `accounts`: {part index: account id} for parts whose
    account the person chose. `answers`: {part index: 'owed' or 'favour'} for a card balance the
    person said the sign of (see balance_questions). Call inside db.write()."""
    if parsed.format == "actual":
        raise ImportError_("Actual Budget exports are learnt from, not imported.")
    accounts, answers = accounts or {}, answers or {}
    return [_import_part(conn, parsed.format, part, filename, sha, archived_path, accounts.get(i), parsed.kind,
                         answers.get(i))
            for i, part in enumerate(parsed.parts)]


def _import_part(conn, fmt, part, filename, sha, archived_path, account_id=None, kind=None, answer=None):
    kind = kind or fmt
    result = Result(filename, fmt, total=len(part.rows), first_date=part.first_date, last_date=part.last_date)
    notes = list(part.notes)
    rows = _match_rows(part)
    if account_id is None:
        account_id, created = find_or_create_account(conn, part.account, rows, fingerprints(rows))
    else:
        learn_keys(conn, part.account, account_id, chosen=True)
        created = False
    acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    flipped = False
    if part.card_file and ledger.is_debt(acct["kind"]):
        # Purchases negative, what you owe a negative balance: some banks write them the other way round.
        flipped, remember = _flip(conn, part, account_id)
        if remember:
            _remember(conn, account_id, "flip", "yes" if flipped else "no")
        if flipped:
            part = replace(part, rows=[replace(r, amount=-r.amount) for r in part.rows],
                           balance=(part.balance[0], -part.balance[1]) if part.balance else None)
            notes.append("This file shows purchases as positive: the signs were flipped")
    fps = fingerprints(part.rows)
    result.account_id, result.account_name, result.new_account = account_id, acct["name"], created
    import_id = conn.execute(
        "INSERT INTO imports (filename, sha256, account_id, format, rows_total, first_date, last_date, archived_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (filename, sha, account_id, kind, len(part.rows), part.first_date, part.last_date, archived_path)).lastrowid
    result.import_id = import_id

    # A bank's transaction ID is a hint, not proof: banks repeat them (one per day, 1, 2, 3…
    # in every download, one for two identical coffees). One that repeats within the file says
    # nothing about its rows, which the fingerprint tells apart (it counts identical rows).
    repeated = {f for f, n in Counter(r.fitid for r in part.rows if r.fitid).items() if n > 1}
    fitids = [None if row.fitid in repeated else row.fitid for row in part.rows]

    # First every row's exact twin: its fingerprint, else its bank ID. Those old rows leave the pool.
    twins = {}
    for i, (row, fp) in enumerate(zip(part.rows, fps)):
        existing = conn.execute("SELECT id, fitid FROM transactions WHERE account_id = ? AND fingerprint = ?",
                                (account_id, fp)).fetchone()
        if existing is None and fitids[i]:
            # The same ID counts only for the same amount, about when it was (posting moves a date).
            existing = conn.execute(
                "SELECT id, fitid FROM transactions WHERE account_id = ? AND fitid = ? AND amount = ? "
                "AND date BETWEEN date(?, ?) AND date(?, ?)",
                (account_id, fitids[i], row.amount, row.date, f"-{FITID_DAYS} days", row.date, f"+{FITID_DAYS} days")
            ).fetchone()
        if existing is not None:
            twins[i] = existing

    # Then, over every row left at once, the rows from a different kind of file (this bank's CSV, when
    # this is its Quicken file, or a layout set up again): the same amount, a day or two either side,
    # is the same transaction, whatever each file called it and whenever it was dated (posting moves a
    # date). Each is matched once, nearest date first: not in the order of the file, where a new
    # row would take the twin of an old one. Only where one of the two rows falls within the other's
    # file dates: the rows at the edges of two downloads (one ending Oct 15, the next starting Oct 16,
    # or overlapping by a day) are different transactions, and a fare on the 16th isn't the 14th's
    # (issue #124; ledger.one_transaction_in_two_files).
    gone = {t["id"] for t in twins.values()}
    others = [dict(r) for r in conn.execute(  # never a balance adjustment: it stands for rows still to come
        "SELECT t.id, t.date, t.amount, i.format AS kind, i.first_date AS first, i.last_date AS last "
        "FROM transactions t JOIN imports i ON i.id = t.import_id "
        "WHERE t.account_id = ? AND t.date BETWEEN date(?, ?) AND date(?, ?) AND t.hint != ?",
        (account_id, part.first_date or "", f"-{ledger.OTHER_KIND_DAYS} days",
         part.last_date or "", f"+{ledger.OTHER_KIND_DAYS} days", ADJUSTMENT))
        if r["id"] not in gone and not ledger.same_kind_of_file(r["kind"], kind)]
    left = [{"id": i, "date": row.date, "amount": row.amount, "first": part.first_date, "last": part.last_date}
            for i, row in enumerate(part.rows) if i not in twins]
    for i, other_id in ledger.pair_nearest(left, others, allowed=ledger.one_transaction_in_two_files):
        twins[i] = conn.execute("SELECT id, fitid FROM transactions WHERE id = ?", (other_id,)).fetchone()

    for i, (row, fp) in enumerate(zip(part.rows, fps)):
        fitid, existing = fitids[i], twins.get(i)
        # The ID is kept only where no other transaction in the account has it (it's unique there).
        keep_fitid = fitid if fitid and not conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND fitid = ?", (account_id, fitid)).fetchone() else None
        if existing is not None:
            if keep_fitid and not existing["fitid"]:
                conn.execute("UPDATE transactions SET fitid = ? WHERE id = ?", (keep_fitid, existing["id"]))
            result.duplicates += 1
            continue
        desc = describe(row.name, row.memo, acct["kind"])
        # One you added by hand (no fingerprint, bank ID or import) is the same transaction when
        # the bank's file brings it: same amount, a few days either side, each matched once.
        mine = conn.execute(
            "SELECT id FROM transactions WHERE account_id = ? AND fingerprint IS NULL AND fitid IS NULL "
            "AND import_id IS NULL AND amount = ? AND date BETWEEN date(?, ?) AND date(?, ?) "
            "ORDER BY abs(julianday(date) - julianday(?)), id LIMIT 1",
            (account_id, row.amount, row.date, f"-{MANUAL_DAYS} days", row.date, f"+{MANUAL_DAYS} days", row.date)).fetchone()
        if mine is not None:
            # It takes what the bank says and keeps what you decided (category, split, note, payee, by_you).
            payee_id, similar = ledger.resolve_payee(conn, desc)
            conn.execute("UPDATE transactions SET date = ?, raw_name = ?, raw_memo = ?, bank_type = ?, bank_category = ?, "
                         "hint = ?, fingerprint = ?, fitid = ?, import_id = ?, payee_id = COALESCE(payee_id, ?) WHERE id = ?",
                         (row.date, row.name, row.memo, row.bank_type, row.bank_category, transaction_hint(desc), fp,
                          keep_fitid, import_id, payee_id, mine["id"]))
            result.duplicates += 1
            continue
        payee_id, similar = ledger.resolve_payee(conn, desc)
        result.new_ids.append(conn.execute(
            "INSERT INTO transactions (account_id, date, amount, payee_id, raw_name, raw_memo, bank_type, "
            "bank_category, hint, fingerprint, fitid, import_id, similar_payee_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, row.date, row.amount, payee_id, row.name, row.memo, row.bank_type, row.bank_category,
             transaction_hint(desc), fp, keep_fitid, import_id, similar)).lastrowid)
    result.added = len(result.new_ids)
    conn.execute("UPDATE imports SET rows_new = ? WHERE id = ?", (result.added, import_id))

    if part.balance:
        # The bank's own balance beats one typed in by hand: a balance typed during the day
        # misses what the bank adds later that day, and every balance is worked out from
        # this one. So a file's balance replaces any as old as it, or older: a typed one without
        # a word, and that's where the balance check starts. One from an earlier file is checked
        # against this one by what arrived since (ledger.balance_difference): what no transaction
        # explains waits in the Inbox as a balance adjustment (Inbox.check_balance, once the
        # rows have arrived), so the budget's starting balance stays put.
        as_of, balance = part.balance
        ours = ledger.account_balance(conn, account_id, as_of)
        if part.card_file and ledger.is_debt(acct["kind"]) and not flipped and balance > 0:
            balance = _positive_balance(conn, account_id, balance, ours, answer)
            if balance < 0:
                notes.append("The file's balance read as what you owe")
        if acct["anchor_date"] is None or as_of >= acct["anchor_date"]:
            checked = acct["anchor_source"] == "file" and acct["check_from"] is not None
            conn.execute("UPDATE accounts SET anchor_date = ?, anchor_balance = ?, anchor_source = 'file', check_from = ? "
                         "WHERE id = ?", (as_of, balance, acct["check_from"] if checked else import_id, account_id))
            # Remembered so that removing this import puts the balance it replaced back.
            conn.execute("UPDATE imports SET anchor_before_date = ?, anchor_before_balance = ?, anchor_before_source = ?, "
                         "anchor_after_date = ?, anchor_after_balance = ? WHERE id = ?",
                         (acct["anchor_date"], acct["anchor_balance"], acct["anchor_source"], as_of, balance, import_id))
            if not checked and ours is not None and ours != balance:
                log.info("%s: balance on %s corrected from the file by %+d cents", acct["name"], as_of, balance - ours)
        elif ours is not None and ours != balance and not ledger.is_debt(acct["kind"]):
            # An older file's balance, by date: not for a card, whose files bring charges dated before they posted.
            result.balance_note = f"The file says the balance was {money(balance)} on {short_day(as_of)}; the app has {money(ours)}"
    if notes:
        result.balance_note = ". ".join([*notes, result.balance_note] if result.balance_note else notes)
        log.info("%s: %s", filename, result.balance_note)
    return result


def learn_from_actual(conn, parsed):
    """Takes categories and payee → category examples from an Actual Budget export.

    Categories are matched by name, including names they used to have (so renaming
    "Eating Out" to "Food" here still matches Actual's "Eating Out"); missing ones are
    created. Returns {'examples': n (new ones), 'already_learnt': n, 'categories_created': [...]}."""
    created, examples, already = [], 0, 0
    seen = {}  # (payee, category, amount, date) → how many of those this file has had so far
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
        payee_id, _ = ledger.resolve_payee(conn, desc)
        # Each example once: one this file taught before (or an earlier export that had it) isn't added
        # again, which would double its weight in every suggestion. Identical rows in one file
        # (two $4.00 coffees on a day) are each an example.
        key = (payee_id, category_id, r["amount"], r["date"])
        seen[key] = seen.get(key, 0) + 1
        have = conn.execute("SELECT COUNT(*) FROM examples WHERE payee_id = ? AND category_id = ? AND amount = ? "
                            "AND date = ? AND source = 'actual'", key).fetchone()[0]
        if have >= seen[key]:
            already += 1
            continue
        conn.execute("INSERT INTO examples (payee_id, category_id, amount, date, source) VALUES (?, ?, ?, ?, 'actual')", key)
        examples += 1
    return {"examples": examples, "already_learnt": already, "categories_created": created}


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
