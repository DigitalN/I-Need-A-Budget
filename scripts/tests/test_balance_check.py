# SPDX-License-Identifier: GPL-3.0-only
"""A file's balance never quietly moves the budget's starting balance: a difference between two
files' balances waits in the Inbox as a balance adjustment, worked out by what arrived (issue #43).
Made-up data only."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import db
from helpers import bank_csv, call, card_number, write
from test_card_signs import card_ofx

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE CU"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _import(app, name, text):
    return call(app, "import_files", [str(write(app.test_downloads, name, text))])


def _adjustments(app):
    return [i for i in call(app, "inbox")["items"] if i["payee_name"] == "Balance adjustment"]


def _start(app):
    return call(app, "month", "2026-09")["summary"]["carried_rta"]


SEPTEMBER = [("20260901", "2000.00", "PAYROLL SAMPLE CO", "A1"), ("20260910", "-50.00", "SAMPLE CAFE CALGARY AB", "A2")]
MISSING = [("20260920", "-100.00", "SAMPLE PHONE CO", "A5"), ("20260925", "-200.00", "SAMPLE INSURANCE CO", "A6")]
OCTOBER = [("20261003", "-100.00", "SAMPLE HARDWARE CALGARY AB", "A4")]


def _first_file(app):
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    call(app, "finish_onboarding", "2026-09")


def _second_file(app):
    """October's download, without the two September payments the bank's balance has (a download missing)."""
    return _import(app, "chequing-2.ofx", ofx(OCTOBER, "2600.00", "20261010"))


def test_a_gap_between_downloads_doesnt_move_the_starting_balance(app):
    """The issue's case: the balance taken from file 2 moved September's starting Ready to assign by $300."""
    _first_file(app)
    start = _start(app)
    assert start == 105000
    out = _second_file(app)
    assert _start(app) == start
    [adjustment] = _adjustments(app)
    assert (adjustment["amount"], adjustment["date"], adjustment["guess"], adjustment["group"]) == (-30000, "2026-10-10", None, "")
    assert "$300.00 more went out" in adjustment["reason"] and "between Sep 15 and Oct 3" in adjustment["reason"]
    assert "balance adjustment is waiting in the Inbox" in out["results"][0]["balance_note"]
    assert "balance adjustment" in call(app, "imports")[0]["balance_note"]  # Accounts → Imported files says so too
    assert call(app, "accounts")[0]["balance"] == 260000  # the bank's balance, as before


def test_a_balance_adjustment_is_never_guessed_or_accepted(app):
    _first_file(app)
    _second_file(app)
    [adjustment] = _adjustments(app)
    out = call(app, "accept_all", [adjustment["id"]])
    assert out["categorized"] == 0 and len(_adjustments(app)) == 1


def test_a_typed_balance_gives_way_to_a_files_without_a_word(app):
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 290000, "2026-09-30")  # typed: wrong by the two payments' $300.00
    out = _second_file(app)
    assert _adjustments(app) == [] and out["results"][0]["balance_note"] == ""
    assert call(app, "accounts")[0]["balance"] == 260000


def test_a_card_files_charges_dated_before_the_last_balance_make_no_adjustment(app):
    """Card files bring charges dated before they posted: by date, most card balances looked off."""
    sep = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260912", "-40.00", "SAMPLE CAFE", "S2")]
    _import(app, "card-1.ofx", card_ofx(sep, "-100.00", "20260930"))
    # A charge from Sep 29 that posted after the first download, then October's own.
    october = [("20260929", "-25.00", "SAMPLE TAXI", "S3"), ("20261005", "-30.00", "SAMPLE BAKERY", "O1")]
    out = _import(app, "card-2.ofx", card_ofx(october, "-155.00", "20261010"))
    assert _adjustments(app) == [] and out["results"][0]["balance_note"] == ""
    assert call(app, "accounts")[0]["balance"] == -15500


def test_a_waiting_adjustment_shrinks_and_goes_as_the_missing_transactions_arrive(app):
    _first_file(app)
    _second_file(app)
    # The missing downloads turn up: September's second half, in two older files.
    out = _import(app, "chequing-sep-a.ofx", ofx(MISSING[:1], "2850.00", "20260922"))
    assert [a["amount"] for a in _adjustments(app)] == [-20000]
    assert "now -$200.00" in out["results"][0]["balance_note"]
    out = _import(app, "chequing-sep-b.ofx", ofx(MISSING[1:], "2650.00", "20260930"))
    assert _adjustments(app) == []
    assert "the balance adjustment is gone" in out["results"][0]["balance_note"]
    assert call(app, "accounts")[0]["balance"] == 260000


def test_removing_the_file_that_filled_the_gap_puts_the_adjustment_back(app):
    _first_file(app)
    _second_file(app)
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2650.00", "20260930"))
    assert _adjustments(app) == []
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert [a["amount"] for a in _adjustments(app)] == [-30000]
    # ...and removing the file that made it takes it, and its balance, away.
    call(app, "undo_import", call(app, "imports")[0]["id"])
    assert _adjustments(app) == []
    assert (call(app, "accounts")[0]["anchor_date"], _start(app)) == ("2026-09-15", 105000)


def test_one_you_categorized_asks_when_its_money_arrives_and_remove_it_takes_both(app):
    _first_file(app)
    _second_file(app)
    categories = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    [adjustment] = _adjustments(app)
    call(app, "categorize", txn_ids=[adjustment["id"]], category_id=categories["Income"])  # yours now
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2650.00", "20260930"))
    [question] = _adjustments(app)  # the app's new one, cancelling yours out
    assert question["amount"] == 30000 and question["guess"] is None
    assert question["reason"] == "The $300.00 your balance adjustment stood for has arrived: remove the adjustment?"
    assert question["adjustment_question"]["ids"] == [adjustment["id"]]
    call(app, "remove_adjustment", question["id"])
    assert _adjustments(app) == []
    assert not [t for t in call(app, "transactions", {})["items"] if t["payee_name"] == "Balance adjustment"]
    assert call(app, "accounts")[0]["balance"] == 260000 and _start(app) == 105000
    assert call(app, "undo")["undone"] == "Remove balance adjustment"  # one small step
    assert len(_adjustments(app)) == 1


def test_keep_both_makes_the_new_adjustment_yours(app):
    _first_file(app)
    _second_file(app)
    categories = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    [adjustment] = _adjustments(app)
    call(app, "categorize", txn_ids=[adjustment["id"]], category_id=categories["Income"])
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2650.00", "20260930"))
    [question] = _adjustments(app)
    call(app, "keep_both", question["id"])
    [kept] = _adjustments(app)
    assert kept["id"] == question["id"] and kept["yours"] and "adjustment_question" not in kept
    # A later download changes nothing: both are yours, and they cancel out.
    _import(app, "chequing-3.ofx", ofx([("20261012", "-20.00", "SAMPLE BAKERY", "A7")], "2580.00", "20261012"))
    assert [a["id"] for a in _adjustments(app)] == [kept["id"]]


def test_an_adjustment_is_never_a_transfers_other_half_nor_a_copy_of_a_bank_row(app):
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(OCTOBER, "2700.00", "20261010"))  # $200.00 missing
    [adjustment] = _adjustments(app)
    # The card's file brings a $200.00 payment the day before: it isn't paired with the adjustment...
    card = [("20261009", "200.00", "PAYMENT - THANK YOU", "P1"), ("20261002", "-15.00", "SAMPLE CAFE", "P2")]
    _import(app, "card.ofx", card_ofx(card, "-0.00", "20261010", acct_id=card_number("4444")))
    again = next(t for t in call(app, "transactions", {})["items"] if t["id"] == adjustment["id"])
    assert (again["transfer_id"], again["status"]) == (None, "review")
    # ...and chequing's CSV bringing the missing row, dated next to it, isn't taken for a copy of it.
    csv = bank_csv([("10/03/2026", "SAMPLE HARDWARE CALGARY AB", "SAMPLE HARDWARE CALGARY AB", "-100.00"),
                    ("10/09/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-200.00"),
                    ("10/11/2026", "SAMPLE CAFE CALGARY AB", "SAMPLE CAFE CALGARY AB", "-4.00")])
    path = write(app.test_downloads, "Chequing.CSV", csv)
    chequing = next(a["id"] for a in call(app, "accounts") if not a["is_debt"])
    [result] = call(app, "set_file_accounts", str(path), [{"index": 0, "account_id": chequing}])["results"]
    assert result["added"] == 2  # the payment and the café, not the hardware store again
    # The CSV goes past the balance's date, so what it brought counts once a newer balance has it.
    assert [a["amount"] for a in _adjustments(app)] == [-20000]
    _import(app, "chequing-3.ofx", ofx([("20261012", "-20.00", "SAMPLE BAKERY", "A7")], "2676.00", "20261012"))
    assert _adjustments(app) == []


def test_reconciling_adds_an_adjustment_that_waits_and_the_next_file_is_taken_as_it_is(app):
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 295000)["adjusted"] == -5000
    [adjustment] = _adjustments(app)
    assert adjustment["guess"] is None and adjustment["reason"] == "To match the balance the bank shows"
    _second_file(app)  # checks start again from this file's balance
    assert [a["amount"] for a in _adjustments(app)] == [-5000]


def test_merging_in_a_copy_with_the_missing_transactions_explains_the_gap(app):
    _first_file(app)
    _second_file(app)
    assert len(_adjustments(app)) == 1
    # The missing download went into an account of its own (a new account number, say).
    path = write(app.test_downloads, "copy.ofx", ofx(SEPTEMBER + MISSING, "2650.00", "20260930", acct_id="000999888777"))
    call(app, "set_file_accounts", str(path), [{"index": 0, "name": "Copy"}])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    out = call(app, "merge_accounts", accounts["Copy"], accounts["SAMPLE CU Chequing"])
    assert (out["moved"], out["dropped"]) == (2, 2)
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 260000 and _start(app) == 105000
    assert "Copy comes back as an account of its own" in call(app, "undo_step")["undoes"]


def test_an_existing_database_learns_where_its_balances_came_from(tmp_path):
    path = tmp_path / "budget.db"
    database = db.Database(path)
    c = database.conn
    c.execute("INSERT INTO accounts (id, name, kind, anchor_date, anchor_balance) VALUES "
              "(1, 'From a file', 'chequing', '2026-10-10', 500), (2, 'An older file', 'chequing', '2026-10-01', 700), "
              "(3, 'Typed', 'cash', '2026-10-05', 900), (4, 'No balance', 'savings', NULL, NULL)")
    c.execute("INSERT INTO imports (id, filename, sha256, account_id, format, anchor_after_date, anchor_after_balance) "
              "VALUES (1, 'a.ofx', 'x', 1, 'ofx', '2026-10-10', 500), (2, 'b.ofx', 'y', 2, 'ofx', NULL, NULL), "
              "(3, 'c.csv', 'z', 3, 'tangerine_bank', NULL, NULL)")
    c.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
    database.close()
    again = db.Database(path)
    rows = {r["id"]: (r["anchor_source"], r["check_from"]) for r in again.conn.execute("SELECT * FROM accounts")}
    assert rows == {1: ("file", 1), 2: ("file", None), 3: ("typed", None), 4: (None, None)}
    assert again.get_meta("schema_version") == "8"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v7-to-v8-*.db"))


# ── onboarding's 3. Balances (issue #135) ────────────────────────────────────

GAP = [("20261014", "-100.00", "SAMPLE HARDWARE CALGARY AB", "A4")]  # with the bank's $2,600.00 on Oct 20: $300.00 missing


def test_onboardings_balances_step_keeps_the_files_balance_checked(app):
    """The issue's case: Continue saves every box that has an amount, so the file's own balance came back as a
    typed one, and the next download's gap moved September's starting Ready to assign without a word."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    account = call(app, "accounts")[0]
    call(app, "set_balance", account_id=account["id"], cents=account["balance"], in_favour=False)  # Continue
    assert call(app, "undo_step") is None  # nothing changed, so there's nothing to undo
    call(app, "finish_onboarding", "2026-09")
    _import(app, "chequing-2.ofx", ofx(GAP, "2600.00", "20261020"))
    assert [a["amount"] for a in _adjustments(app)] == [-30000] and _start(app) == 105000


