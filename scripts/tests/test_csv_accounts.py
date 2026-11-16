# SPDX-License-Identifier: GPL-3.0-only
"""A set-up CSV finds its account by its file name, but some banks give every download the
same name. Before a file goes into the account its name points to, it has to look like that
account. Made-up data only."""

from helpers import call, write

# TD-style: no header, date, description, debit, credit, running balance (same name for every account)
CHEQUING = ("10/01/2026,SAMPLE GROCER,61.20,,938.80\n10/02/2026,SAMPLE EMPLOYER PAY,,1500.00,2438.80\n"
            "10/03/2026,SAMPLE CAFE,4.50,,2434.30\n")
NEXT_CHEQUING = ("10/04/2026,SAMPLE BOOKS,10.00,,2424.30\n10/05/2026,SAMPLE REFUND,,20.00,2444.30\n"
                 "10/06/2026,SAMPLE GYM,30.00,,2414.30\n")
VISA = ("10/04/2026,SAMPLE AIRLINE,500.00,,500.00\n10/05/2026,PAYMENT - THANK YOU,,100.00,400.00\n"
        "10/06/2026,SAMPLE HOTEL,200.00,,600.00\n")
NEXT_VISA = ("10/07/2026,SAMPLE TAXI,25.00,,625.00\n10/08/2026,SAMPLE REFUND,,5.00,620.00\n"
             "10/09/2026,SAMPLE LUNCH,15.00,,635.00\n")


def _set_up(app, text, name, kind, bank="TD", filename="accountactivity.csv"):
    path = write(app.test_downloads, filename, text)
    setup = call(app, "csv_setup", str(path))
    return call(app, "setup_csv", str(path), setup["layout"], {"name": name, "kind": kind, "bank": bank})


def _accounts(app):
    return {a["name"]: a for a in call(app, "accounts")}


def test_a_second_account_with_the_same_file_name_is_asked_about(app):
    """Issue #50: three Visa rows landed in TD Chequing, unasked."""
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    visa = write(app.test_downloads, "accountactivity.csv", VISA)
    out = call(app, "import_files", [str(visa)])
    assert out["needs_setup"] and not out["results"]
    assert [(f["filename"], f["needs"]) for f in call(app, "inbox")["files"]] == [("accountactivity.csv", "account")]
    # The pop-up doesn't pre-select the account that doesn't fit.
    assert call(app, "csv_setup", str(visa))["account_id"] is None


