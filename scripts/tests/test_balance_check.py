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
from helpers import bank_csv, call, card_number, categories, write
from test_card_signs import card_ofx, covers

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE CU", start=None):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid). `start`: the first day the file says it
    covers (DTSTART, YYYYMMDD), if it says."""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n"
            f"{covers(start, as_of)}{trn}"
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


def test_reconciling_adds_an_adjustment_that_waits_and_the_next_file_is_still_checked(app):
    """Check balance… adds a balance adjustment for the difference, waiting in the Inbox. It doesn't end the check
    (issue #137): October's file, whose balance is older than it, is still checked, and its gap waits on its own."""
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 295000)["adjusted"] == -5000  # Oct 12
    [adjustment] = _adjustments(app)
    assert adjustment["guess"] is None and adjustment["reason"] == "To match the balance the bank shows"
    _second_file(app)
    assert sorted(a["amount"] for a in _adjustments(app)) == [-30000, -5000]
    assert _start(app) == 105000


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
    assert again.get_meta("schema_version") == "10"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v7-to-v10-*.db"))


def test_an_existing_database_keeps_where_each_file_says_it_starts(tmp_path):
    """Version 9 keeps the first day each file says it covers (issue #136). Imports from before don't know it: the
    check uses their first row's date, and what they recorded stays as it was."""
    path = tmp_path / "budget.db"
    database = db.Database(path)
    c = database.conn
    c.execute("INSERT INTO accounts (id, name, kind, anchor_date, anchor_balance, anchor_source) "
              "VALUES (1, 'Chequing', 'chequing', '2026-10-10', 500, 'file')")
    c.execute("INSERT INTO imports (id, filename, sha256, account_id, format, first_date, last_date, anchor_after_date, "
              "anchor_after_balance) VALUES (1, 'a.ofx', 'x', 1, 'ofx', '2026-09-01', '2026-10-09', '2026-10-10', 500)")
    c.execute("UPDATE accounts SET check_from = 1")
    for suffix in ("ins", "upd", "del"):  # the undo triggers name every column
        c.execute(f"DROP TRIGGER IF EXISTS undo_imports_{suffix}")
    c.execute("ALTER TABLE imports DROP COLUMN covers_from")  # a version 8 database
    c.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    c.commit()
    database.close()
    again = db.Database(path)
    old = again.conn.execute("SELECT * FROM imports").fetchone()
    assert (old["filename"], old["first_date"], old["anchor_after_date"], old["covers_from"]) == \
        ("a.ofx", "2026-09-01", "2026-10-10", None)
    assert again.conn.execute("SELECT anchor_source, check_from FROM accounts").fetchone()[:] == ("file", 1)
    assert again.get_meta("schema_version") == "10"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v8-to-v10-*.db"))


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


# ── older history (issue #136) ──────────────────────────────────────────────
#
# A file reaching back before the first day of the file the check starts from brings older history: its rows dated
# before that day were in the starting balance already, not money that moved since.

AUGUST = [("20260805", "1000.00", "PAYROLL SAMPLE CO", "B1"), ("20260820", "-200.00", "SAMPLE GROCER CALGARY AB", "B2")]


def test_an_older_file_imported_later_was_in_the_starting_balance_already(app):
    """The issue's case: August's file after September's counted as money that moved since Sep 15, and a −$800.00
    balance adjustment waited for good (the budget's starting balance moving with it)."""
    _first_file(app)
    call(app, "set_budget_start", "2026-08")
    _import(app, "chequing-aug.ofx", ofx(AUGUST, "1050.00", "20260831"))
    assert _adjustments(app) == []
    assert call(app, "month", "2026-08")["summary"]["carried_rta"] == 25000  # the bank's balance on Jul 31
    _import(app, "chequing-oct.ofx", ofx([("20260920", "-100.00", "SAMPLE HARDWARE CALGARY AB", "C1")], "2900.00",
                                         "20261010"))
    assert _adjustments(app) == []