def test_a_changed_balance_on_a_files_row_is_typed_for_that_day(app):
    """3. Balances shows a file's balance for its own day ("Balance on Sep 15, 2026, from your file"): the same
    amount there changes nothing, and another is a typed balance for that day, not the day you onboard."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    account = call(app, "accounts")[0]
    assert (account["balance"], account["balance_date"]) == (300000, "2026-09-15")
    call(app, "set_balance", account_id=account["id"], cents=300000, date="2026-09-15")
    assert call(app, "undo_step") is None
    call(app, "set_balance", account_id=account["id"], cents=295000, date="2026-09-15")
    account = call(app, "accounts")[0]
    assert (account["anchor_source"], account["anchor_date"], account["balance"]) == ("typed", "2026-09-15", 295000)


def test_a_balance_is_for_the_day_of_the_last_transaction_it_counts(app):
    """A file whose rows go past its balance's date: the balance shown counts them, so it's for that later day."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER + MISSING[:1], "3000.00", "20260915"))
    account = call(app, "accounts")[0]
    assert (account["balance"], account["balance_date"]) == (290000, "2026-09-20")
    call(app, "set_balance", account_id=account["id"], cents=290000, date="2026-09-20")
    assert call(app, "undo_step") is None


CARD_SEPTEMBER = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260912", "-40.00", "SAMPLE CAFE", "S2")]
CARD_OCTOBER = [("20261005", "-30.00", "SAMPLE BAKERY", "O1")]


