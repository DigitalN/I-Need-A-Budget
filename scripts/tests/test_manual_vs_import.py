# SPDX-License-Identifier: GPL-3.0-only
"""A transaction added by hand is the same one the bank's file brings later, not a second one.
Made-up data only."""

from helpers import call, categories, write


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE BANK"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _id(added):
    return added["id"]


def _account(app):
    first = write(app.test_downloads, "chq-1.ofx", ofx([("20261001", "2000.00", "PAYROLL DEPOSIT SAMPLE CO", "C1")],
                                                       "2000.00", "20261001"))
    call(app, "import_files", [str(first)])
    call(app, "finish_onboarding", "2026-10")
    return call(app, "accounts")[0]["id"]


def _of(app, amount):
    return [t for t in call(app, "transactions", {})["items"] if t["amount"] == amount]


def _import(app, rows, balance="1920.00", as_of="20261031", name="chq-2.ofx"):
    return call(app, "import_files", [str(write(app.test_downloads, name, ofx(rows, balance, as_of)))])["results"][0]


def test_a_hand_added_transaction_isnt_imported_again(app):
    account = _account(app)
    call(app, "add_transaction", account, "2026-10-05", "SAMPLE GROCER", -8000)
    result = _import(app, [("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")], as_of="20261006")
    assert (result["added"], result["duplicates"]) == (0, 1)
    assert len(_of(app, -8000)) == 1


def test_the_hand_added_one_takes_the_banks_details_and_keeps_what_you_decided(app):
    ids = categories(app, {"Everyday": ["Food"]})
    account = _account(app)
    mine = _id(call(app, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000, ids["Food"], "for the party"))
    result = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    assert result["added"] == 0
    [row] = _of(app, -8000)
    assert row["id"] == mine
    assert (row["date"], row["raw_name"]) == ("2026-10-06", "SAMPLE GROCER CALGARY AB")  # the bank's
    assert (row["category_id"], row["note"], row["status"], row["payee_name"]) == (ids["Food"], "for the party", "done", "Sample Grocer")
    assert row["manual"] is False  # it has the bank's fingerprint and ID now
    # The same file again: nothing new, nothing matched twice.
    again = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")], name="chq-3.ofx")
    assert (again["added"], again["duplicates"]) == (0, 1) and len(_of(app, -8000)) == 1


def test_two_hand_added_a_day_apart_each_match_their_own_row(app):
    account = _account(app)
    first = _id(call(app, "add_transaction", account, "2026-10-05", "Sample Grocer", -8000))
    second = _id(call(app, "add_transaction", account, "2026-10-06", "Sample Grocer", -8000))
    result = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2"),
                           ("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C3")])
    assert (result["added"], result["duplicates"]) == (0, 2)
    rows = {t["id"]: t for t in _of(app, -8000)}
    assert set(rows) == {first, second}
    assert (rows[first]["date"], rows[second]["date"]) == ("2026-10-05", "2026-10-06")


def test_a_hand_added_row_five_days_away_or_of_another_amount_isnt_matched(app):
    account = _account(app)
    call(app, "add_transaction", account, "2026-10-01", "Sample Grocer", -8000)
    call(app, "add_transaction", account, "2026-10-05", "Sample Grocer", -7999)
    result = _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    assert (result["added"], result["duplicates"]) == (1, 0)
    assert len(_of(app, -8000)) == 2


