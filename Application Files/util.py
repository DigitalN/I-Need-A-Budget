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


def parse_date(text):
    """Bank export dates: '10/08/2026' (month first, as Tangerine writes them),
    '2026-10-08', or OFX's '20261008' / '20261008120000[-5:EST]'."""
    s = str(text).strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    raise ValueError(f"Not a date: {text!r}")


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
