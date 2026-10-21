#!/usr/bin/env python3
"""A made-up budget for screenshots and demos: nothing in it is anyone's real data.

    python3 scripts/demo_data.py [FOLDER] [--today 2026-10-14]

then the `demo` entry in .claude/launch.json (the preview on that folder, port 8766), and
scripts/screenshots.py for the README's pictures. FOLDER defaults to
~/Library/Caches/I Need A Budget-demo and is replaced each time.

Writes made-up Quicken files for a fictional credit union (chequing and a Mastercard),
imports them through the real engine (BudgetService, as the app does), categorizes most
of them the way a person would in the Inbox, sets budgeted amounts, catches up past
months, assigns this month and tracks the subscriptions it finds. A few recent charges
are left in the Inbox so it has something to show.
"""

import argparse
import os
import random
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_DATA = Path.home() / "Library" / "Application Support" / "I Need A Budget"

BANK = "Northwind Credit Union"
START = date(2026, 3, 1)

CATEGORIES = [
    ("Bills", ["Rent", "Utilities", "Phone & Internet", "Insurance", "Subscriptions"]),
    ("Everyday", ["Groceries", "Food", "Gas & Transit", "Household", "Fun Money"]),
    ("Saving", ["Emergency Fund", "Vacation", "Gifts"]),
]
BUDGETED = {"Rent": 1650_00, "Utilities": 160_00, "Phone & Internet": 140_00, "Insurance": 128_00,
            "Subscriptions": 45_00, "Groceries": 650_00, "Food": 220_00, "Gas & Transit": 200_00,
            "Household": 120_00, "Fun Money": 150_00, "Emergency Fund": 500_00, "Vacation": 200_00,
            "Gifts": 50_00}

# (description on the statement, category, low, high, visits a month)
CARD_SHOPS = [
    ("SAFEWAY #4821 CALGARY AB", "Groceries", 70, 110, 4),
    ("COSTCO WHOLESALE W532 CALGARY AB", "Groceries", 160, 220, 1),
    ("NO FRILLS #3120 CALGARY AB", "Groceries", 35, 60, 2),
    ("STARBUCKS #04412 CALGARY AB", "Food", 5, 12, 4),
    ("TIM HORTONS #2214 CALGARY AB", "Food", 3, 9, 3),
    ("A&W #4410 CALGARY AB", "Food", 12, 28, 1),
    ("SKIP THE DISHES WINNIPEG MB", "Food", 28, 55, 1),
    ("SHELL C04417 CALGARY AB", "Gas & Transit", 45, 75, 2),
    ("PETRO-CANADA 98112 CALGARY AB", "Gas & Transit", 40, 70, 1),
    ("CANADIAN TIRE #331 CALGARY AB", "Household", 15, 90, 1),
    ("IKEA CALGARY AB", "Household", 40, 90, 0.4),
    ("AMAZON.CA AMAZON.CA ON", "Household", 15, 70, 1.5),
    ("CINEPLEX #9147 CALGARY AB", "Fun Money", 18, 40, 0.8),
    ("INDIGO #922 CALGARY AB", "Fun Money", 15, 45, 0.6),
]
# (description, category, cents, day of month)
CARD_SUBSCRIPTIONS = [
    ("NETFLIX.COM 866-579-7172 CA", "Subscriptions", 18_99, 7),
    ("SPOTIFY P2A9C1D4F STOCKHOLM", "Subscriptions", 11_99, 15),
    ("APPLE.COM/BILL 866-712-7753 ON", "Subscriptions", 3_99, 22),
]