def test_the_next_download_with_that_name_still_imports_on_its_own(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    more = write(app.test_downloads, "accountactivity.csv", NEXT_CHEQUING)
    [result] = call(app, "import_files", [str(more)])["results"]
    assert (result["account_name"], result["added"]) == ("TD Chequing", 3)


def test_two_accounts_sharing_a_file_name_each_get_their_own_downloads(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    out = _set_up(app, VISA, "TD Visa", "credit_card")
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("TD Visa", 3)]
    # Now the name points at the Visa, but a chequing download is known by how its balance moves.
    [chq] = call(app, "import_files", [str(write(app.test_downloads, "accountactivity.csv", NEXT_CHEQUING))])["results"]
    assert (chq["account_name"], chq["added"]) == ("TD Chequing", 3)
    [visa] = call(app, "import_files", [str(write(app.test_downloads, "accountactivity.csv", NEXT_VISA))])["results"]
    assert (visa["account_name"], visa["added"]) == ("TD Visa", 3)
    assert _accounts(app)["TD Visa"]["kind"] == "credit_card"


def test_a_file_whose_overlapping_rows_dont_match_is_asked_about(app):
    """The same name, dates that overlap the account's, and none of the rows are in it."""
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    elsewhere = write(app.test_downloads, "accountactivity.csv",
                      "10/01/2026,SAMPLE OTHER ONE,12.00,,100.00\n10/02/2026,SAMPLE OTHER TWO,,13.00,250.00\n"
                      "10/03/2026,SAMPLE OTHER THREE,14.00,,175.00\n10/04/2026,SAMPLE OTHER FOUR,15.00,,301.00\n")
    out = call(app, "import_files", [str(elsewhere)])
    assert out["needs_setup"] and not out["results"]


def test_an_overlapping_download_of_the_same_account_still_imports(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    overlap = write(app.test_downloads, "accountactivity.csv", CHEQUING + NEXT_CHEQUING)
    [result] = call(app, "import_files", [str(overlap)])["results"]
    assert (result["account_name"], result["added"], result["duplicates"]) == ("TD Chequing", 3, 3)


def test_a_balance_that_doesnt_continue_the_accounts_is_said_not_asked_about(app):
    """Issue #120: the file name already says which account this is. A balance that doesn't continue is
    a missed download or a balance typed wrong, not a reason to ask which account: the import says so."""
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    chequing = _accounts(app)["TD Chequing"]
    call(app, "set_balance", chequing["id"], 243430)  # what the last file ended on
    # Same direction of travel, but it starts from a balance this account never had.
    odd = write(app.test_downloads, "accountactivity.csv", "10/04/2026,SAMPLE BOOKS,10.00,,5000.00\n"
                                                           "10/05/2026,SAMPLE REFUND,,20.00,5020.00\n"
                                                           "10/06/2026,SAMPLE GYM,30.00,,4990.00\n")
    out = call(app, "import_files", [str(odd)])
    assert not out["needs_setup"] and [r["added"] for r in out["results"]] == [3]
    # Compared once the file is in (issue #138): the balance was typed for today, so Oct 3's has this file's rows in it.
    assert "This file's balance on Oct 3 was $5,010.00; the app has $2,454.30" in out["results"][0]["balance_note"]


HEAD = "Date,Description,Amount,Balance\n"


def test_an_account_with_a_slightly_wrong_balance_isnt_asked_about_every_time(app):
    """Issue #120: a typed balance $10.00 short made every next download of the right account 'contradicted'."""
    app.db.set_meta("onboarded", "1")
    first = write(app.test_downloads, "activity.csv", HEAD + "2026-10-01,SAMPLE CAFE,-10.00,990.00\n"
                  "2026-10-02,SAMPLE GROCER,-20.00,970.00\n2026-10-03,SAMPLE EMPLOYER PAY,100.00,1070.00\n")
    setup = call(app, "csv_setup", str(first))
    call(app, "setup_csv", str(first), setup["layout"], {"name": "Sample Chequing", "kind": "chequing", "bank": "Sample CU"})
    acct = next(a["id"] for a in call(app, "accounts") if a["name"] == "Sample Chequing")
    call(app, "set_balance", acct, 106000, "2026-10-03")   # typed $10.00 short
    asked = 0
    for n, days in enumerate([(4, 5, 6), (7, 8, 9)]):
        bal = 1070.00 - 15 * n
        rows = "".join(f"2026-10-{d:02d},SAMPLE BAKERY {d},-5.00,{bal - 5 * (i + 1):.2f}\n" for i, d in enumerate(days))
        path = write(app.test_downloads, "activity.csv", HEAD + rows)
        out = call(app, "import_files", [str(path)])
        if out["needs_setup"]:
            asked += 1
            again = call(app, "csv_setup", str(path))
            call(app, "setup_csv", str(path), again["layout"], {"id": acct})
    assert asked <= 1, f"asked {asked} times"


def test_what_is_off_about_the_balance_is_said_with_the_import(app):
    app.db.set_meta("onboarded", "1")
    first = write(app.test_downloads, "activity.csv", HEAD + "2026-10-01,SAMPLE CAFE,-10.00,990.00\n"
                  "2026-10-02,SAMPLE GROCER,-20.00,970.00\n2026-10-03,SAMPLE EMPLOYER PAY,100.00,1070.00\n")
    setup = call(app, "csv_setup", str(first))
    call(app, "setup_csv", str(first), setup["layout"], {"name": "Sample Chequing", "kind": "chequing", "bank": "Sample CU"})
    acct = next(a["id"] for a in call(app, "accounts") if a["name"] == "Sample Chequing")
    call(app, "set_balance", acct, 106000, "2026-10-03")   # typed $10.00 short
    path = write(app.test_downloads, "activity.csv", HEAD + "2026-10-04,SAMPLE BAKERY,-5.00,1065.00\n"
                 "2026-10-05,SAMPLE BAKERY,-5.00,1060.00\n2026-10-06,SAMPLE BAKERY,-5.00,1055.00\n")
    [result] = call(app, "import_files", [str(path)])["results"]
    assert "This file's balance on Oct 3 was $1,070.00; the app has $1,060.00" in result["balance_note"]
    # With the balance put right, the next file continues it and says nothing.
    call(app, "set_balance", acct, 107000, "2026-10-03")
    path = write(app.test_downloads, "activity.csv", HEAD + "2026-10-07,SAMPLE BAKERY,-5.00,1050.00\n"
                 "2026-10-08,SAMPLE BAKERY,-5.00,1045.00\n2026-10-09,SAMPLE BAKERY,-5.00,1040.00\n")
    [result] = call(app, "import_files", [str(path)])["results"]
    assert "This file's balance" not in result["balance_note"]


# A file's balance is compared with the app's once the file's rows are in (issue #138). Before, a file whose rows
# came before the account's balance was always off by its own rows.


def _sample_chequing(app, text):
    app.db.set_meta("onboarded", "1")
    path = write(app.test_downloads, "activity.csv", text)
    setup = call(app, "csv_setup", str(path))
    call(app, "setup_csv", str(path), setup["layout"],
         {"name": "Sample Chequing", "kind": "chequing", "bank": "Sample CU"})
    return _accounts(app)["Sample Chequing"]["id"]


def test_an_older_csv_that_continues_the_balance_says_nothing(app):
    acct = _sample_chequing(app, HEAD + "2026-09-01,SAMPLE EMPLOYER PAY,2000.00,3050.00\n"
                                        "2026-09-10,SAMPLE CAFE,-50.00,3000.00\n"
                                        "2026-09-12,SAMPLE GROCER,-20.00,2980.00\n")
    call(app, "set_balance", acct, 298000, "2026-09-15")
    # August's download, filled in later: $250.00 before its first row ("the app has $1,050.00" until its rows are in).
    august = write(app.test_downloads, "activity.csv", HEAD + "2026-08-05,SAMPLE EMPLOYER PAY,1000.00,1250.00\n"
                   "2026-08-12,SAMPLE GROCER,-100.00,1150.00\n2026-08-20,SAMPLE PHONE CO,-100.00,1050.00\n")
    [result] = call(app, "import_files", [str(august)])["results"]
    assert (result["added"], result["balance_note"]) == (3, "")


def test_the_next_csv_after_a_balance_typed_for_today_says_nothing_when_it_continues(app, monkeypatch):
    acct = _sample_chequing(app, HEAD + "2026-10-01,SAMPLE CAFE,-10.00,990.00\n"
                                        "2026-10-02,SAMPLE GROCER,-20.00,970.00\n"
                                        "2026-10-03,SAMPLE EMPLOYER PAY,100.00,1070.00\n")
    call(app, "set_balance", acct, 106000)  # Oct 12: what the bank shows now, with two charges no file has yet
    monkeypatch.setenv("INAB_TODAY", "2026-10-20")
    path = write(app.test_downloads, "activity.csv", HEAD + "2026-10-05,SAMPLE BAKERY,-5.00,1065.00\n"
                 "2026-10-09,SAMPLE BAKERY,-5.00,1060.00\n2026-10-15,SAMPLE BOOKS,-20.00,1040.00\n")
    [result] = call(app, "import_files", [str(path)])["results"]
    assert (result["added"], result["balance_note"]) == (3, "")  # it said "the app has $1,060.00" for Oct 4


# A card's CSV dates each purchase, but lists them, and moves its balance, as they post: by date, its balance before its
# first row is off by whatever posted out of order. Like an older card file's, it isn't compared (#157's review).
NEXT_VISA_AS_POSTED = ("10/08/2026,SAMPLE TAXI,25.00,,625.00\n10/07/2026,SAMPLE LUNCH,15.00,,640.00\n"
                       "10/09/2026,SAMPLE BOOKS,10.00,,650.00\n")


def test_a_card_csv_listed_as_things_posted_says_nothing_about_its_balance(app, monkeypatch):
    """The bank's $600.00 before the taxi posted doesn't have the Oct 7 lunch, which posted after it; once the file is
    in, the app's Oct 7 has it."""
    app.db.set_meta("onboarded", "1")
    _set_up(app, VISA, "TD Visa", "credit_card")
    call(app, "set_balance", _accounts(app)["TD Visa"]["id"], 60000, "2026-10-06")  # owe $600.00, as the bank says
    monkeypatch.setenv("INAB_TODAY", "2026-10-10")
    path = write(app.test_downloads, "accountactivity.csv", NEXT_VISA_AS_POSTED)
    [result] = call(app, "import_files", [str(path)])["results"]
    assert (result["added"], result["balance_note"]) == (3, "")  # it said "…on Oct 7 was -$600.00; the app has -$615.00"


# The last file's hotel is dated Oct 8, and the next file starts with an Oct 7 lunch that posted after it.
VISA_TO_OCT_8 = ("10/04/2026,SAMPLE AIRLINE,500.00,,500.00\n10/05/2026,PAYMENT - THANK YOU,,100.00,400.00\n"
                 "10/08/2026,SAMPLE HOTEL,200.00,,600.00\n")
NEXT_AFTER_OCT_8 = ("10/07/2026,SAMPLE LUNCH,15.00,,615.00\n10/09/2026,SAMPLE TAXI,25.00,,640.00\n"
                    "10/10/2026,SAMPLE BOOKS,10.00,,650.00\n")


def test_a_card_csv_whose_first_purchase_is_dated_before_the_last_files_last_says_nothing(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, VISA_TO_OCT_8, "TD Visa", "credit_card")
    call(app, "set_balance", _accounts(app)["TD Visa"]["id"], 60000, "2026-10-08")  # owe $600.00 at the end of Oct 8
    path = write(app.test_downloads, "accountactivity.csv", NEXT_AFTER_OCT_8)
    [result] = call(app, "import_files", [str(path)])["results"]
    assert (result["added"], result["balance_note"]) == (3, "")  # it said "the app has -$385.00" (before #138, -$400.00)


# Issue #159: the account you pick in the set-up pop-up is where the file goes. Picking it asked again, forever.


def _pick(app, path, account):
    return call(app, "setup_csv", str(path), call(app, "csv_setup", str(path))["layout"], account)


def _refused(app, path, account):
    out = app.dispatch("setup_csv", [str(path), call(app, "csv_setup", str(path))["layout"], account])
    assert not out["ok"]
    return out["error"]


def test_a_card_download_with_charges_that_posted_late_comes_in_once_you_pick_its_account(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, "10/01/2026,SAMPLE AIRLINE,500.00,,500.00\n10/05/2026,PAYMENT - THANK YOU,,100.00,400.00\n"
                 "10/10/2026,SAMPLE HOTEL,200.00,,600.00\n", "TD Visa", "credit_card")
    # Three charges from Oct 7–9 that posted after the first download, then Oct 11–12.
    path = write(app.test_downloads, "accountactivity.csv",
                 "10/07/2026,SAMPLE LUNCH,15.00,,615.00\n10/08/2026,SAMPLE TAXI,25.00,,640.00\n"
                 "10/09/2026,SAMPLE BOOKS,10.00,,650.00\n10/11/2026,SAMPLE CAFE,5.00,,655.00\n"
                 "10/12/2026,SAMPLE GROCER,20.00,,675.00\n")
    assert call(app, "import_files", [str(path)])["needs_setup"]  # "Which account is accountactivity.csv for?"
    out = _pick(app, path, {"id": _accounts(app)["TD Visa"]["id"]})
    assert not out["needs_setup"]  # it asked again
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("TD Visa", 5)]


GAP_HEAD = "Date,Description,Amount\n"
GAP = GAP_HEAD + "".join(f"2026-08-{d},SAMPLE SHOP {d},-{d}.00\n" for d in (10, 12, 15, 20))


def _gap_account(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, GAP_HEAD + "2026-08-01,SAMPLE CAFE,-4.50\n2026-08-02,SAMPLE GROCER,-61.20\n"
                            "2026-08-03,SAMPLE EMPLOYER PAY,1500.00\n",
            "Sample Chequing", "chequing", bank="Sample CU", filename="activity.csv")
    sep = write(app.test_downloads, "activity.csv", GAP_HEAD + "2026-09-01,SAMPLE CAFE,-4.75\n"
                "2026-09-02,SAMPLE GROCER,-58.10\n2026-09-03,SAMPLE EMPLOYER PAY,1500.00\n")
    [result] = call(app, "import_files", [str(sep)])["results"]
    assert result["added"] == 3
    return _accounts(app)["Sample Chequing"]["id"]


def test_a_missed_download_filled_in_later_comes_in_once_you_pick_its_account(app):
    acct = _gap_account(app)
    path = write(app.test_downloads, "activity.csv", GAP)  # Aug 10–20, all inside Aug 1 – Sep 3, none of it there
    assert call(app, "import_files", [str(path)])["needs_setup"]
    out = _pick(app, path, {"id": acct})
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("Sample Chequing", 4)]
    # The same file dropped again after Remove import is checked as before: asked again.
    call(app, "undo_import", out["results"][0]["import_id"])
    again = write(app.test_downloads, "activity.csv", GAP)
    assert call(app, "import_files", [str(again)])["needs_setup"]


