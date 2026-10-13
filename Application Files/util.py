"""Small helpers shared by the engine: money in cents, months and dates.

Money is always an int number of cents. Negative means money left the account.
Dates are ISO strings ('2026-10-08'); months are 'YYYY-MM'.
"""

import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_cents(text):
    """'−1,259.99', '(4.08)', '$12', '-10.4900' → int cents. Raises ValueError."""
    s = str(text).strip().replace(",", "").replace("$", "").replace("−", "-").replace(" ", "")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        s, negative = s[1:-1], True
    try:
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


def today():
    return date.today().isoformat()


def now():
    return datetime.now().isoformat(timespec="seconds")


def same_day_next_month(day):
    """The same day of the month a month later, clamped to the month's end."""
    d = date.fromisoformat(day)
    month = add_months(month_of(day), 1)
    last = date.fromisoformat(last_day(month)).day
    return f"{month}-{min(d.day, last):02d}"
