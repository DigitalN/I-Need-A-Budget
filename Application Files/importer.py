"""Reading bank exports and adding their new transactions, never twice.

Formats (detected from the content, not the file name):
- Tangerine credit card CSV: `Transaction date,Transaction,Name,Memo,Amount`. The file
  is named after the masked card number (four digits, x's, then the card's last four), which tells us
  which card. Memo holds the card's own category ("Rewards earned: 0.20 ~ Category: Restaurant").
- Tangerine bank account CSV: `Date,Transaction,Name,Memo,Amount`. Named after the
  account's nickname ("Chequing.CSV"). Name is cut at 32 characters; Memo has it in full.
- OFX / QFX (Tangerine's "Microsoft Money" and "Quicken" downloads): every transaction
  has the bank's own ID (FITID), and the file includes the account balance. The account
  is found by its bank and number. Tangerine calls every bank account SAVINGS in these
  files, chequing included, so for Tangerine the file name (the account's nickname, as
  with its CSVs) decides which account it is, not ACCTTYPE.
- Actual Budget's transaction export: not imported as transactions, only learnt from
  (its categories and which payees went where). See `learn_from_actual()`.

Which account: from what the file says (see each format), else the one existing account
that already has most of the file's rows, else a new account. A file never lands in an
account just because it's the only one of its kind: with more than one bank that would
put one bank's file in another bank's account.

Duplicates: each row gets a fingerprint from its date, amount and description, plus
how many identical rows came before it in the same file. Exports overlap freely, so a
row whose fingerprint is already in the account is skipped, while two identical coffees
on the same day in one file are both kept. **Never change `fingerprint()`**: rows
already imported would stop matching and every overlapping file would import twice.
OFX rows are also matched on FITID.
"""

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import ledger
import subscriptions
import transfers
from learn import analyze
from payees import describe
from util import parse_date, to_cents

log = logging.getLogger("inab")

CARD_HEADER = ["transaction date", "transaction", "name", "memo", "amount"]
BANK_HEADER = ["date", "transaction", "name", "memo", "amount"]
ACTUAL_HEADER = ["account", "date", "payee", "notes", "category_group", "category", "amount"]
SUPPORTED_SUFFIXES = {".csv", ".ofx", ".qfx"}


class ImportError_(Exception):
    """A file that isn't a bank export we can read."""


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


@dataclass
class Parsed:
    format: str           # tangerine_card | tangerine_bank | ofx | actual
    rows: list = field(default_factory=list)
    account: AccountHint = None
    balance: tuple = None  # (date, cents) when the file states one (OFX)
    actual_rows: list = field(default_factory=list)

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
    if header[:7] == ACTUAL_HEADER:
        return "actual"
    return None


def parse(path):
    """Reads one file. Raises ImportError_ if it isn't a format we know."""
    path = Path(path)
    text = read_text(path)
    fmt = detect(text)
    if fmt == "tangerine_card":
        return _parse_tangerine(text, path.name, card=True)
    if fmt == "tangerine_bank":
        return _parse_tangerine(text, path.name, card=False)
    if fmt == "ofx":
        return _parse_ofx(text, path.name)
    if fmt == "actual":
        return _parse_actual(text)
    raise ImportError_("This isn't a Tangerine export, an OFX/QFX file or an Actual Budget export.")


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
        hint = AccountHint(keys + ["tangerine-card"], "credit_card", "Mastercard", last4)
    else:
        key, kind, name = _tangerine_bank_account(filename)
        hint = AccountHint([key], kind, name)
    return Parsed("tangerine_card" if card else "tangerine_bank", rows, hint)


def account_number_key(bank, last4):
    """The account_sources key for an account number, the same whichever kind of file
    from that bank it came in."""
    return f"number:{re.sub(r'[^a-z0-9]', '', str(bank or '').lower()) or 'unknown'}:{last4}"