def test_the_next_file_with_that_name_is_still_checked(app):
    acct = _gap_account(app)
    _pick(app, write(app.test_downloads, "activity.csv", GAP), {"id": acct})
    # Inside the account's dates, and none of it is there: asked about, as before.
    odd = write(app.test_downloads, "activity.csv", GAP_HEAD + "2026-08-25,SAMPLE OTHER ONE,-12.00\n"
                "2026-08-26,SAMPLE OTHER TWO,-13.00\n2026-08-27,SAMPLE OTHER THREE,-14.00\n")
    out = call(app, "import_files", [str(odd)])
    assert out["needs_setup"] and not out["results"]


def test_a_new_file_name_set_up_for_a_card_whose_late_charges_arent_there_comes_in_at_once(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, "10/01/2026,SAMPLE AIRLINE,500.00,,500.00\n10/05/2026,PAYMENT - THANK YOU,,100.00,400.00\n"
                 "10/10/2026,SAMPLE HOTEL,200.00,,600.00\n", "TD Visa", "credit_card")
    path = write(app.test_downloads, "visa 2026-10-13.csv",
                 "10/07/2026,SAMPLE LUNCH,15.00,,615.00\n10/08/2026,SAMPLE TAXI,25.00,,640.00\n"
                 "10/09/2026,SAMPLE BOOKS,10.00,,650.00\n10/11/2026,SAMPLE CAFE,5.00,,655.00\n"
                 "10/12/2026,SAMPLE GROCER,20.00,,675.00\n")
    assert call(app, "import_files", [str(path)])["needs_setup"]  # a name the app hasn't seen
    out = _pick(app, path, {"id": _accounts(app)["TD Visa"]["id"]})
    assert not out["needs_setup"]
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("TD Visa", 5)]


