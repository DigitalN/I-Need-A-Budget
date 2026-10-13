"""Reading any bank's CSV, once the person has shown the app which column is which.

Pure functions, no database. `sniff()` says whether a file looks like bank transactions
(a date column and a money column) and guesses its layout; `signature()` names the
layout, so the next file with the same columns is read without asking; `read()` turns a
file into (date, cents, description) rows with a layout.

A layout is a dict:
    has_header   the first row is column names
    date_col     column number of the date
    date_order   mdy | dmy | ymd (how to read 01/02/2026)
    desc_cols    column numbers joined, in order, into the description
    amount_col   one signed amount column (negative: money out) ...
    negate       ... unless this is set: money out is shown as positive (most card exports)
    out_col, in_col   or money out and money in in two columns
"""

import csv
import io
import re
from collections import Counter

from util import parse_date, to_cents

LAYOUT_KEYS = ("has_header", "date_col", "date_order", "desc_cols", "amount_col", "out_col", "in_col", "negate")
SAMPLE = 200

_OUT_WORDS = ("debit", "debits", "withdrawal", "withdrawals", "money out", "paid out", "out", "charge", "charges",
              "withdrawn", "spent")
_IN_WORDS = ("credit", "credits", "deposit", "deposits", "money in", "paid in", "in", "received")
_AMOUNT_WORDS = ("amount", "cad", "cad$", "amount cad", "transaction amount", "amt", "value", "net amount")
_DESC_WORDS = ("description", "payee", "name", "merchant", "details", "transaction details", "memo", "narrative",
               "description 1", "description 2", "transaction description", "particulars", "reference")


class LayoutError(ValueError):
    """The layout doesn't fit the file (a column that isn't there, a date that isn't one)."""