def ofx(rows, card, acct_id, balance, as_of):
    """rows: (date, cents, description, fitid)"""
    acct = (f"<CCACCTFROM><ACCTID>{acct_id}</CCACCTFROM>" if card else
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>")
    lines = "".join(f"<STMTTRN><TRNTYPE>{'CREDIT' if c > 0 else 'DEBIT'}<DTPOSTED>{d:%Y%m%d}<TRNAMT>{c / 100:.2f}"
                    f"<FITID>{f}<NAME>{n[:32]}<MEMO>{n}</STMTTRN>\n" for d, c, n, f in rows)
    stmt = "CCSTMTRS" if card else "STMTRS"
    msgs = ("CREDITCARDMSGSRSV1", "CCSTMTTRNRS") if card else ("BANKMSGSRSV1", "STMTTRNRS")
    return (f"OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>{BANK}<FID>00000"
            f"</FI></SONRS></SIGNONMSGSRSV1><{msgs[0]}><{msgs[1]}><{stmt}><CURDEF>CAD\n{acct}\n<BANKTRANLIST>\n"
            f"{lines}</BANKTRANLIST><LEDGERBAL><BALAMT>{balance / 100:.2f}<DTASOF>{as_of:%Y%m%d}</LEDGERBAL>"
            f"</{stmt}></{msgs[1]}></{msgs[0]}></OFX>\n")


def made_up_history(today):
    rnd = random.Random(2026)
    cheq, card = [], []
    d = START
    while d < today:
        if d.day == 1:
            cheq.append((d, -1650_00, "EFT Withdrawal to MAPLE RIDGE APARTMENTS"))
            cheq.append((d + timedelta(days=2), -128_00, "PREAUTHORIZED DEBIT ALLSTATE INSURANCE"))
        if d.day == 9:
            cheq.append((d, -rnd.randint(118_00, 152_00), "EFT Withdrawal to CALGARY UTILITIES"))
        if d.day == 12:
            cheq.append((d, -80_00, "PREAUTHORIZED DEBIT TELUS COMMUNICATIONS"))
            cheq.append((d, -55_00, "PREAUTHORIZED DEBIT ROGERS WIRELESS"))
        if (d - START).days % 14 == 4:
            cheq.append((d, 1720_40, "PAYROLL DEPOSIT NORTHWIND TRADERS"))
        if d.day == 28:
            cheq.append((d, 21_37, "Interest Paid"))
        # Steady habits: visits evenly spaced, amounts within their range, so a month is
        # only "above usual" when something really happened.
        for i, (name, _, low, high, per_month) in enumerate(CARD_SHOPS):
            every = 30 / per_month
            if int(((d - START).days + i * 3) % every) == 0 and rnd.random() < 0.9:
                card.append((d, -rnd.randint(low * 100, high * 100), name))
        for name, _, cents, day in CARD_SUBSCRIPTIONS:
            if d.day == day:
                card.append((d, -cents, name))
        d += timedelta(days=1)
    # One bigger, rarer charge for the Vacation category, and pay the card off each month.
    card.append((date(2026, 7, 18), -486_40, "WESTJET 8382940118 CALGARY AB"))
    # Recent ones the app isn't sure about, left in the Inbox: someone new, a refund, and
    # an e-transfer for a share of a dinner (the dinner itself already categorized).
    card += [(today - timedelta(days=4), -349_99, "BEST BUY #965 CALGARY AB"),
             (today - timedelta(days=7), -85_00, "THE KEG STEAKHOUSE #12 CALGARY AB"),
             (today - timedelta(days=3), 34_99, "AMAZON.CA AMAZON.CA ON")]
    cheq.append((today - timedelta(days=2), 42_50, "INTERAC e-Transfer From: JORDAN"))
    card.sort()
    month = START
    while month < today.replace(day=1):
        nxt = (month + timedelta(days=32)).replace(day=1)
        owed = -sum(c for dd, c, n in card if month <= dd < nxt and n != "PAYMENT - THANK YOU")
        pay_on = nxt + timedelta(days=19)
        if pay_on < today and owed:
            cheq.append((pay_on, -owed, f"Bill Payment {BANK.upper()} MASTERCARD"))
            card.append((pay_on, owed, "PAYMENT - THANK YOU"))
        month = nxt
    cheq.sort()
    card.sort()
    return cheq, card


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", nargs="?", default=str(Path.home() / "Library" / "Caches" / "I Need A Budget-demo"))
    parser.add_argument("--today", default="2026-10-14")
    args = parser.parse_args()
    data = Path(args.folder).expanduser().resolve()
    if data == REAL_DATA.resolve() or data in REAL_DATA.resolve().parents:
        sys.exit("Never the app's real data folder. Pass a throwaway folder.")
    if data.exists():
        shutil.rmtree(data)
    downloads = data / "downloads"
    downloads.mkdir(parents=True)
    os.environ["INAB_TODAY"] = args.today
    today = date.fromisoformat(args.today)

    sys.path.insert(0, str(ROOT / "Application Files"))
    sys.path.insert(0, str(ROOT / "tests"))
    from db import Database
    from helpers import call, card_number
    from service import BudgetService
    from settings import Settings

    settings = Settings(data / "settings.json")
    settings.update({"downloads_dir": str(downloads)})
    app = BudgetService(Database(data / "budget.db"), settings, data, hooks={})

    cheq, card = made_up_history(today)
    opening = 1_250_00
    files = [
        ("Chequing.qfx", ofx([(d, c, n, f"C{i}") for i, (d, c, n) in enumerate(cheq)], False, "000123456789",
                             opening + sum(c for _, c, _ in cheq), today - timedelta(days=1))),
        ("Mastercard.qfx", ofx([(d, c, n, f"M{i}") for i, (d, c, n) in enumerate(card)], True, card_number("4417"),
                               sum(c for _, c, _ in card), today - timedelta(days=1))),
    ]
    paths = []
    for name, text in files:
        (downloads / name).write_text(text, encoding="utf-8")
        paths.append(str(downloads / name))
    out = call(app, "import_files", paths)
    assert not out["errors"] and not out["needs_setup"], out

    for account in call(app, "accounts"):
        call(app, "update_account", account["id"],
             {"name": "Mastercard" if account["kind"] == "credit_card" else "Chequing", "apr": 20.99})
    for group, names in CATEGORIES:
        gid = call(app, "add_group", group)
        for n in names:
            call(app, "add_category", gid, n)
    # The cards' group under the spending groups, as a person would drag it
    call(app, "move_group", next(g["id"] for g in call(app, "categories") if g["role"] == "debt"))
    cats ={c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    by_text = {n: cat for n, cat, *_ in CARD_SHOPS + CARD_SUBSCRIPTIONS}
    by_text.update({"MAPLE RIDGE": "Rent", "ALLSTATE": "Insurance", "UTILITIES": "Utilities",
                    "TELUS": "Phone & Internet", "ROGERS": "Phone & Internet", "PAYROLL": "Income",
                    "WESTJET": "Vacation", "KEG STEAKHOUSE": "Food"})

    def category_for(item):
        text = " ".join(str(item.get(k) or "") for k in ("payee_name", "description", "memo", "raw_name")).upper()
        for key, cat in by_text.items():
            if re.search(rf"\b{re.escape(key.split(' ')[0])}\b", text):
                return cats[cat]
        return None

    # Categorize everything but the last few days, the way a person would in the Inbox.
    leave_from = (today - timedelta(days=6)).isoformat()
    for _ in range(50):
        items = [i for i in call(app, "inbox")["items"] if i["date"] < leave_from]
        if not items:
            break
        item = items[0]
        cid = category_for(item) or (item["guess"] if item.get("guess") else None)
        if not cid:
            print("No category for", item.get("payee_name"), item.get("description"))
            cid = cats["Household"]
        call(app, "categorize", [item["id"]], cid, True)

    call(app, "finish_onboarding", "2026-06")
    for name, cents in BUDGETED.items():
        call(app, "set_monthly_budget", cats[name], cents)
    call(app, "catch_up_all")
    month = today.strftime("%Y-%m")
    plan = call(app, "assign_plan", month)
    call(app, "apply_plan", month, [p for p in plan["plan"] if p["amount"]])

    names = {"Netflix": "Netflix", "Spotify": "Spotify", "Apple": "iCloud+", "Maple Ridge Apartments": "Rent",
             "Calgary Utilities": "Power & Water", "Allstate Insurance": "Car Insurance", "Telus": "Internet",
             "Rogers": "Phone"}
    for candidate in call(app, "subscriptions")["suggested"]:
        if candidate["name"] in names:
            call(app, "track_subscription", candidate=candidate, name=names[candidate["name"]])
        else:
            call(app, "dismiss_subscription", candidate)
    print(f"Made-up budget in {data}: {len(cheq)} chequing and {len(card)} card transactions, "
          f"{len(call(app, 'inbox')['items'])} left in the Inbox.")


if __name__ == "__main__":
    main()
