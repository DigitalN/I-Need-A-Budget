# SPDX-License-Identifier: GPL-3.0-only
"""A new card whose balance needs a sign (issues #119, #126): the "A new account?" pop-up asks
what the balance is, so declining leaves no account behind. Made-up data only."""

from helpers import call, card_number, write
from test_card_signs import card_ofx

SEPTEMBER = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260912", "-40.00", "SAMPLE CAFE", "S2")]
OCTOBER = [("20261005", "-30.00", "SAMPLE BAKERY", "O1"), ("20261006", "-20.00", "SAMPLE DINER", "O2")]


def _file(app, name, text):
    return str(write(app.test_downloads, name, text))


def _cards_and_a_new_file(app):
    """One card the app has (with a balance), then a file for another card, with 112.65 as its balance."""
    app.db.set_meta("onboarded", "1")
    call(app, "import_files", [_file(app, "card-a.ofx", card_ofx(SEPTEMBER, "-100.00", "20260930", acct_id=card_number("1111")))])
    return _file(app, "card-b.ofx", card_ofx(OCTOBER, "112.65", "20261010", acct_id=card_number("2222")))


def _payment_categories(app):
    with app.db.read() as c:
        return c.execute("SELECT COUNT(*) FROM categories WHERE debt_account_id IS NOT NULL").fetchone()[0]


def test_the_new_account_pop_up_knows_it_has_a_balance_to_ask_about(app):
    path = _cards_and_a_new_file(app)
    out = call(app, "import_files", [path])
    assert [f["needs"] for f in out["needs_setup"]] == ["new_account"]
    [part] = call(app, "file_accounts", path)["parts"]
    assert part["balance"] == 11265 and part["asks_for_new"] is True
    assert [c["asks"] for c in part["choices"]] == [False]  # the card the app has already knows its balance


def test_a_new_card_without_an_answer_creates_nothing(app):
    path = _cards_and_a_new_file(app)
    before = (len(call(app, "accounts")), _payment_categories(app))
    refused = app.dispatch("set_file_accounts", [path, [{"index": 0, "name": "Sample New Card"}]])
    assert not refused["ok"] and "owe" in refused["error"]
    assert (len(call(app, "accounts")), _payment_categories(app)) == before


def test_the_answer_comes_with_the_new_account_and_imports_in_one_step(app):
    path = _cards_and_a_new_file(app)
    out = call(app, "set_file_accounts", path, [{"index": 0, "name": "Sample New Card", "balance": "owed"}])
    assert out["needs_setup"] == [] and out["results"][0]["added"] == 2
    card = next(a for a in call(app, "accounts") if a["name"] == "Sample New Card")
    assert card["balance"] == -11265


def test_in_your_favour_comes_with_it_too(app):
    path = _cards_and_a_new_file(app)
    call(app, "set_file_accounts", path, [{"index": 0, "name": "Sample New Card", "balance": "favour"}])
    assert next(a for a in call(app, "accounts") if a["name"] == "Sample New Card")["balance"] == 11265


def test_a_file_with_no_balance_to_read_asks_nothing_extra(app):
    app.db.set_meta("onboarded", "1")
    call(app, "import_files", [_file(app, "card-a.ofx", card_ofx(SEPTEMBER, "-100.00", "20260930", acct_id=card_number("1111")))])
    path = _file(app, "card-b.ofx", card_ofx(OCTOBER, "-50.00", "20261010", acct_id=card_number("2222")))
    [part] = call(app, "file_accounts", path)["parts"]
    assert part["balance"] is None and part["asks_for_new"] is False
    out = call(app, "set_file_accounts", path, [{"index": 0, "name": "Sample New Card"}])
    assert out["results"][0]["added"] == 2


# ── the page ──────────────────────────────────────────────────────────────────

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def test_the_new_account_pop_up_asks_the_balance_before_anything_is_made():
    assert "data-part-balance" in PAGE and 'name="balance-${p.index}"' in PAGE
    assert "choice.balance = answer.value" in PAGE and "Say whether ${money(p.balance)} is what you owe or in your favour." in PAGE
    assert 'data-asks="${p.asks_for_new ? 1 : 0}"' in PAGE


def test_a_card_in_your_favour_reads_as_in_your_favour():
    """Issue #126: a card balance above zero read "owing −$112.65" (Accounts), "You owe −$112.65" (Paying
    down debt) and "you owe −$112.65" (Check balance…). The page words the figure Python gives it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node")
    source = re.search(r"^const money = \(c, opts = \{\}\) => \{.*?^\};$", PAGE, re.S | re.M).group(0) + "\n" + \
        re.search(r"^const owingText = [^\n]*$", PAGE, re.M).group(0)
    script = source + """
console.log(JSON.stringify([owingText(11265), owingText(-11265), owingText(0), owingText(5000, m => `You owe ${m}`),
  owingText(-5000, m => `You owe ${m}`), owingText(-5000, m => `owe ${m}`)]));"""
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert got == ["$112.65 owing", "$112.65 in your favour", "$0.00 owing", "You owe $50.00", "$50.00 in your favour",
                   "$50.00 in your favour"]
    # ...and the places that printed a negative owing figure no longer do
    for old in ("${money(-a.balance)} owing", "owe ? money(-a.balance)", "${money(d.owing)}", "money(a.is_debt ? -a.balance : a.balance)",
                "owe ${money(c.owing)}", "You owe ${money(d.owed)}"):
        assert old not in PAGE, old
    assert "is in your favour" in PAGE and "'In your favour'" in PAGE and "'in your favour'" in PAGE