def _tangerine_bank_account(filename):
    """Tangerine names bank account downloads after the account's nickname ("Chequing.CSV",
    "Chequing.QFX"): (account_sources key, kind, name)."""
    stem = base_name(filename)
    lower = stem.lower()
    kind = "savings" if "saving" in lower else "line_of_credit" if "credit" in lower else "chequing"
    return f"tangerine-bank:{lower}", kind, stem.title() if stem.islower() or stem.isupper() else stem


def _ofx_value(block, tag):
    m = re.search(rf"<{tag}>\s*([^<\r\n]*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_ofx(text, filename):
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
    is_card = re.search(r"<CCACCTFROM>", text, re.IGNORECASE) is not None
    acct_block = re.search(r"<(?:CC|BANK)ACCTFROM>(.*?)</(?:CC|BANK)ACCTFROM>", text, re.IGNORECASE | re.DOTALL)
    acct_id = _ofx_value(acct_block.group(1) if acct_block else text, "ACCTID") or ""
    acct_type = (_ofx_value(acct_block.group(1), "ACCTTYPE") if acct_block else None) or ""
    digits = re.sub(r"\D", "", acct_id)
    last4 = digits[-4:] if len(digits) >= 4 else None
    bank = _ofx_bank(text)
    tangerine = bank == "Tangerine"
    number_keys = [account_number_key(bank, last4)] if last4 else []
    if is_card:
        keys = number_keys + (([f"tangerine-card:{last4}"] if last4 else []) + ["tangerine-card"] if tangerine else [])
        hint = AccountHint(keys, "credit_card", "Mastercard" if tangerine else f"{bank} card" if bank else "Credit card",
                           last4)
    else:
        kind = "savings" if acct_type.upper() in ("SAVINGS", "MONEYMRKT") else \
            "line_of_credit" if acct_type.upper() == "CREDITLINE" else "chequing"
        legacy = [f"ofx:{kind}:{last4}"] if last4 else []  # how version 1.0 keyed OFX accounts
        if tangerine:
            key, kind, name = _tangerine_bank_account(filename)
            hint = AccountHint(number_keys + [key], kind, name, last4, legacy)
        else:
            label = kind.replace("_", " ").title()
            hint = AccountHint(number_keys, kind, f"{bank} {label}" if bank else label, last4, legacy)
    balance = None
    ledger_block = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|$)", text, re.IGNORECASE | re.DOTALL)
    if ledger_block:
        amount, as_of = _ofx_value(ledger_block.group(1), "BALAMT"), _ofx_value(ledger_block.group(1), "DTASOF")
        try:
            if amount is not None and as_of:
                balance = (parse_date(as_of), to_cents(amount))
        except ValueError:
            balance = None
    return Parsed("ofx", rows, hint, balance)


def _ofx_bank(text):
    """The bank an OFX file comes from (its <ORG>), or None if it doesn't say."""
    org = (_ofx_value(text, "ORG") or "").strip()
    if "tangerine" in org.lower() or (_ofx_value(text, "BANKID") or "").lstrip("0") == "614":
        return "Tangerine"  # 614 is Tangerine's institution number
    return _unescape(org) or None


def _unescape(text):
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&apos;", "'").replace("&quot;", '"'))


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