def _card_read_as_in_your_favour(app):
    """A bank that writes what you owe as a positive number: during onboarding its $120.50 is read as written."""
    _import(app, "card-1.ofx", card_ofx(CARD_SEPTEMBER, "120.50", "20260930"))
    [card] = call(app, "accounts")
    assert card["balance"] == 12050  # in your favour, as the file writes it
    return card


def test_unticking_in_your_favour_on_a_cards_file_balance_is_remembered(app):
    """Unticked on 3. Balances, the card's later files are read that way even when their balance disagrees with
    the app's, like the answer to "Is $120.50 what you owe…?". Without it, the next one with a gap was read as
    written, in your favour, and replaced your balance without a word."""
    card = _card_read_as_in_your_favour(app)
    call(app, "set_balance", account_id=card["id"], cents=12050, in_favour=False, date=card["balance_date"])
    call(app, "finish_onboarding", "2026-09")
    # The bank says $160.50 while the app has you owing $150.50: a $10.00 charge is in no file.
    _import(app, "card-2.ofx", card_ofx(CARD_OCTOBER, "160.50", "20261010"))
    assert call(app, "accounts")[0]["balance"] == -16050  # what you owe, not $160.50 in your favour


def test_a_cards_box_left_as_it_is_remembers_nothing(app):
    card = _card_read_as_in_your_favour(app)
    call(app, "set_balance", account_id=card["id"], cents=12050, in_favour=True, date=card["balance_date"])  # Continue
    call(app, "finish_onboarding", "2026-09")
    _import(app, "card-2.ofx", card_ofx(CARD_OCTOBER, "160.50", "20261010"))
    assert call(app, "accounts")[0]["balance"] == 16050  # as the file writes it: nothing was said about its sign