def test_an_older_file_after_a_starting_file_without_rows(app):
    """A quiet month's file (a balance, no transactions) can be the one the check starts from: without rows or a
    first day of its own it covers from its balance's date, and an older file after it adds nothing."""
    _import(app, "chequing-quiet.ofx", ofx([], "1050.00", "20260831"))
    call(app, "finish_onboarding", "2026-08")
    _import(app, "chequing-aug.ofx", ofx(AUGUST, "1050.00", "20260820"))
    assert _adjustments(app) == []
    assert call(app, "month", "2026-08")["summary"]["carried_rta"] == 25000


# A card's charge is dated when it's made, but its file covers the days things posted: the card's Oct 1–3 file
# has a Sep 30 taxi that posted Oct 1.
CARD_START = [("20260930", "-20.00", "SAMPLE TAXI", "K1"), ("20261002", "-30.00", "SAMPLE BAKERY", "K2")]


def test_a_card_charge_dated_before_the_starting_files_first_day_that_posted_after_it_counts(app):
    """The next file (Oct 1–12: it starts on Oct 1 too, not before) brings a Sep 29 parking charge that posted
    Oct 4. The Oct 3 balance doesn't have it, so it counts. By the starting file's first row (Sep 30) it wouldn't,
    and −$15.00 would wait for good."""
    _import(app, "card-1.ofx", card_ofx(CARD_START, "-50.00", "20261003", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    later = CARD_START + [("20260929", "-15.00", "SAMPLE PARKING", "K3"), ("20261010", "-40.00", "SAMPLE GROCER", "K4")]
    _import(app, "card-2.ofx", card_ofx(later, "-105.00", "20261012", start="20261001"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -10500


# September's charges, which posted in September: the card's Oct 1–3 balance ($250.00 owing) already has them.
CARD_SEPTEMBER_POSTED = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260930", "-25.00", "SAMPLE CAFE", "S2")]


def test_a_long_card_file_reaching_back_doesnt_count_what_the_starting_balance_had(app):
    """A 90-day file (Jul 15 – Oct 20) after the Oct 1–3 one: its Sep 30 coffee is dated on or after the starting
    file's first row (the Sep 30 taxi), but it posted Sep 30, so the Oct 3 balance had it. Only its Oct 15 charge is
    new."""
    _import(app, "card-oct.ofx", card_ofx(CARD_START, "-250.00", "20261003", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    long = CARD_SEPTEMBER_POSTED + CARD_START + [("20261015", "-10.00", "SAMPLE NEWS", "S3")]
    _import(app, "card-90.ofx", card_ofx(long, "-260.00", "20261020", start="20260715"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -26000


def test_the_cards_previous_months_file_adds_nothing(app):
    _import(app, "card-oct.ofx", card_ofx(CARD_START, "-250.00", "20261003", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    _import(app, "card-sep.ofx", card_ofx(CARD_SEPTEMBER_POSTED, "-200.00", "20260930", start="20260901"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -25000


def test_a_starting_file_without_rows_covers_from_the_day_it_says(app):
    """The card's Oct 1–2 file has no charges yet (a balance only). The next file brings a charge made Oct 1 that
    posted Oct 4: the Oct 2 balance doesn't have it, so it counts, though it's dated before that balance."""
    _import(app, "card-1.ofx", card_ofx([], "-200.00", "20261002", start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    _import(app, "card-2.ofx", card_ofx([("20261001", "-15.00", "SAMPLE PARKING", "K3")], "-215.00", "20261012",
                                        start="20261001"))
    assert _adjustments(app) == []


def test_a_first_day_after_the_files_own_balance_says_nothing(app):
    """A bank writing nonsense: a file "starting" Oct 20 whose balance is for Oct 3. It covers from its first row's
    date (Sep 30) instead, and the next file's charges count."""
    _import(app, "card-1.ofx", card_ofx(CARD_START, "-50.00", "20261003", start="20261020"))
    call(app, "finish_onboarding", "2026-10")
    later = CARD_START + [("20261010", "-40.00", "SAMPLE GROCER", "K4")]
    _import(app, "card-2.ofx", card_ofx(later, "-90.00", "20261012", start="20261001"))
    assert _adjustments(app) == []


def test_merging_in_a_copy_with_older_history_adds_nothing(app):
    _first_file(app)
    # August's file went into an account of its own (a new account number, say).
    path = write(app.test_downloads, "copy.ofx", ofx(AUGUST, "1050.00", "20260831", acct_id="000999888777",
                                                      start="20260801"))
    call(app, "set_file_accounts", str(path), [{"index": 0, "name": "Copy"}])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    out = call(app, "merge_accounts", accounts["Copy"], accounts["SAMPLE CU Chequing"])
    assert (out["moved"], out["dropped"]) == (2, 0)
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 300000


def test_removing_the_starting_file_checks_from_the_next_files_first_day(app):
    """With the file the check starts from removed, the check starts from the next file's balance, and August's
    file, older than that one too, still adds nothing."""
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261010", start="20260916"))
    _import(app, "chequing-aug.ofx", ofx(AUGUST, "1050.00", "20260831", start="20260801"))
    assert _adjustments(app) == []
    first = next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-1.ofx")
    call(app, "undo_import", first)
    assert _adjustments(app) == []


# ── Check balance… (issue #137) ──────────────────────────────────────────────
#
# Check balance… doesn't end the check. Its balance adjustment stands for what's happened since the newest file's
# balance: the check leaves it alone until a file whose balance is as new as it comes in after it, which takes it
# over (it goes, or what that file doesn't explain stays as the account's balance adjustment). Categorized, it's
# yours, and counts as one you kept.


def test_a_waiting_adjustment_still_goes_after_a_reconcile(app):
    """The issue's case: Check balance… ended the check, so the −$300.00 was never worked out again, and once the
    missing September file arrived it counted twice (September started at $1,350.00)."""
    _first_file(app)
    _second_file(app)  # −$300.00 waits
    account = call(app, "accounts")[0]["id"]
    call(app, "reconcile", account, 259000)  # Oct 12: the bank shows $10.00 less
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
    assert [a["amount"] for a in _adjustments(app)] == [-1000] and _start(app) == 105000


# October's file without the gap: the bank's $2,600.00 on Oct 10. Check balance… on Oct 12 then finds the bank at
# $2,590.00: two charges from Oct 11 that posted after that file.
LATE = [("20261011", "-4.00", "SAMPLE PARKING CALGARY AB", "P1"), ("20261011", "-6.00", "SAMPLE COFFEE CALGARY AB", "P2")]
LATER = [("20261015", "-20.00", "SAMPLE BAKERY CALGARY AB", "P3")]


def _checked_on_oct_12(app):
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261010"))
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -1000
    return account


def test_a_later_file_that_explains_a_check_balance_adjustment_takes_it_away(app, monkeypatch):
    _checked_on_oct_12(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    out = _import(app, "chequing-3.ofx", ofx(LATE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000
    assert out["results"][0]["balance_note"] == "The missing transactions arrived: the balance adjustment is gone"


def test_an_existing_database_gets_a_place_to_remember_what_a_check_balance_came_after(tmp_path):
    """Version 10 remembers, on a balance adjustment from Check balance…, the newest import whose balance it came
    after (issue #137). Everything else stays as it was."""
    path = tmp_path / "budget.db"
    database = db.Database(path)
    c = database.conn
    c.execute("INSERT INTO accounts (id, name, kind, anchor_date, anchor_balance, anchor_source) "
              "VALUES (1, 'Chequing', 'chequing', '2026-10-10', 500, 'file')")
    c.execute("INSERT INTO transactions (id, account_id, date, amount, hint, reason) "
              "VALUES (1, 1, '2026-10-12', -1000, 'adjustment', 'To match the balance the bank shows')")
    for suffix in ("ins", "upd", "del"):  # the undo triggers name every column
        c.execute(f"DROP TRIGGER IF EXISTS undo_transactions_{suffix}")
    c.execute("ALTER TABLE transactions DROP COLUMN after_import")  # a version 9 database
    c.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    c.commit()
    database.close()
    again = db.Database(path)
    old = again.conn.execute("SELECT * FROM transactions").fetchone()
    assert (old["date"], old["amount"], old["hint"], old["after_import"]) == ("2026-10-12", -1000, "adjustment", None)
    assert again.get_meta("schema_version") == "10"
    assert list((tmp_path / "I Need A Budget Backups").glob("budget-before-upgrade-v9-to-v10-*.db"))


def test_a_later_file_that_doesnt_explain_it_leaves_the_rest_as_the_accounts_balance_adjustment(app, monkeypatch):
    """The two charges are in no file, but the bank's balance on Oct 20 has them: the check takes the −$10.00 over,
    and it waits as the account's balance adjustment, dated that balance's date."""
    _checked_on_oct_12(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(LATER, "2570.00", "20261020"))
    [adjustment] = _adjustments(app)
    assert (adjustment["amount"], adjustment["date"]) == (-1000, "2026-10-20")
    assert "By the bank's balance on Oct 20, $10.00 more went out" in adjustment["reason"]
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_a_categorized_check_balance_adjustment_asks_when_its_money_arrives(app, monkeypatch):
    _checked_on_oct_12(app)
    income = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=income)  # yours now
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(LATE + LATER, "2570.00", "20261020"))
    [question] = _adjustments(app)  # the app's, cancelling yours out
    assert question["amount"] == 1000 and question["adjustment_question"]["ids"] == [mine["id"]]
    assert question["reason"] == "The $10.00 your balance adjustment stood for has arrived: remove the adjustment?"
    assert _start(app) == 105000
    call(app, "remove_adjustment", question["id"])
    assert _adjustments(app) == [] and mine["id"] not in [t["id"] for t in call(app, "transactions", {})["items"]]
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_check_balance_on_the_day_of_the_last_file_changes_the_balance_not_the_start(app):
    """Every real Quicken file's balance is dated the day it was downloaded. Check balance… that day was in that
    balance: the balance stayed $2,600.00, September's start moved to $1,060.00, and each Match it added another."""
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261012"))  # downloaded today
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -1000
    assert call(app, "accounts")[0]["balance"] == 259000 and _start(app) == 105000
    assert call(app, "reconcile", account, 259000)["adjusted"] == 0  # "It already matches"
    assert [a["amount"] for a in _adjustments(app)] == [-1000]


def test_a_later_file_the_same_day_takes_it_over(app):
    _first_file(app)
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER, "2600.00", "20261012"))
    account = call(app, "accounts")[0]["id"]
    call(app, "reconcile", account, 259000)
    _import(app, "chequing-3.ofx", ofx(MISSING + OCTOBER + LATE, "2590.00", "20261012"))  # downloaded again that day
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 259000 and _start(app) == 105000


def test_check_balance_on_the_day_of_a_typed_balance(app):
    """The same for a balance you typed: the bank's balance later that day is that day's, and the days before
    stay as they were."""
    account = call(app, "add_account", "Sample Savings", "savings", 100000, "2026-10-12")
    call(app, "finish_onboarding", "2026-10")
    start = call(app, "month", "2026-10")["summary"]["carried_rta"]
    assert call(app, "reconcile", account, 99000)["adjusted"] == -1000
    assert call(app, "accounts")[0]["balance"] == 99000
    assert call(app, "month", "2026-10")["summary"]["carried_rta"] == start
    assert call(app, "reconcile", account, 99000)["adjusted"] == 0


def test_the_file_that_starts_the_check_takes_a_check_balance_adjustment_over(app, monkeypatch):
    """An account whose balance isn't checked (here a typed one): Check balance… finds the bank at $2,590.00 on
    Oct 12, −$110.00 from what the app has (the Oct 3 charge and the two from Oct 11, in no file yet). The next file's
    balance replaces the typed one and starts the check, so the app's adjustment goes: that file's balance has it."""
    _first_file(app)
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 270000, "2026-09-30")  # typed: the check ends
    assert call(app, "reconcile", account, 259000)["adjusted"] == -11000
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    out = _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000
    assert out["results"][0]["balance_note"] == "The missing transactions arrived: the balance adjustment is gone"


def test_a_changed_box_on_3_balances_takes_the_apps_waiting_adjustment_away(app):
    """At onboarding the Sep 1–15 and October files disagree (−$300.00 waiting), and October's box is changed to
    $2,590.00. The balance you typed stands for everything, so the −$300.00 goes: September starts at $740.00 until
    the missing file arrives, then at $1,040.00, as the balance you typed says (it stayed, and counted twice:
    $1,340.00)."""
    _import(app, "chequing-1.ofx", ofx(SEPTEMBER, "3000.00", "20260915"))
    _import(app, "chequing-2.ofx", ofx(OCTOBER, "2600.00", "20261010"))
    assert [a["amount"] for a in _adjustments(app)] == [-30000]
    account = call(app, "accounts")[0]
    call(app, "set_balance", account_id=account["id"], cents=259000, date=account["balance_date"])
    call(app, "finish_onboarding", "2026-09")
    assert _adjustments(app) == [] and _start(app) == 74000
    _import(app, "chequing-sep.ofx", ofx(MISSING, "2700.00", "20260930"))
    assert _adjustments(app) == [] and _start(app) == 104000


def test_a_changed_balance_leaves_a_balance_adjustment_you_categorized(app):
    _first_file(app)
    _second_file(app)
    income = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=income)
    account = call(app, "accounts")[0]
    call(app, "set_balance", account_id=account["id"], cents=259000, date=account["balance_date"])
    assert mine["id"] in [t["id"] for t in call(app, "transactions", {})["items"]]


ONE_CHARGE = [("20261011", "-10.00", "SAMPLE PARKING CALGARY AB", "F1")]  # what the bank had on Oct 12, as one row


def test_the_banks_row_for_the_same_money_takes_a_check_balance_adjustments_place_under_its_own_name(app, monkeypatch):
    """A row of the same amount within 3 days takes the adjustment's place, as for a transaction you added by hand.
    It's the bank's transaction now, so it has the bank's name: it was left as "Balance adjustment"."""
    _checked_on_oct_12(app)
    [adjustment] = _adjustments(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(ONE_CHARGE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    row = next(t for t in call(app, "transactions", {})["items"] if t["id"] == adjustment["id"])
    assert (row["payee_name"], row["raw_name"], row["date"]) == ("Sample Parking", "SAMPLE PARKING CALGARY AB", "2026-10-11")
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_the_banks_row_keeps_the_category_you_gave_the_adjustment(app, monkeypatch):
    _checked_on_oct_12(app)
    income = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["name"] == "Income")
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=income)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(ONE_CHARGE + LATER, "2570.00", "20261020"))
    row = next(t for t in call(app, "transactions", {})["items"] if t["id"] == mine["id"])
    assert (row["payee_name"], row["category_id"], row["status"]) == ("Sample Parking", income, "done")
    assert _adjustments(app) == [] and _start(app) == 105000


def test_undoing_check_balance_puts_the_balance_back(app):
    """Check balance… is a small step: undone straight away, the day's balance it changed included."""
    account = call(app, "add_account", "Sample Savings", "savings", 100000, "2026-10-12")
    call(app, "finish_onboarding", "2026-10")
    assert call(app, "reconcile", account, 99000)["adjusted"] == -1000
    assert call(app, "undo")["undone"] == "Balance adjustment"
    assert call(app, "accounts")[0]["balance"] == 100000 and _adjustments(app) == []


def test_removing_the_file_that_took_it_over_doesnt_bring_it_back(app, monkeypatch):
    """Accepted (decided on #137): the check works the difference out again from the files' balances. If the bank
    still shows something else, Check balance… again."""
    _checked_on_oct_12(app)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(LATE + LATER, "2570.00", "20261020"))
    call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-3.ofx"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 260000 and _start(app) == 105000


def test_removing_the_file_a_check_balance_came_after_still_lets_the_next_file_take_it_over(app, monkeypatch):
    """Remove import of October's file after Check balance…: the next file whose balance is as new as it still takes
    the adjustment over, though that file gets the removed one's number."""
    _checked_on_oct_12(app)
    call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-2.ofx"))
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-3.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_a_cards_check_balance_adjustment_goes_when_the_next_file_brings_the_charges(app, monkeypatch):
    """A card: you owe $500.00 on Oct 10 by its file, and $530.00 by the bank on Oct 12 (two charges made Oct 11 that
    posted after that file). The Oct 1–20 file brings them, so the −$30.00 goes."""
    _import(app, "card-1.ofx", card_ofx([("20261005", "-500.00", "SAMPLE GROCER", "C1")], "-500.00", "20261010",
                                        start="20261001"))
    call(app, "finish_onboarding", "2026-10")
    card = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", card, 53000)["adjusted"] == -3000
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    later = [("20261005", "-500.00", "SAMPLE GROCER", "C1"), ("20261011", "-12.00", "SAMPLE CAFE", "C2"),
             ("20261011", "-18.00", "SAMPLE TAXI", "C4"), ("20261015", "-20.00", "SAMPLE BAKERY", "C3")]
    _import(app, "card-2.ofx", card_ofx(later, "-550.00", "20261020", start="20261001"))
    assert _adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == -55000


def test_a_categorized_adjustment_asks_when_the_file_that_starts_the_check_brings_its_money(app, monkeypatch):
    """An account whose balance isn't checked yet (a typed balance here; at the first launch of 1.4 every account whose
    balance came from a file, too): Check balance… on Oct 12 finds −$110.00 (the Oct 3 charge and the two from Oct 11),
    and you categorize it. The next file's balance starts the check, and that file brings the charges. It asks, as on an
    account already checked: it counted twice instead, and September started at $1,160.00."""
    _first_file(app)
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 270000, "2026-09-30")  # typed: the check ends
    assert call(app, "reconcile", account, 259000)["adjusted"] == -11000
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=food)  # yours now
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    [question] = _adjustments(app)  # the app's, cancelling yours out
    assert question["amount"] == 11000 and question["adjustment_question"]["ids"] == [mine["id"]]
    assert question["reason"] == "The $110.00 your balance adjustment stood for has arrived: remove the adjustment?"
    assert _start(app) == 105000
    call(app, "remove_adjustment", question["id"])
    assert _adjustments(app) == [] and mine["id"] not in [t["id"] for t in call(app, "transactions", {})["items"]]
    assert call(app, "accounts")[0]["balance"] == 257000 and _start(app) == 105000


def test_removing_the_file_the_check_starts_from_still_asks_about_yours(app, monkeypatch):
    """Yours (−$410.00 from Oct 12) is asked about once the Oct 20 file brings its money. Remove import of the Sep
    file, the one the check started from: the check now starts from the Oct 20 file, whose balance has yours in it, so
    it still asks (the question went, and yours counted twice)."""
    _first_file(app)
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -41000
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=food)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020"))
    call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == "chequing-1.ofx"))
    [question] = _adjustments(app)
    assert question["amount"] == 41000 and question["adjustment_question"]["ids"] == [mine["id"]]

# ── a check that starts again (#145's review) ─────────────────────────────────
#
# Merge into… a copy whose newer balance wins, or Set balance and then the next file, start the check again from a
# newer balance. Every balance adjustment inside it from before is taken over alike: yours and the one you kept
# with Keep both still cancel out, and a question waiting about yours stays one question.


N1 = [("20261025", "-5.00", "SAMPLE CAFE CALGARY AB", "N1")]


def _yours_whose_money_arrived(app, monkeypatch):
    """Checked from the Sep file: Check balance… on Oct 12 finds −$410.00 (the late-September payments and October's,
    in no file yet), categorized; the Oct 20 file brings them, so the app asks whether to remove yours."""
    _first_file(app)
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -41000
    [mine] = _adjustments(app)
    call(app, "categorize", txn_ids=[mine["id"]], category_id=food)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    _import(app, "chequing-2.ofx", ofx(MISSING + OCTOBER + LATE + LATER, "2570.00", "20261020", start="20260916"))
    [question] = _adjustments(app)
    assert question["adjustment_question"]["ids"] == [mine["id"]] and _start(app) == 105000
    return question


def _merge_in_a_newer_copy(app, monkeypatch):
    """The account's Oct 13–31 download went into an account of its own (another number); Merge into…: its newer
    balance wins, and its check with it."""
    monkeypatch.setenv("INAB_TODAY", "2026-11-01")
    path = write(app.test_downloads, "copy.ofx", ofx(N1, "2565.00", "20261031", acct_id="000999888777", start="20261013"))
    call(app, "set_file_accounts", str(path), [{"index": 0, "name": "Copy"}])
    accounts = {a["name"]: a["id"] for a in call(app, "accounts")}
    call(app, "merge_accounts", accounts["Copy"], accounts["SAMPLE CU Chequing"])


def test_keep_both_still_holds_after_a_newer_copy_is_merged_in(app, monkeypatch):
    """You answered Keep both: yours and the one you kept cancel out. After the merge nothing asks again (#145: the app
    asks about yours a second time, and September starts at $640.00 whatever you answer)."""
    question = _yours_whose_money_arrived(app, monkeypatch)
    call(app, "keep_both", question["id"])
    _merge_in_a_newer_copy(app, monkeypatch)
    assert [(a["amount"], a["yours"], bool(a.get("adjustment_question"))) for a in _adjustments(app)] == [(41000, True, False)]
    assert _start(app) == 105000 and call(app, "accounts")[0]["balance"] == 256500


def test_keep_both_still_holds_after_set_balance_and_the_next_file(app, monkeypatch):
    """The same with Set balance (a changed balance ends the check; the next file starts it again). In the page only
    onboarding's 3. Balances can change a balance an account already has, so this one is reachable through the API only."""
    question = _yours_whose_money_arrived(app, monkeypatch)
    call(app, "keep_both", question["id"])
    account = call(app, "accounts")[0]["id"]
    call(app, "set_balance", account, 257500, "2026-10-20")  # typed, $5.00 out: the check ends
    monkeypatch.setenv("INAB_TODAY", "2026-11-01")
    _import(app, "chequing-3.ofx", ofx(N1, "2565.00", "20261031", start="20261021"))
    assert [(a["amount"], a["yours"], bool(a.get("adjustment_question"))) for a in _adjustments(app)] == [(41000, True, False)]
    assert _start(app) == 105000


def test_a_question_waiting_when_a_newer_copy_is_merged_in_counts_once(app, monkeypatch):
    """The question about yours is still waiting at the merge: its money counts once (#145: a second +$410.00 joins it,
    and September starts at $640.00)."""
    _yours_whose_money_arrived(app, monkeypatch)
    _merge_in_a_newer_copy(app, monkeypatch)
    assert [a["amount"] for a in _adjustments(app) if not a["yours"]] == [41000]
    assert _start(app) == 105000