def find_or_create_account(conn, hint, rows=(), fps=()):
    """The account a file's rows belong to: by what the file says (`hint.keys`), else the
    existing account that already has the file's rows, else a new one. A found account
    learns the file's keys, so the next file from it is recognised straight away."""
    account_id = ledger.account_by_key(conn, hint.keys) or ledger.account_by_key(conn, hint.legacy_keys) \
        or account_with_rows(conn, hint.kind, rows, fps)
    if account_id:
        for key in hint.keys:
            conn.execute("INSERT OR IGNORE INTO account_sources (key, account_id) VALUES (?, ?)", (key, account_id))
        return account_id, False
    name = hint.name
    if conn.execute("SELECT 1 FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        name = f"{name} {hint.last4}" if hint.last4 else f"{name} 2"
    keys = [k for k in hint.keys if not conn.execute("SELECT 1 FROM account_sources WHERE key = ?", (k,)).fetchone()]
    account_id = ledger.create_account(conn, name, hint.kind, keys=keys, last4=hint.last4)
    log.info("Created account %s (%s)", name, hint.kind)
    return account_id, True


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


def import_parsed(conn, parsed, filename, sha, archived_path=None):
    """Adds the new rows of a parsed bank file and files what it can. Call inside db.write()."""
    if parsed.format == "actual":
        raise ImportError_("Actual Budget exports are learnt from, not imported.")
    result = Result(filename, parsed.format, total=len(parsed.rows),
                    first_date=parsed.first_date, last_date=parsed.last_date)
    fps = fingerprints(parsed.rows)
    account_id, created = find_or_create_account(conn, parsed.account, parsed.rows, fps)
    acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    result.account_id, result.account_name, result.new_account = account_id, acct["name"], created
    import_id = conn.execute(
        "INSERT INTO imports (filename, sha256, account_id, format, rows_total, first_date, last_date, archived_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (filename, sha, account_id, parsed.format, len(parsed.rows), parsed.first_date, parsed.last_date,
         archived_path)).lastrowid
    result.import_id = import_id

    new_ids = []
    for row, fp in zip(parsed.rows, fps):
        exists = conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND fingerprint = ?",
                              (account_id, fp)).fetchone()
        if not exists and row.fitid:
            exists = conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND fitid = ?",
                                  (account_id, row.fitid)).fetchone()
        if exists:
            result.duplicates += 1
            continue
        desc = describe(row.name, row.memo, acct["kind"])
        payee_id, note, similar = ledger.resolve_payee(conn, desc)
        new_ids.append(conn.execute(
            "INSERT INTO transactions (account_id, date, amount, payee_id, raw_name, raw_memo, bank_type, "
            "bank_category, hint, fingerprint, fitid, import_id, reason, similar_payee_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, row.date, row.amount, payee_id, row.name, row.memo, row.bank_type, row.bank_category,
             desc.hint, fp, row.fitid, import_id, note, similar)).lastrowid)
    result.added = len(new_ids)
    conn.execute("UPDATE imports SET rows_new = ? WHERE id = ?", (result.added, import_id))

    filed, review = file_transactions(conn, new_ids)
    result.filed, result.to_review = filed, review

    if parsed.balance:
        # The bank's own balance beats one typed in by hand: a balance typed during the day
        # misses what the bank adds later that day, and every balance is worked out from
        # this one. So a file's balance replaces any as old as it, or older.
        as_of, balance = parsed.balance
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
    categories. Returns (filed without asking, left for review)."""
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
            conn.execute("UPDATE transactions SET category_id = COALESCE(?, category_id), status = 'review', "
                         "reason = ? WHERE id = ?", (sub["category_id"], alert, txn["id"]))
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
            guess = analyze(conn, txn)
            conn.execute("UPDATE transactions SET category_id = ?, status = 'review', reason = ? WHERE id = ?",
                         (guess.options[0] if guess.options else None, question, txn["id"]))
            review += 1
            continue
        matched = re.match(r"Matched to .*? \(similar name\)", txn["reason"] or "")
        suggestion = analyze(conn, txn)
        reason = suggestion.reason
        status = "done" if suggestion.sure and not txn["similar_payee_id"] else "review"
        if matched:
            reason = f"{matched.group(0)}. {reason}"
        category = suggestion.options[0] if suggestion.options else None
        if category is not None and txn["amount"] > 0 and suggestion.options == [ledger.income_category(conn)] \
                and not suggestion.sure:
            category = None  # unknown money in: don't guess "income" until asked
        kind = conn.execute("SELECT kind FROM payees WHERE id = ?", (txn["payee_id"],)).fetchone()
        if kind and kind["kind"] == "person" and not suggestion.sure:
            category = None  # transfers with people are never assumed, not even provisionally
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