# ── the page: 3. Balances as you'd use it ────────────────────────────────────

_BALANCES_STEP = r'''
const ACCOUNTS = __ACCOUNTS__;
const out = { calls: [], toasts: [], html: '' };
const state = {};
const root = { hidden: true, innerHTML: '' };
let inputs = [];
function drawn(html) {  // the <input>s the page drew: boxes to type in and tick
  return [...html.matchAll(/<input([^>]*)>/g)].map(m => {
    const attrs = Object.fromEntries([...m[1].matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(a => [a[1], a[2] ?? '']));
    const dataset = Object.fromEntries(Object.entries(attrs).filter(([k]) => k.startsWith('data-'))
      .map(([k, v]) => [k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase()), v]));
    return { attrs, dataset, value: attrs.value ?? '', checked: 'checked' in attrs };
  });
}
const matching = sel => {
  const m = sel.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
  return el => !!m && m[1] in el.attrs && (m[2] === undefined || el.attrs[m[1]] === m[2]);
};
const $ = sel => sel === '#onboard' ? root : inputs.find(matching(sel)) || null;
const $$ = sel => inputs.filter(matching(sel));
const box = id => $(`[data-ob-balance="${id}"]`);
const favour = id => $(`[data-favour="${id}"]`);
async function call(name, args) {
  if (name === 'accounts') return ACCOUNTS;
  if (name === 'set_balance') { out.calls.push(args); return ACCOUNTS; }
  throw new Error('unexpected call: ' + name);
}
function toast(message) { out.toasts.push(message); }
__PAGE__
(async () => {
  ob.step = 2;
  await renderOnboarding();
  out.html = root.innerHTML;
  inputs = drawn(out.html);
  __EDIT__
  renderOnboarding = async () => {};  // 4. Start isn't under test
  await onboardingAction({ dataset: { ob: 'save-balances' } });
  console.log(JSON.stringify(out));
})();
'''


