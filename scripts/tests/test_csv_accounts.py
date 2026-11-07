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
    assert "This file's balance on Oct 3 was $5,010.00; the app has $2,434.30" in out["results"][0]["balance_note"]


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
