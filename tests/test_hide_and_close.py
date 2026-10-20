"""A category or account that has been used is never deleted, only hidden or closed:
deleting one sent its transactions back to the inbox, took its assigned money out of
every month, and sent its transfers' other halves to the inbox."""

from helpers import CARD_FILE, bank_csv, call, card_csv, categories, txns, write
from test_importer import ofx_file

CATS = {"Bills": ["Utilities", "Subscriptions"], "Everyday": ["Food", "Household", "Typo"]}


def imp(app, name, text):
    return call(app, "import_files", [str(write(app.test_downloads, name, text))])


def refused(app, request, *args):
    result = app.dispatch(request, list(args))
    assert not result["ok"]
    return result["error"]


def used(app):
    return {c["name"]: c["used"] for g in call(app, "categories") for c in g["categories"]}


def account(app, name):
    return next(a for a in call(app, "accounts") if a["name"] == name)


def chequing(app):
    imp(app, "Chequing.CSV", bank_csv([
        ("09/08/2026", "SAMPLE CAFE CALGARY AB", "SAMPLE CAFE CALGARY AB", "-12.50"),
        ("09/09/2026", "SAMPLE HARDWARE CALGARY AB", "SAMPLE HARDWARE CALGARY AB", "-80.00"),
        ("09/10/2026", "EFT Withdrawal to SAMPLE UTILI", "EFT Withdrawal to SAMPLE UTILITY", "-171.25"),
    ]))
    return {t["payee_name"]: t for t in txns(app)}


def test_a_category_never_used_can_be_deleted(app):
    ids = categories(app, CATS)
    assert used(app)["Typo"] is False
    assert call(app, "delete_category", ids["Typo"]) is True
    assert "Typo" not in used(app)
    assert call(app, "undo")["undone"] == "Delete category"
    assert "Typo" in used(app)


def test_a_used_category_can_only_be_hidden(app):
    ids = categories(app, CATS)
    rows = chequing(app)
    call(app, "categorize", txn_ids=[rows["Sample Cafe"]["id"]], category_id=ids["Food"], recheck_payee=True)       # a transaction
    call(app, "split_transaction", rows["Sample Hardware"]["id"], [
        {"category_id": ids["Household"], "amount": -5000}, {"category_id": ids["Food"], "amount": -3000}])  # a split
    call(app, "set_assigned", "2026-10", ids["Utilities"], 20000)                         # money assigned
    call(app, "add_subscription", payee="Sample Streaming", name="Sample Streaming", amount=999,
         category_id=ids["Subscriptions"])                                                 # a recurring charge
    names = ("Utilities", "Subscriptions", "Food", "Household")
    flags = used(app)
    assert all(flags[n] for n in names) and flags["Typo"] is False

    inbox = [i["id"] for i in call(app, "inbox")["items"]]
    for name in names:
        assert refused(app, "delete_category", ids[name]) == f"{name} has been used, so it can only be hidden."
    # Nothing moved: no transaction went back to the inbox and the money is still assigned.
    assert [i["id"] for i in call(app, "inbox")["items"]] == inbox
    assert {t["payee_name"]: t["category_name"] for t in txns(app)}["Sample Cafe"] == "Food"
    with app.db.read() as c:
        assert c.execute("SELECT assigned FROM budget WHERE category_id = ?", (ids["Utilities"],)).fetchone()[0] == 20000

    # Hidden instead: it keeps its history.
    call(app, "set_category_hidden", ids["Food"], True)
    assert {t["payee_name"]: t["category_name"] for t in txns(app)}["Sample Cafe"] == "Food"
    assert next(c for g in call(app, "categories") for c in g["categories"] if c["id"] == ids["Food"])["hidden"]


def test_an_example_learnt_from_actual_counts_as_used(app):
    ids = categories(app, CATS)
    with app.db.write() as c:
        payee = c.execute("INSERT INTO payees (name) VALUES ('Sample Bakery')").lastrowid
        c.execute("INSERT INTO examples (payee_id, category_id, amount, date, source) "
                  "VALUES (?, ?, -800, '2026-01-05', 'actual')", (payee, ids["Typo"]))
    assert used(app)["Typo"] is True
    assert "can only be hidden" in refused(app, "delete_category", ids["Typo"])


def test_the_apps_own_categories_cant_be_deleted(app):
    call(app, "add_account", name="Sample Card", kind="credit_card")
    call(app, "finish_onboarding", "2026-10")
    own = {c["name"]: c for g in call(app, "categories") for c in g["categories"]
           if c["name"] in ("Income", "Interest & Fees")}
    assert len(own) == 2 and all(c["used"] for c in own.values())
    for c in own.values():
        assert refused(app, "delete_category", c["id"]) == "This category is needed by the app and can't be deleted."