def _balances_step(accounts, edit=""):
    """Node: the page draws onboarding's 3. Balances from `accounts` (the service's answer), `edit` runs (JS: what
    you'd change there, with box(id) and favour(id)), then Continue is clicked. Returns the step's HTML and the
    set_balance calls Continue made, to send to the service."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node")
    page = "\n".join(re.search(rf"^{pattern}", PAGE, re.S | re.M).group(0) for pattern in (
        r"const esc = [^\n]*$", r"const moneyPlain = [^\n]*$", r"const dayYear = [^\n]*$", r"const MAX_CENTS = [^\n]*$",
        r"function parseMoney\(.*?^}$", r"const favourBox = [^\n]*$", r"const inFavour = [^\n]*$",
        r"async function loadAccounts\(\) [^\n]*$", r"const ob = [^\n]*$",
        r"async function renderOnboarding\(\) \{.*?^}$", r"async function onboardingAction\(btn\) \{.*?^}$"))
    script = _BALANCES_STEP.replace("__ACCOUNTS__", json.dumps(accounts)).replace("__PAGE__", page).replace("__EDIT__", edit)
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)


def test_continue_on_3_balances_leaves_a_files_balance_and_its_check(app):
    """The way the page does it (#132's tests skipped this step): 3. Balances draws each box from `accounts`, says
    where its balance came from, and Continue saves every box that has an amount. Left as it is, nothing changes."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    got = _balances_step(call(app, "accounts"))
    assert "The balances from your files are filled in: change one only if it's wrong." in got["html"]
    assert "Balance on Sep 15, 2026, from your file" in got["html"] and "For the others" not in got["html"]
    for args in got["calls"]:
        call(app, "set_balance", **args)
    assert call(app, "undo_step") is None
    call(app, "finish_onboarding", "2026-09")
    _import(app, "chequing-2.ofx", ofx(GAP, "2600.00", "20261020"))
    assert [a["amount"] for a in _adjustments(app)] == [-30000] and _start(app) == 105000


def test_a_box_changed_on_3_balances_is_the_balance_on_its_rows_day(app):
    """Typed over a file's balance, the amount is for the day the row names, not the day you onboard. Unticking a
    card's In your favour there is remembered: its next file is read as what you owe though its balance disagrees."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    _import(app, "card-1.ofx", card_ofx(CARD_SEPTEMBER, "120.50", "20260930"))
    ids = {a["is_debt"]: a["id"] for a in call(app, "accounts")}
    got = _balances_step(call(app, "accounts"), f"box({ids[False]}).value = '2,950.00'; favour({ids[True]}).checked = false;")
    assert "Amount owing on Sep 30, 2026, from your file" in got["html"]
    for args in got["calls"]:
        call(app, "set_balance", **args)
    accounts = {a["id"]: a for a in call(app, "accounts")}
    chequing = accounts[ids[False]]
    assert (chequing["anchor_source"], chequing["anchor_date"], chequing["balance"]) == ("typed", "2026-09-15", 295000)
    call(app, "finish_onboarding", "2026-09")
    _import(app, "card-2.ofx", card_ofx(CARD_OCTOBER, "160.50", "20261010"))
    assert {a["id"]: a for a in call(app, "accounts")}[ids[True]]["balance"] == -16050


def test_3_balances_asks_what_the_bank_shows_now_for_an_account_without_one(app):
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    call(app, "import_files", [str(write(app.test_downloads, "Savings.CSV",
                                        bank_csv([("09/02/2026", "SAMPLE INTEREST", "SAMPLE INTEREST", "1.25")])))])
    accounts = call(app, "accounts")
    got = _balances_step(accounts)
    assert ("The balances from your files are filled in: change one only if it's wrong. "
            "For the others, enter what your bank shows now.") in got["html"]
    assert "Balance · transactions up to Sep 2, 2026" in got["html"]
    assert [c["account_id"] for c in got["calls"]] == [a["id"] for a in accounts if a["balance"] is not None]
