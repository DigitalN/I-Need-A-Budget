# SPDX-License-Identifier: GPL-3.0-only
"""Pairing rows of two kinds of file compares only rows a day or two apart (issue #126), so the
"imported twice" check, run on every Inbox and Accounts load, stays fast on a long history.
Made-up data only."""

import random
import time

import ledger
from util import add_days, days_between


def _reference(wanted, offered, days, allowed=None):
    """What pair_nearest did before: every row against every row of the same amount."""
    by_amount = {}
    for o in offered:
        by_amount.setdefault(o["amount"], []).append(o)
    candidates = sorted(
        (gap, w["id"], o["id"]) for w in wanted for o in by_amount.get(w["amount"], ())
        for gap in [abs(days_between(w["date"], o["date"]))] if gap <= days and (allowed is None or allowed(w, o)))
    used_w, used_o, out = set(), set(), []
    for _, w, o in candidates:
        if w not in used_w and o not in used_o:
            used_w.add(w)
            used_o.add(o)
            out.append((w, o))
    return out


def test_the_window_gives_the_pairs_the_full_comparison_gave():
    rng = random.Random(7)
    for _ in range(60):
        days = rng.choice([0, 1, 2, 3])
        make = lambda start: [{"id": start + i, "date": add_days("2026-01-01", rng.randrange(25)),
                               "amount": rng.choice([-350, -350, -1200, 500, 7700])} for i in range(rng.randrange(0, 40))]
        wanted, offered = make(0), make(1000)
        allowed = None if rng.random() < .5 else (lambda w, o: (w["id"] + o["id"]) % 3 != 0)
        assert ledger.pair_nearest(wanted, offered, days, allowed) == _reference(wanted, offered, days, allowed)


def _long_history(app, name, kind, offset):
    """An account with 6,000 rows over 3,000 days, three amounts repeating all along."""
    with app.db.write() as c:
        account_id = ledger.create_account(c, name, "chequing", bank="Sample CU")
        first, last = add_days("2016-01-01", offset), add_days("2016-01-01", offset + 2999)
        import_id = c.execute("INSERT INTO imports (filename, sha256, account_id, format, first_date, last_date) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (name, name, account_id, kind, first, last)).lastrowid
        c.executemany("INSERT INTO transactions (account_id, date, amount, raw_name, fingerprint, import_id) "
                      "VALUES (?, ?, ?, 'SAMPLE FARE', ?, ?)",
                      [(account_id, add_days("2016-01-01", offset + i // 2), -(350 + 100 * (i % 3)), f"{name}-{i}", import_id)
                       for i in range(6000)])
    return account_id


def test_a_long_history_is_checked_quickly(app):
    a = _long_history(app, "Sample A", "tangerine_bank", 0)
    b = _long_history(app, "Sample B", "ofx", 1)  # the same days a day later, from another kind of file
    started = time.perf_counter()
    with app.db.read() as c:
        pairs = ledger.cross_kind_pairs(c, a, b)
        found = ledger.duplicate_accounts(c)
    assert time.perf_counter() - started < 3  # it took about 10 s when every row met every row of its amount
    assert len(pairs) > 5000
    assert [(d["account_id"], d["into_id"]) for d in found] == [(b, a)]