def table(text):
    """The file's rows, from its header (or first transaction) on. Skips a preamble
    above the table (some banks put the account and a date range there) and blank rows."""
    sample = text[:20000]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    rows = [[cell.strip() for cell in r] for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    rows = [r for r in rows if any(r)]
    widths = Counter(len(r) for r in rows if len(r) >= 3)
    if not widths:
        return []
    width = widths.most_common(1)[0][0]
    # A preamble line can have as many commas as the table, but not 3 filled cells.
    start = next((i for i, r in enumerate(rows) if len(r) == width and sum(bool(c) for c in r) >= min(3, width)), None)
    if start is None:
        return []
    return [r + [""] * (width - len(r)) for r in rows[start:] if len(r) >= width - 1]


def _is_date(value):
    try:
        parse_date(value)
        return True
    except ValueError:
        try:
            parse_date(value, "dmy")
            return True
        except ValueError:
            return False


_MONEY = re.compile(r"[-+−]?\(?[-+−]?\$?\s*[\d,]*\.?\d+\)?(\s*(CR|DR))?", re.IGNORECASE)


def _is_money(value):
    return bool(_MONEY.fullmatch(value)) and any(ch.isdigit() for ch in value) and not _is_date(value)


def _cents(value):
    """to_cents, with a trailing CR (credit, money in) or DR (debit, money out)."""
    m = re.fullmatch(r"(.*?)\s*(CR|DR)", value, re.IGNORECASE)
    if m:
        cents = abs(to_cents(m.group(1)))
        return cents if m.group(2).upper() == "CR" else -cents
    return to_cents(value)


def _column_types(rows, width):
    """'d' date, 'm' money, 'e' empty, 't' text, for each column of these rows."""
    types = []
    for col in range(width):
        values = [r[col] for r in rows if r[col]]
        if not values:
            types.append("e")
        elif sum(_is_date(v) for v in values) >= 0.9 * len(values):
            types.append("d")
        elif sum(_is_money(v) for v in values) >= 0.9 * len(values) and \
                sum(bool(re.search(r"\.\d{2}\b", v)) for v in values) >= 0.5 * len(values):
            types.append("m")
        else:
            types.append("t")
    return types


def sniff(text):
    """None if this isn't a table of transactions. Otherwise {'header': [...] or None,
    'rows': [...] (data rows), 'types': [...], 'guess': layout}."""
    rows = table(text)
    if len(rows) < 2:
        return None
    width = len(rows[0])
    types = _column_types(rows[1:SAMPLE + 1], width)
    if "d" not in types or "m" not in types or "t" not in types:
        return None
    date_col = types.index("d")
    has_header = not _is_date(rows[0][date_col])
    header = rows[0] if has_header else None
    data = rows[1:] if has_header else rows
    if not has_header:
        types = _column_types(data[:SAMPLE], width)
    return {"header": header, "rows": data, "types": types, "width": width,
            "guess": _guess(header, data, types, date_col)}


def _named(header, col, words):
    return header is not None and re.sub(r"\s+", " ", header[col].lower().strip(" :")) in words


def _guess(header, data, types, date_col):
    money = [i for i, t in enumerate(types) if t == "m" and not (header and "balance" in header[i].lower())]
    layout = {"has_header": header is not None, "date_col": date_col, "date_order": date_order(data, date_col),
              "desc_cols": [], "amount_col": None, "out_col": None, "in_col": None, "negate": False}
    outs = [i for i in money if _named(header, i, _OUT_WORDS)]
    ins = [i for i in money if _named(header, i, _IN_WORDS)]
    amounts = [i for i in money if _named(header, i, _AMOUNT_WORDS)]
    if outs and ins:
        layout["out_col"], layout["in_col"] = outs[0], ins[0]
    elif amounts:
        layout["amount_col"] = amounts[0]
    elif len(money) >= 2 and _one_or_the_other(data[:SAMPLE], money[0], money[1]):
        layout["out_col"], layout["in_col"] = money[0], money[1]  # Withdrawals, Deposits (, Balance)
    elif money:
        layout["amount_col"] = money[0]
    if layout["amount_col"] is not None:
        # A card export showing purchases as positive: mostly positive, and the few negative
        # rows are payments and refunds. (A savings account, also mostly positive, has
        # withdrawals and transfers as its negatives.)
        col = layout["amount_col"]
        rows = [(_cents(r[col]), " ".join(r).upper()) for r in data[:SAMPLE] if _is_money(r[col])]
        negatives = [text for cents, text in rows if cents < 0]
        layout["negate"] = len(rows) >= 3 and len(rows) - len(negatives) >= 0.6 * len(rows) and bool(negatives) and all(
            re.search(r"PAYMENT|PAIEMENT|REFUND|RETURN|REVERSAL|CASH ?BACK|REWARD|CREDIT", t) for t in negatives)
    text_cols = [i for i, t in enumerate(types) if t == "t"]
    named = [i for i in text_cols if _named(header, i, _DESC_WORDS)]
    if named:
        layout["desc_cols"] = named[:2]
    elif text_cols:
        layout["desc_cols"] = [max(text_cols, key=lambda i: sum(len(r[i]) for r in data[:SAMPLE]))]
    return layout


def _one_or_the_other(rows, a, b):
    return sum(bool(r[a]) != bool(r[b]) for r in rows) >= 0.9 * len(rows)


def date_order(rows, col):
    """mdy, dmy or ymd, from the dates themselves: a first number over 12 can only be a day."""
    firsts, seconds = [], []
    for r in rows[:SAMPLE]:
        m = re.fullmatch(r"(\d{1,4})[/.-](\d{1,2})[/.-]\d{2,4}", r[col])
        if m:
            if len(m.group(1)) == 4:
                return "ymd"
            firsts.append(int(m.group(1)))
            seconds.append(int(m.group(2)))
    if any(f > 12 for f in firsts):
        return "dmy"
    return "mdy"


def signature(sniffed):
    """What makes two files the same layout: the header row, or for a file without one,
    how many columns it has and what's in each."""
    if sniffed["header"]:
        return "h:" + "|".join(re.sub(r"\s+", " ", h.lower()) for h in sniffed["header"])
    return f"n:{sniffed['width']}:" + "".join(sniffed["types"])


def clean_layout(layout, width):
    """A layout from the page, checked against a file `width` columns wide."""
    try:
        out = {"has_header": bool(layout.get("has_header")), "date_col": int(layout["date_col"]),
               "date_order": layout.get("date_order") if layout.get("date_order") in ("mdy", "dmy", "ymd") else "mdy",
               "desc_cols": [int(c) for c in layout.get("desc_cols") or [] if c not in (None, "")],
               "amount_col": None, "out_col": None, "in_col": None, "negate": bool(layout.get("negate"))}
        for key in ("amount_col", "out_col", "in_col"):
            if layout.get(key) not in (None, ""):
                out[key] = int(layout[key])
    except (KeyError, TypeError, ValueError):
        raise LayoutError("Pick the date, description and amount columns.") from None
    columns = [out["date_col"], *out["desc_cols"], out["amount_col"], out["out_col"], out["in_col"]]
    if any(c is not None and not 0 <= c < width for c in columns):
        raise LayoutError("That column isn't in the file.")
    if not out["desc_cols"]:
        raise LayoutError("Pick the column with the description.")
    if out["amount_col"] is None and (out["out_col"] is None or out["in_col"] is None):
        raise LayoutError("Pick the amount column, or the money out and money in columns.")
    if out["amount_col"] is not None:
        out["out_col"] = out["in_col"] = None
    return out


def read(text, layout):
    """[(date, cents, description)] for every transaction in the file. Raises LayoutError
    naming the first row it can't read."""
    rows = table(text)
    if layout["has_header"]:
        rows = rows[1:]
    out = []
    for n, r in enumerate(rows, start=2 if layout["has_header"] else 1):
        try:
            if not r[layout["date_col"]]:
                continue  # a total or a note under the table
            day = parse_date(r[layout["date_col"]], layout["date_order"])
            if layout["amount_col"] is not None:
                value = r[layout["amount_col"]]
                if not value:
                    continue
                cents = _cents(value)
                if layout["negate"]:
                    cents = -cents
            else:
                out_v, in_v = r[layout["out_col"]], r[layout["in_col"]]
                if not out_v and not in_v:
                    continue
                cents = (abs(_cents(in_v)) if in_v else 0) - (abs(_cents(out_v)) if out_v else 0)
        except (ValueError, IndexError) as e:
            raise LayoutError(f"Row {n} doesn't fit: {e}") from None
        description = " ".join(r[c] for c in layout["desc_cols"] if c < len(r) and r[c])
        out.append((day, cents, description))
    return out
