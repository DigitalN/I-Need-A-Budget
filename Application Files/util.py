# SPDX-License-Identifier: GPL-3.0-only
"""Small helpers shared by the engine: money in cents, months and dates.

Money is always an int number of cents. Negative means money left the account.
Dates are ISO strings ('2026-10-08'); months are 'YYYY-MM'.
"""

import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


_DECIMAL_COMMA = re.compile(r"\d+,\d{1,2}")            # 12,34
_THOUSANDS_COMMAS = re.compile(r"\d+(?:,\d{3})+(?:\.\d+)?")  # 1,234  1,234,567.89
_EUROPEAN = re.compile(r"\d{1,3}(?:\.\d{3})+,\d{1,2}")    # 1.234,56


def _with_decimal_point(s):
    """The digits of an amount written with a decimal point, or None when its commas and dots
    don't make sense. A comma then one or two digits is the decimal comma of a French-language
    file; a comma then exactly three is a thousands separator; 1.234,56 is European."""
    sign = s[0] if s[:1] in ("+", "-") else ""
    digits = s[len(sign):]
    if "," not in digits:
        return s
    if _DECIMAL_COMMA.fullmatch(digits):
        return sign + digits.replace(",", ".")
    if _THOUSANDS_COMMAS.fullmatch(digits):
        return sign + digits.replace(",", "")
    if _EUROPEAN.fullmatch(digits):
        return sign + digits.replace(".", "").replace(",", ".")
    return None


def to_cents(text):
    """'−1,259.99', '(4.08)', '$12', '-10.4900', '-12,34' → int cents. Raises ValueError."""
    s = re.sub(r"\s+", "", str(text)).replace("$", "").replace("−", "-")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        s, negative = s[1:-1], True
    s = _with_decimal_point(s)
    try:
        if s is None:
            raise InvalidOperation
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Not an amount: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"Not an amount: {text!r}")
    cents = int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def month_of(day):
    return str(day)[:7]


def add_months(month, n):
    year, mon = (int(p) for p in month.split("-"))
    index = year * 12 + (mon - 1) + n
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def months_between(first, last):
    """Every month from `first` to `last`, inclusive. Empty if last < first."""
    out, month = [], first
    while month <= last:
        out.append(month)
        month = add_months(month, 1)
    return out


def first_day(month):
    return f"{month}-01"


def last_day(month):
    return (date.fromisoformat(first_day(add_months(month, 1))) - timedelta(days=1)).isoformat()


def day_before(day):
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def add_days(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"))}


def parse_date(text, order="mdy"):
    """Bank export dates: '10/08/2026' (month first, as Tangerine writes them, unless
    `order` is 'dmy'), '2026-10-08', '2026/10/08', OFX's '20261008' / '20261008120000[-5:EST]',
    'Oct 8, 2026', '8 Oct 2026', '08-OCT-26'. Two-digit years are 20xx."""
    s = str(text).strip()
    if order == "ymd":  # 2026-10-08, or 26-10-08 (a two-digit year first)
        m = re.fullmatch(r"(\d{4}|\d{2})[/.-](\d{1,2})[/.-](\d{1,2})", s)
        if m:
            return date(_year(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})", s)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        month, day = (b, a) if order == "dmy" else (a, b)
        return date(year, month, day).isoformat()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    words = re.findall(r"[A-Za-z]+|\d+", s)
    if len(words) == 3:
        names = [w for w in words if w[:3].lower() in MONTHS and not w.isdigit()]
        numbers = [w for w in words if w.isdigit()]
        if len(names) == 1 and len(numbers) == 2:
            day, year = (numbers if len(numbers[1]) >= len(numbers[0]) else numbers[::-1])
            if len(day) <= 2:
                return date(_year(year), MONTHS[names[0][:3].lower()], int(day)).isoformat()
    raise ValueError(f"Not a date: {text!r}")


def _year(text):
    return 2000 + int(text) if len(text) == 2 else int(text)


def money(cents):
    """12345 → '$123.45', -500 → '-$5.00': an amount in words the page shows as it is."""
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def month_name(month):
    """'2026-08' → 'August 2026'."""
    return f"{_MONTHS[int(month[5:7]) - 1]} {month[:4]}"


def short_day(day):
    """'2026-09-15' → 'Sep 15'."""
    return f"{_MONTHS[int(day[5:7]) - 1][:3]} {int(day[8:10])}"


def today():
    return date.today().isoformat()


def now():
    return datetime.now().isoformat(timespec="seconds")


def same_day_next_month(day, anchor_day=None):
    """The same day of the month a month later, clamped to the month's end. With `anchor_day`
    (the day a monthly charge belongs on: the 31st), that day instead of `day`'s, so a charge
    clamped to the 28th in February is due on the 31st again in March."""
    d = date.fromisoformat(day)
    month = add_months(month_of(day), 1)
    last = date.fromisoformat(last_day(month)).day
    return f"{month}-{min(anchor_day or d.day, last):02d}"