def test_your_pick_wins_over_another_account_the_file_fits(app):
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    _set_up(app, "10/01/2026,SAMPLE INTEREST,,1.00,101.00\n10/05/2026,SAMPLE TRANSFER IN,,50.00,151.00\n"
                 "10/10/2026,SAMPLE TRANSFER OUT,20.00,,131.00\n", "TD Savings", "savings")
    # NEXT_CHEQUING continues TD Chequing's balance, and its rows fall inside TD Savings' dates without being there:
    # on its own it goes to TD Chequing, though the name points at TD Savings.
    path = write(app.test_downloads, "accountactivity.csv", NEXT_CHEQUING)
    [result] = call(app, "import_files", [str(path)])["results"]
    assert result["account_name"] == "TD Chequing"
    call(app, "undo_import", result["import_id"])
    path = write(app.test_downloads, "accountactivity.csv", NEXT_CHEQUING)
    out = _pick(app, path, {"id": _accounts(app)["TD Savings"]["id"]})
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("TD Savings", 3)]


def _ofx(rows, balance="2000.00", as_of="20261008"):
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Sample CU"
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            "<BANKACCTFROM><BANKID>0000<ACCTID>000999888777<ACCTTYPE>CHECKING</BANKACCTFROM>\n<BANKTRANLIST>\n"
            f"{trn}</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS>"
            "</BANKMSGSRSV1></OFX>\n")