def test_only_an_empty_group_can_be_deleted(app):
    ids = categories(app, {"Spare": ["Typo"]})
    group = next(g["id"] for g in call(app, "categories") if g["name"] == "Spare")
    assert refused(app, "delete_group", group) == "Only an empty group can be deleted."
    call(app, "delete_category", ids["Typo"])
    assert call(app, "delete_group", group) is True
    assert "Spare" not in {g["name"] for g in call(app, "categories")}


def test_an_account_never_used_can_be_deleted(app):
    loc = call(app, "add_account", name="Line of credit", kind="line_of_credit", bank="Sample Credit Union")
    assert account(app, "Line of credit")["used"] is False
    assert call(app, "delete_account", loc) is True
    assert call(app, "accounts") == []


def test_a_used_account_can_only_be_closed(app):
    categories(app, CATS)
    chequing(app)
    cheq = account(app, "Chequing")
    assert cheq["used"] is True
    assert refused(app, "delete_account", cheq["id"]) == "Chequing has been used, so it can only be closed."
    assert len(txns(app)) == 3

    # A transfer marked to an account with nothing imported yet uses it too.
    loc = call(app, "add_account", name="Line of credit", kind="line_of_credit")
    hardware = next(t for t in txns(app) if t["payee_name"] == "Sample Hardware")
    call(app, "update_transaction", hardware["id"], {"transfer_account_id": loc})
    assert "can only be closed" in refused(app, "delete_account", loc)

    # So does money assigned to a card's payment category.
    card = call(app, "add_account", name="Sample Card", kind="credit_card")
    payment = next(c["id"] for g in call(app, "categories") for c in g["categories"] if c["debt_account_id"] == card)
    assert account(app, "Sample Card")["used"] is False
    call(app, "set_assigned", "2026-10", payment, 5000)
    assert "can only be closed" in refused(app, "delete_account", card)


def test_a_closed_account_keeps_its_history_and_can_be_reopened(app):
    categories(app, CATS)
    chequing(app)
    imp(app, CARD_FILE, card_csv([("09/12/2026", "SAMPLE CAFE CALGARY AB", "-4.75", "Restaurant")]))
    cheq = account(app, "Chequing")
    call(app, "set_balance", cheq["id"], 100000)
    call(app, "update_account", cheq["id"], {"closed": True})
    assert call(app, "undo")["undone"] == "Close account"
    call(app, "update_account", cheq["id"], {"closed": True})

    closed = account(app, "Chequing")
    assert closed["closed"] == 1 and closed["transactions"] == 3 and closed["balance"] == 100000
    assert len(txns(app, account_id=cheq["id"])) == 3
    assert call(app, "dashboard", 3)["cash"] == 100000  # it still has money, so it's still in Cash
    assert refused(app, "add_transaction", cheq["id"], "2026-10-12", "Sample Cafe", -500) == "Chequing is closed."

    # Not offered when a bank file is for an account the app hasn't seen.
    app.db.set_meta("onboarded", "1")
    other = write(app.test_downloads, "Savings.QFX", ofx_file(
        "Sample Credit Union", [("20260910", "-8.00", "SAMPLE DINER", "", "S1")], acct_id="0005678"))
    [part] = call(app, "file_accounts", str(other))["parts"]
    assert cheq["id"] not in [c["id"] for c in part["choices"]]

    call(app, "update_account", cheq["id"], {"closed": False})
    assert call(app, "undo")["undone"] == "Reopen account"
    call(app, "update_account", cheq["id"], {"closed": False})
    assert account(app, "Chequing")["closed"] == 0
    assert call(app, "add_transaction", cheq["id"], "2026-10-12", "Sample Cafe", -500)


def test_a_file_for_a_closed_account_imports_into_it(app):
    """Found by its own number or name, a closed account still gets its file's new rows,
    and stays closed."""
    chequing(app)
    cheq = account(app, "Chequing")
    call(app, "update_account", cheq["id"], {"closed": True})
    [result] = imp(app, "Chequing.CSV", bank_csv([
        ("10/01/2026", "SAMPLE CAFE CALGARY AB", "SAMPLE CAFE CALGARY AB", "-3.25")]))["results"]
    assert (result["account_id"], result["added"]) == (cheq["id"], 1)
    assert account(app, "Chequing")["closed"] == 1


def test_a_transfer_with_a_closed_account_names_it(app):
    categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("07/03/2026", "PAYMENT - THANK YOU", "1250.00", "")]))
    card = next(a for a in call(app, "accounts") if a["is_debt"])
    call(app, "update_account", card["id"], {"closed": True})
    imp(app, "Chequing.CSV", bank_csv([
        ("07/03/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-1250.00")]))
    payment = next(t for t in txns(app) if t["account_kind"] == "chequing")
    assert payment["transfer_account_id"] == card["id"]
    assert payment["reason"] == f"Transfer to {card['name']}"