def test_a_hand_added_row_in_another_account_isnt_matched(app):
    _account(app)
    wallet = call(app, "add_account", "Wallet", "cash", 10000, "2026-09-30")
    call(app, "add_transaction", wallet, "2026-10-05", "Sample Grocer", -8000)
    result = _import(app, [("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    assert result["added"] == 1 and len(_of(app, -8000)) == 2


def test_the_banks_id_already_on_another_row_doesnt_break_the_match(app):
    """Banks repeat IDs (PR #90): the hand-added row takes the bank's details, but not an ID the
    account already has on another row, which is unique there."""
    account = _account(app)  # its payroll row has the ID C1
    mine = _id(call(app, "add_transaction", account, "2026-10-05", "SAMPLE GROCER", -8000))
    result = _import(app, [("20261005", "-80.00", "SAMPLE GROCER CALGARY AB", "C1")], as_of="20261006")
    assert (result["added"], result["duplicates"]) == (0, 1)
    [row] = _of(app, -8000)
    assert row["id"] == mine and row["raw_name"] == "SAMPLE GROCER CALGARY AB"


# ── Remove import puts back what the file's row took the place of (issue #149) ───────────────
#
# A transaction you added by hand, or a balance adjustment from Check balance…, whose place a file's row took (the same
# amount within 3 days): Remove import of the last file that has it puts it back as it was before that file came in,
# keeping what you decided. Not deleted, and not left with the bank's date, which can fall inside an older balance.

import db  # noqa: E402
import pytest  # noqa: E402
import test_balance_check as check  # noqa: E402


def _txn(app, txn_id):
    return next((t for t in call(app, "transactions", {})["items"] if t["id"] == txn_id), None)


def _remove(app, name):
    return call(app, "undo_import", next(i["id"] for i in call(app, "imports") if i["filename"] == name))


def test_removing_the_file_puts_back_the_transaction_you_added_by_hand(app):
    """The issue's case: it was deleted, your category and note with it."""
    ids = categories(app, {"Everyday": ["Food"]})
    account = _account(app)
    mine = _id(call(app, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000, ids["Food"], "for the party"))
    _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    out = _remove(app, "chq-2.ofx")
    assert [t["id"] for t in _of(app, -8000)] == [mine]
    row = _txn(app, mine)
    assert (row["date"], row["raw_name"], row["payee_name"]) == ("2026-10-04", "", "Sample Grocer")  # as you entered it
    assert (row["category_id"], row["note"], row["status"], row["manual"]) == (ids["Food"], "for the party", "done", True)
    assert (out["removed"], out["stayed"], out["restored"]) == (0, 0, 1)


def test_one_whose_bank_date_falls_inside_the_older_balance_goes_back_after_it(app, monkeypatch):
    """Chequing's Oct 10 file says $2,600.00. You add −$80.00 on Oct 11; the bank dates it Oct 10, and the Oct 20 file
    brings it. With the bank's date it would count inside Oct 10's balance: $2,600.00 and a start of $1,130.00."""
    check._first_file(app)
    check._import(app, "chequing-2.ofx", check.ofx(check.MISSING + check.OCTOBER, "2600.00", "20261010"))
    account = call(app, "accounts")[0]["id"]
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    mine = _id(call(app, "add_transaction", account, "2026-10-11", "Sample Grocer", -8000, food))
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    check._import(app, "chequing-3.ofx", check.ofx([("20261010", "-80.00", "SAMPLE GROCER CALGARY AB", "G1")],
                                                   "2520.00", "20261020"))
    assert _txn(app, mine)["date"] == "2026-10-10"
    _remove(app, "chequing-3.ofx")
    assert _txn(app, mine)["date"] == "2026-10-11"
    assert call(app, "accounts")[0]["balance"] == 252000 and check._start(app) == 105000
    assert check._adjustments(app) == []


def test_the_removed_file_imported_again_takes_its_place_again(app):
    account = _account(app)
    mine = _id(call(app, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000))
    rows = [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")]
    _import(app, rows)
    _remove(app, "chq-2.ofx")
    again = _import(app, rows, name="chq-3.ofx")
    assert (again["added"], again["duplicates"]) == (0, 1)
    assert [t["id"] for t in _of(app, -8000)] == [mine] and _txn(app, mine)["date"] == "2026-10-06"
    _remove(app, "chq-3.ofx")
    assert [t["id"] for t in _of(app, -8000)] == [mine] and _txn(app, mine)["date"] == "2026-10-04"


def test_what_you_changed_after_the_import_stays(app):
    ids = categories(app, {"Everyday": ["Food", "Gifts"]})
    account = _account(app)
    mine = _id(call(app, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000, ids["Food"]))
    _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    call(app, "update_transaction", mine, {"payee": "Sample Party Shop", "note": "cake"})
    call(app, "split_transaction", mine, [{"category_id": ids["Food"], "amount": -5000},
                                          {"category_id": ids["Gifts"], "amount": -3000}])
    _remove(app, "chq-2.ofx")
    row = _txn(app, mine)
    assert (row["date"], row["payee_name"], row["note"], row["is_split"]) == ("2026-10-04", "Sample Party Shop", "cake", 1)


def test_one_you_left_without_a_name_goes_back_without_one(app):
    account = _account(app)
    mine = _id(call(app, "add_transaction", account, "2026-10-04", "", -8000))
    _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    assert _txn(app, mine)["payee_name"] == "Sample Grocer"  # the bank's, while the file has it
    _remove(app, "chq-2.ofx")
    assert (_txn(app, mine)["payee_name"] or "", _txn(app, mine)["raw_name"]) == ("", "")


def test_undoing_remove_import_puts_the_banks_row_back_in_its_place(app):
    account = _account(app)
    mine = _id(call(app, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000, None, "for the party"))
    _import(app, [("20261006", "-80.00", "SAMPLE GROCER CALGARY AB", "C2")])
    before = _txn(app, mine)
    _remove(app, "chq-2.ofx")
    call(app, "undo")
    assert _txn(app, mine) == before
    _remove(app, "chq-2.ofx")  # and again, after the undo
    assert _txn(app, mine)["date"] == "2026-10-04"


def _parking_on_the_file_day(app, monkeypatch, categorize=True):
    """Check balance… on Oct 12, the day of the Oct 12 file's $2,600.00, with $2,590.00: −$10.00, put in Parking. The Oct
    20 file's −$10.00 parking charge, dated Oct 12, takes its place."""
    check._first_file(app)
    check._import(app, "chequing-2.ofx", check.ofx(check.MISSING + check.OCTOBER, "2600.00", "20261012"))
    account = call(app, "accounts")[0]["id"]
    assert call(app, "reconcile", account, 259000)["adjusted"] == -1000
    parking = categories(app, {"Everyday": ["Parking"]})["Parking"]
    [adjustment] = check._adjustments(app)
    if categorize:
        call(app, "categorize", txn_ids=[adjustment["id"]], category_id=parking)
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")
    check._import(app, "chequing-3.ofx", check.ofx(PARKING + check.LATER, "2570.00", "20261020"))
    assert check._adjustments(app) == [] and _txn(app, adjustment["id"])["payee_name"] == "Sample Parking"
    return adjustment["id"], parking


PARKING = [("20261012", "-10.00", "SAMPLE PARKING CALGARY AB", "F1")]


def test_a_check_balance_adjustment_you_categorized_comes_back(app, monkeypatch):
    """It was deleted: the balance said $2,600.00 and Parking lost its $10.00. Kept with the bank's date, it would count
    inside Oct 12's balance and the start would move to $1,060.00."""
    mine, parking = _parking_on_the_file_day(app, monkeypatch)
    out = _remove(app, "chequing-3.ofx")
    row = _txn(app, mine)
    assert (row["payee_name"], row["date"], row["amount"], row["category_id"]) == ("Balance adjustment", "2026-10-12",
                                                                                  -1000, parking)
    assert call(app, "accounts")[0]["balance"] == 259000 and check._start(app) == 105000
    assert out["restored"] == 1
    # The same file again: the parking charge takes its place again, and nothing waits.
    check._import(app, "chequing-3.ofx", check.ofx(PARKING + check.LATER, "2570.00", "20261020"))
    assert _txn(app, mine)["payee_name"] == "Sample Parking" and check._adjustments(app) == []
    assert call(app, "accounts")[0]["balance"] == 257000 and check._start(app) == 105000


@pytest.mark.parametrize("order", [("chequing-3.ofx", "chequing-1.ofx"), ("chequing-1.ofx", "chequing-3.ofx")])
def test_removing_the_file_the_check_starts_from_as_well(app, monkeypatch, tmp_path, order):
    """With the put-back Check balance… adjustment there, in either order: as if neither file had come in (the check
    starts again from the Oct 12 file, and the adjustment, made after it, is still yours, in Parking)."""
    mine, parking = _parking_on_the_file_day(app, monkeypatch)
    for name in order:
        _remove(app, name)
    replay = check._service(tmp_path / "replay")
    check._import(replay, "chequing-2.ofx", check.ofx(check.MISSING + check.OCTOBER, "2600.00", "20261012"))
    call(replay, "finish_onboarding", "2026-09")
    monkeypatch.setenv("INAB_TODAY", "2026-10-12")
    call(replay, "reconcile", call(replay, "accounts")[0]["id"], 259000)
    [theirs] = check._adjustments(replay)
    call(replay, "categorize", txn_ids=[theirs["id"]],
         category_id=categories(replay, {"Everyday": ["Parking"]})["Parking"])
    monkeypatch.setenv("INAB_TODAY", "2026-10-21")

    def seen(service, txn_id):
        row = _txn(service, txn_id)
        return (call(service, "accounts")[0]["balance"], check._start(service),
                [(a["amount"], a["date"]) for a in check._adjustments(service)],
                (row["payee_name"], row["date"], row["amount"]))
    assert seen(app, mine) == seen(replay, theirs["id"])
    assert _txn(app, mine)["category_id"] == parking


def test_the_apps_own_check_balance_adjustment_comes_back_waiting(app, monkeypatch):
    """Not categorized: it comes back to the Inbox as it was, even after the app categorized the bank's row."""
    mine, parking = _parking_on_the_file_day(app, monkeypatch, categorize=False)
    with app.db.write() as c:  # what the re-check does with a payee it's sure of
        c.execute("UPDATE transactions SET category_id = ?, status = 'done', reason = 'Usually Parking' WHERE id = ?",
                  (parking, mine))
    _remove(app, "chequing-3.ofx")
    [adjustment] = check._adjustments(app)
    assert (adjustment["id"], adjustment["amount"], adjustment["date"]) == (mine, -1000, "2026-10-12")
    with app.db.read() as c:
        row = c.execute("SELECT * FROM transactions WHERE id = ?", (mine,)).fetchone()
    assert (row["category_id"], row["status"], row["by_you"]) == (None, "review", 0)
    assert row["reason"] == "To match the balance the bank shows"
    assert call(app, "accounts")[0]["balance"] == 259000 and check._start(app) == 105000
    call(app, "accept_all", [mine])
    assert len(check._adjustments(app)) == 1  # never in Accept all, as before


def test_after_merge_into_the_one_kept_still_goes_back_to_yours(app):
    """You added it in the copy account, whose file took its place; Merge into… drops that copy for the account's own
    transaction. Removing both files puts yours back, as you entered it."""
    food = categories(app, {"Everyday": ["Food"]})["Food"]
    grocer = [("20260912", "-80.00", "SAMPLE GROCER CALGARY AB", "G1")]
    check._import(app, "chequing-1.ofx", check.ofx(check.SEPTEMBER + grocer, "2920.00", "20260915"))
    call(app, "finish_onboarding", "2026-09")
    check._new_account(app, "copy-1.ofx", check.ofx([("20260901", "2000.00", "PAYROLL SAMPLE CO", "A1")], "3050.00",
                                                    "20260901", acct_id=check.COPY))
    copy = next(a["id"] for a in call(app, "accounts") if a["name"] == "Copy")
    call(app, "add_transaction", copy, "2026-09-11", "Sample Grocer", -8000, food, "for the party")
    check._import(app, "copy-2.ofx", check.ofx(check.SEPTEMBER[1:] + grocer, "2920.00", "20260915", acct_id=check.COPY,
                                               start="20260902"))
    check._merge(app, "Copy", "SAMPLE CU Chequing")
    [kept] = _of(app, -8000)
    assert (kept["category_id"], kept["note"]) == (food, "for the party")
    _remove(app, "chequing-1.ofx")
    _remove(app, "copy-2.ofx")
    [back] = _of(app, -8000)
    assert (back["id"], back["date"], back["raw_name"], back["category_id"], back["note"]) == (
        kept["id"], "2026-09-11", "", food, "for the party")


def test_remove_imports_message_says_what_you_added_stays():
    from test_remove_import import _removed_text
    assert _removed_text({"removed": 3, "stayed": 5, "restored": 1}) == \
        "Removed 3 transactions · 5 stay: another file has them · 1 you added stays"
    assert _removed_text({"removed": 3, "stayed": 0, "restored": 1}) == "Removed 3 transactions · 1 you added stays"
    assert _removed_text({"removed": 3, "stayed": 0, "restored": 2}) == "Removed 3 transactions · 2 you added stay"
    assert _removed_text({"removed": 0, "stayed": 0, "restored": 2}) == "Nothing removed · 2 you added stay"
    assert _removed_text({"removed": 0, "stayed": 5, "restored": 1}) == \
        "Nothing removed · 5 stay: another file has them · 1 you added stays"
    assert _removed_text({"removed": 0, "stayed": 8, "restored": 0}) == "Nothing removed: another file has all 8 transactions"
    assert _removed_text({"removed": 3, "stayed": 0, "restored": 0}) == "Removed 3 transactions"


def test_an_existing_database_gets_a_place_to_remember_what_a_files_row_replaced(tmp_path):
    for version in ("12", "7"):
        folder = tmp_path / f"v{version}"
        service = check._service(folder)
        account = call(service, "add_account", "Sample Savings", "savings", 100000, "2026-10-01")
        call(service, "add_transaction", account, "2026-10-04", "Sample Grocer", -8000)
        c = service.db.conn
        for suffix in ("ins", "upd", "del"):
            c.execute(f"DROP TRIGGER IF EXISTS undo_before_file_{suffix}")
        c.execute("DROP TABLE before_file")
        c.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (version,))
        c.commit()
        service.db.close()
        again = db.Database(folder / "budget.db")
        assert again.get_meta("schema_version") == str(db.SCHEMA_VERSION)
        assert list((folder / "I Need A Budget Backups").glob(f"budget-before-upgrade-v{version}-to-v*.db"))
        with again.read() as c:
            assert c.execute("SELECT COUNT(*) FROM before_file").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        again.close()


# Random made-up histories (#148's downloads), with transactions added by hand along the way: rows the bank's later files
# bring, added before they come in, a day or two off, with a category and a note. Removing any one file leaves the budget
# as it would be had that file never come in (what the balance check sees, and where each one added by hand is and how);
# ⌘Z puts back exactly what was there.

import random  # noqa: E402
import re  # noqa: E402
from datetime import date, timedelta  # noqa: E402


def _by_hand(rng, files):
    """Which rows to add by hand, and before which file (from the third on, so the account is there even without the
    first two): [(file index, date, amount)]. Only a row no earlier file has, of an amount no other transaction has, so
    each has one place to go."""
    rows = [re.findall(r"<DTPOSTED>(\d{8})<TRNAMT>([-\d.]+)<FITID>(\w+)", text) for _, text in files]
    amount_of = {f: a for file in rows for d, a, f in file}
    unique = {a for a in amount_of.values() if list(amount_of.values()).count(a) == 1}
    seen, plan = set(), []
    for k, file in enumerate(rows):
        fresh = [(d, a) for d, a, f in file if f not in seen and a in unique]
        if k >= 2:
            for d, a in rng.sample(fresh, min(len(fresh), rng.randint(0, 2))):
                day = date(int(d[:4]), int(d[4:6]), int(d[6:])) + timedelta(days=rng.randint(-2, 2))
                plan.append((k, day.isoformat(), round(float(a) * 100)))
                unique.discard(a)
        seen |= {f for d, a, f in file}
    return plan


def _bring_in(service, files, plan):
    food = categories(service, {"Everyday": ["Food"]})["Food"]

    def add(i):
        for k, day, amount in plan:
            if k == i:
                account = call(service, "accounts")[0]["id"]
                call(service, "add_transaction", account, day, "Sample Shop", amount, food, f"note {amount}")
    for i, (name, text) in enumerate(files):
        add(i)
        check._import(service, name, text)
        if i == 0:
            call(service, "finish_onboarding", "2026-09")
    add(len(files))


def _yours(service):
    """Each one added by hand: its date, amount, wording, payee, note and the file it's under (none: as you added it)."""
    with service.db.read() as c:
        return sorted(tuple(r) for r in c.execute(
            "SELECT t.date, t.amount, t.raw_name, COALESCE(p.name, ''), t.note, COALESCE(i.filename, '') "
            "FROM transactions t LEFT JOIN payees p ON p.id = t.payee_id LEFT JOIN imports i ON i.id = t.import_id "
            "WHERE t.note != ''"))


def _everything(service):
    with service.db.read() as c:
        return {table: [tuple(r) for r in c.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid")]
                for table in ("transactions", "import_rows", "imports", "accounts", "splits", "payees", "before_file")}


@pytest.mark.parametrize("seed", range(25))
def test_removing_any_file_puts_back_what_you_added_as_if_it_had_never_come_in(tmp_path, seed):
    from test_remove_import import _history, _what_the_check_sees
    rng = random.Random(seed)
    files = _history(rng)
    plan = _by_hand(rng, files)
    app = check._service(tmp_path / "budget")
    _bring_in(app, files, plan)
    assert len(_yours(app)) == len(plan)
    for k, (name, _) in enumerate(files):
        before = _everything(app)
        _remove(app, name)
        replay = check._service(tmp_path / f"replay-{k}")
        _bring_in(replay, [f for j, f in enumerate(files) if j != k],
                  [(j if j <= k else j - 1, day, amount) for j, day, amount in plan])
        assert _what_the_check_sees(app) == _what_the_check_sees(replay), (seed, name)
        assert _yours(app) == _yours(replay), (seed, name)
        call(app, "undo")
        assert _everything(app) == before, (seed, name)