def test_a_csv_dated_a_day_before_the_quicken_files_isnt_asked_about(app):
    """Decided on #159: a row is already there when the account has one of the same amount up to two days away
    (ledger.OTHER_KIND_DAYS), as for the import, not only on the same day."""
    app.db.set_meta("onboarded", "1")
    # Quicken files date things the day they posted: Oct 2, 3, 6, 7.
    call(app, "import_files", [str(write(app.test_downloads, "Statement.qfx", _ofx([
        ("20261002", "-61.20", "GROCER 0042", "Q1"), ("20261003", "1500.00", "PAYROLL DEPOSIT", "Q2"),
        ("20261006", "-4.50", "CAFE PURCHASE", "Q3"), ("20261007", "-22.00", "BOOKSTORE", "Q4")])))])
    [acct] = call(app, "accounts")
    # The bank's CSV, under a name now known to be this account (September's rows, before the Quicken file).
    _pick(app, write(app.test_downloads, "activity.csv", GAP_HEAD + "2026-09-01,SAMPLE CAFE,-4.75\n"
                     "2026-09-02,SAMPLE GROCER,-58.10\n2026-09-03,SAMPLE GYM,-30.00\n"), {"id": acct["id"]})
    # Its next download dates the same four the day they were made: Oct 1, 2, 5, 6.
    path = write(app.test_downloads, "activity.csv", GAP_HEAD + "2026-10-01,SAMPLE GROCER,-61.20\n"
                 "2026-10-02,SAMPLE EMPLOYER PAY,1500.00\n2026-10-05,SAMPLE CAFE,-4.50\n2026-10-06,SAMPLE BOOKS,-22.00\n")
    out = call(app, "import_files", [str(path)])
    assert not out["needs_setup"]
    assert [(r["added"], r["duplicates"]) for r in out["results"]] == [(0, 4)]


def test_a_card_file_starts_its_new_account_on_credit_card(app):
    """Decided on #159: the file's balance goes up as money goes out, so "New account…" starts on Credit card, whatever
    the file name says ("accountactivity.csv" said Chequing)."""
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    visa = write(app.test_downloads, "accountactivity.csv", VISA)
    assert call(app, "import_files", [str(visa)])["needs_setup"]
    assert call(app, "csv_setup", str(visa))["balance_sign"] == -1
    assert call(app, "csv_setup", str(write(app.test_downloads, "other.csv", NEXT_CHEQUING)))["balance_sign"] == 1
    assert call(app, "csv_setup", str(write(app.test_downloads, "plain.csv", GAP)))["balance_sign"] is None


CARD_REFUSED = "This file's balance goes up when money goes out, like a card's. Pick a card account."


def _everything(app):
    with app.db.read() as c:
        return [c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
                for t in ("accounts", "account_sources", "transactions", "imports", "csv_layouts")]


def test_a_card_file_cant_go_into_a_chequing_account(app):
    """Decided on #159: refused before anything is written, so no account is left behind."""
    app.db.set_meta("onboarded", "1")
    _set_up(app, CHEQUING, "TD Chequing", "chequing")
    visa = write(app.test_downloads, "accountactivity.csv", VISA)
    call(app, "import_files", [str(visa)])
    before = [[tuple(r) for r in rows] for rows in _everything(app)]
    assert _refused(app, visa, {"id": _accounts(app)["TD Chequing"]["id"]}) == CARD_REFUSED
    assert _refused(app, visa, {"name": "TD Chequing 2", "kind": "chequing", "bank": "TD"}) == CARD_REFUSED
    assert _refused(app, visa, {"name": "TD Wallet", "kind": "cash", "bank": "TD"}) == CARD_REFUSED
    assert [[tuple(r) for r in rows] for rows in _everything(app)] == before
    # A card account takes it.
    out = _pick(app, visa, {"name": "TD Credit card", "kind": "credit_card", "bank": "TD"})
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("TD Credit card", 3)]


def test_a_file_whose_balance_goes_down_with_charges_can_go_into_a_card(app):
    """Either sort: some banks' card balances go down as you spend. The pick wins."""
    app.db.set_meta("onboarded", "1")
    path = write(app.test_downloads, "activity.csv", NEXT_CHEQUING)
    out = _pick(app, path, {"name": "Sample Card", "kind": "credit_card", "bank": "Sample CU"})
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("Sample Card", 3)]


def test_a_check_balance_adjustment_doesnt_make_the_next_download_look_like_another_accounts(app, monkeypatch):
    """The review of #159: a Check balance… adjustment, dated today, stretched the account's dates, so every row of the
    next download fell inside them without being there, and the app asked which account it was for."""
    app.db.set_meta("onboarded", "1")
    _set_up(app, VISA_TO_OCT_8, "TD Visa", "credit_card")
    acct = _accounts(app)["TD Visa"]["id"]
    call(app, "set_balance", acct, 60000, "2026-10-08")
    monkeypatch.setenv("INAB_TODAY", "2026-10-12")
    call(app, "reconcile", acct, 64000)  # the bank shows $640.00 owed: a balance adjustment dated Oct 12
    path = write(app.test_downloads, "accountactivity.csv", "10/09/2026,SAMPLE TAXI,25.00,,625.00\n"
                 "10/10/2026,SAMPLE BOOKS,10.00,,635.00\n10/11/2026,SAMPLE CAFE,5.00,,640.00\n")
    out = call(app, "import_files", [str(path)])
    assert not out["needs_setup"]
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("TD Visa", 3)]
