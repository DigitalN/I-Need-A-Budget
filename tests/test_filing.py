"""What gets filed on its own, what's asked about, and learning from answers."""

from helpers import CARD_FILE, bank_csv, call, card_csv, categories, txns, write

CATS = {"Bills": ["Utilities", "Subscriptions", "Insurance"],
        "Everyday": ["Food", "Household", "Fun Money"]}


def imp(app, name, text):
    return call(app, "import_files", [str(write(app.test_downloads, name, text))])


def by_payee(app, name):
    return [t for t in txns(app) if t["payee_name"] == name]


def test_filing_one_files_the_rest_of_that_payee(app):
    ids = categories(app, CATS)
    imp(app, "Chequing.CSV", bank_csv([
        ("07/09/2026", "EFT Withdrawal to SAMPLE UTILI", "EFT Withdrawal to SAMPLE UTILITY", "-233.10"),
        ("08/11/2026", "EFT Withdrawal to SAMPLE UTILI", "EFT Withdrawal to SAMPLE UTILITY", "-198.40"),
        ("09/08/2026", "EFT Withdrawal to SAMPLE UTILI", "EFT Withdrawal to SAMPLE UTILITY", "-171.25"),
    ]))
    items = call(app, "inbox")["items"]
    assert len(items) == 3 and len({i["group"] for i in items}) == 1  # one card in the inbox
    result = call(app, "approve", txn_id=items[0]["id"], category_id=ids["Utilities"])
    assert result["also_filed"] == 2
    assert result["inbox"] == 0
    assert {t["category_name"] for t in txns(app)} == {"Utilities"}


def test_a_payee_used_for_two_categories_always_asks_closest_amount_first(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([
        ("07/10/2026", "COSTCO WHOLESALE W156 CALGARY A", "-3.99", "Other"),
        ("07/10/2026", "COSTCO WHOLESALE W156 CALGARY A", "-980.00", "Other"),
    ]))
    small, big = sorted(by_payee(app, "Costco"), key=lambda t: -t["amount"])
    call(app, "approve", txn_id=small["id"], category_id=ids["Food"])
    call(app, "approve", txn_id=big["id"], category_id=ids["Household"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([
        ("08/17/2026", "COSTCO WHOLESALE W156 CALGARY A", "-4.99", "Other"),
        ("08/18/2026", "COSTCO WHOLESALE W156 CALGARY A", "-910.00", "Other"),
    ]))
    items = {i["amount"]: i for i in call(app, "inbox")["items"]}
    assert items[-499]["mixed"] and items[-91000]["mixed"]
    assert items[-499]["suggestions"][0]["name"] == "Food"
    assert items[-91000]["suggestions"][0]["name"] == "Household"


def test_card_payments_pair_with_chequing(app):
    categories(app, CATS)
    imp(app, "Chequing.CSV", bank_csv([
        ("06/03/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-1180.00"),
        ("07/03/2026", "Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "-1250.00"),
    ]))
    # The card isn't imported yet: the first payment can only be marked once the card exists.
    imp(app, CARD_FILE, card_csv([("07/03/2026", "PAYMENT - THANK YOU", "1250.00", "")]))
    rows = txns(app)
    card = next(t for t in rows if t["account_kind"] == "credit_card")
    cheq = next(t for t in rows if t["date"] == "2026-07-03" and t["account_kind"] == "chequing")
    assert card["transfer_id"] == cheq["id"] and cheq["transfer_id"] == card["id"]
    assert card["category_id"] is None and card["status"] == "done"
    assert call(app, "inbox")["items"] == [] or all(i["date"] == "2026-06-03" for i in call(app, "inbox")["items"])


def test_refunds_go_back_where_the_purchase_went(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("07/08/2026", "AMZN Mktp CA*O23JY2NY3 TORONTO O", "-64.20", "Other")]))
    [purchase] = txns(app)
    call(app, "approve", txn_id=purchase["id"], category_id=ids["Household"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([
        ("07/10/2026", "AMZN Mktp CA TORONTO ON", "64.20", "Other"),        # exact: filed
    ]))
    refund = next(t for t in txns(app) if t["amount"] > 0)
    assert (refund["category_name"], refund["status"], refund["refund_of"]) == ("Household", "done", purchase["id"])


def test_a_partial_refund_is_suggested_not_assumed(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("08/27/2026", "WWW COSTCO CA OTTAWA ON", "-288.10", "Other")]))
    call(app, "approve", txn_id=txns(app)[0]["id"], category_id=ids["Household"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([("09/08/2026", "WWW COSTCO CA OTTAWA ON", "287.50", "Other")]))
    [item] = call(app, "inbox")["items"]
    assert item["suggestions"][0]["name"] == "Household" and "refund" in item["reason"].lower()


def test_e_transfers_that_are_shares_of_a_bill(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("08/11/2026", "SAMPLE SPORTS CLUB CALGARY AB", "-100.00", "Fitness")]))
    call(app, "approve", txn_id=txns(app)[0]["id"], category_id=ids["Fun Money"])
    imp(app, "Chequing.CSV", bank_csv([
        ("08/05/2026", "INTERAC e-Transfer From: ALEX", "INTERAC e-Transfer From: ALEX SAMPLE", "25.00"),
        ("08/06/2026", "INTERAC e-Transfer From: BLAIR", "INTERAC e-Transfer From: BLAIR SAMPLE", "25.00"),
        ("08/07/2026", "INTERAC e-Transfer From: CASEY", "INTERAC e-Transfer From: CASEY SAMPLE", "25.00"),
    ]))
    items = call(app, "inbox")["items"]
    assert len(items) == 3
    assert all(i["suggestions"][0]["name"] == "Fun Money" and "1/4 share" in i["reason"] for i in items)
    assert len({i["group"] for i in items}) == 1  # filed together


def test_a_share_matches_even_with_always_ask_off(app):
    ids = categories(app, CATS)
    imp(app, "Chequing.CSV", bank_csv([
        ("06/05/2026", "INTERAC e-Transfer From: ALEX", "INTERAC e-Transfer From: ALEX SAMPLE", "40.00")]))
    [earlier] = call(app, "inbox")["items"]
    call(app, "approve", txn_id=earlier["id"], category_id=ids["Food"])
    call(app, "set_always_ask", earlier["payee_id"], False)  # Alex is learnt now: Food, every time so far
    imp(app, CARD_FILE, card_csv([("08/11/2026", "SAMPLE SPORTS CLUB CALGARY AB", "-100.00", "Fitness")]))
    call(app, "approve", txn_id=call(app, "inbox")["items"][0]["id"], category_id=ids["Fun Money"])
    imp(app, "Chequing.CSV", bank_csv([
        ("06/05/2026", "INTERAC e-Transfer From: ALEX", "INTERAC e-Transfer From: ALEX SAMPLE", "40.00"),
        ("08/05/2026", "INTERAC e-Transfer From: ALEX", "INTERAC e-Transfer From: ALEX SAMPLE", "25.00"),
        ("08/06/2026", "INTERAC e-Transfer From: BLAIR", "INTERAC e-Transfer From: BLAIR SAMPLE", "25.00"),
        ("08/07/2026", "INTERAC e-Transfer From: CASEY", "INTERAC e-Transfer From: CASEY SAMPLE", "25.00"),
    ]))
    items = call(app, "inbox")["items"]
    assert len(items) == 3  # Alex's share isn't categorized as Food without asking
    assert all(i["suggestions"][0]["name"] == "Fun Money" and "1/4 share" in i["reason"] for i in items)
    assert len({i["group"] for i in items}) == 1 and all(i["category_id"] is None for i in items)


def test_similar_names_are_matched_or_asked_about(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("07/01/2026", "SAFEWAY #6432 CALGARY AB", "-50.00", "Groceries")]))
    call(app, "approve", txn_id=txns(app)[0]["id"], category_id=ids["Food"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([("07/05/2026", "SAVEWAY 1532 CALGARY AB", "-42.00", "Groceries")]))
    typo = next(t for t in txns(app) if t["date"] == "2026-07-05")
    assert (typo["payee_name"], typo["category_name"], typo["status"]) == ("Safeway", "Food", "done")
    assert "similar name" in typo["reason"]


def test_a_much_bigger_charge_than_usual_asks(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("07/28/2026", "APPLE.COM/BILL TORONTO ON", "-12.99", "Bill Payment")]))
    call(app, "approve", txn_id=txns(app)[0]["id"], category_id=ids["Subscriptions"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([("09/19/2026", "APPLE.COM/CA TORONTO ON", "-489.00", "Bill Payment")]))
    [item] = call(app, "inbox")["items"]
    assert "bigger than usual" in item["reason"]


def test_interest_and_rewards_file_themselves(app):
    categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("10/02/2026", "PURCHASE INTEREST", "-41.30", ""),
                                  ("07/02/2026", "MONEY-BACK REWARD", "15.20", "")]))
    names = {t["payee_name"]: (t["category_name"], t["status"]) for t in txns(app)}
    assert names == {"Interest charge": ("Interest & Fees", "done"), "Money-Back Reward": ("Income", "done")}


def test_interest_earned_follows_the_category_you_move_one_to(app):
    ids = categories(app, {**CATS, "Savings": ["Emergency Fund"]})
    interest = lambda date, amount: (date, "Interest Paid", "Interest Paid", amount)
    imp(app, "Savings.CSV", bank_csv([interest("08/31/2026", "3.12")]))
    [august] = txns(app)
    assert (august["payee_name"], august["category_name"], august["status"]) == ("Interest earned", "Income", "done")
    assert not august["can_always_ask"]
    call(app, "update_transaction", august["id"], {"category_id": ids["Emergency Fund"]})
    imp(app, "Savings.CSV", bank_csv([interest("08/31/2026", "3.12"), interest("09/30/2026", "3.20")]))
    september = txns(app, month="2026-09")[0]
    assert (september["category_name"], september["status"]) == ("Emergency Fund", "done")
    assert call(app, "inbox")["items"] == []  # never asked about


def test_a_cash_withdrawal_has_always_ask_and_is_learnt(app):
    ids = categories(app, CATS)
    cash = lambda date: (date, "Withdrawal - Scotiabank ABM", "Withdrawal - Scotiabank ABM", "-100.00")
    imp(app, "Chequing.CSV", bank_csv([cash("07/03/2026")]))
    [item] = call(app, "inbox")["items"]
    assert item["can_always_ask"] and not item["always_ask"]
    call(app, "approve", txn_id=item["id"], category_id=ids["Food"])
    imp(app, "Chequing.CSV", bank_csv([cash("07/03/2026"), cash("08/03/2026")]))
    august = txns(app, month="2026-08")[0]
    assert (august["payee_name"], august["category_name"], august["status"]) == ("Cash withdrawal", "Food", "done")


def test_renaming_a_payee_sticks_for_future_imports(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("07/01/2026", "SQ *CORNER BAKERY ROAS CALGARY", "-6.50", "Restaurant")]))
    t = txns(app)[0]
    call(app, "update_transaction", t["id"], {"payee": "Corner Bakery Roasters"})
    call(app, "approve", txn_id=t["id"], category_id=ids["Food"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([("07/09/2026", "SQ *CORNER BAKERY ROAS CALGARY", "-7.25", "Restaurant")]))
    new = next(x for x in txns(app) if x["date"] == "2026-07-09")
    assert (new["payee_name"], new["category_name"], new["status"]) == ("Corner Bakery Roasters", "Food", "done")


def test_undo_reverses_filing(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("07/01/2026", "SAMPLE CAFE CALGARY AB", "-6.50", "Restaurant")]))
    t = txns(app)[0]
    call(app, "approve", txn_id=t["id"], category_id=ids["Food"])
    assert call(app, "undo")["undone"] == "File transaction"
    after = txns(app)[0]
    assert (after["category_id"], after["status"]) == (t["category_id"], "review")


def test_transfers_with_people_are_never_assumed(app):
    ids = categories(app, CATS)
    imp(app, "Chequing.CSV", bank_csv([
        ("07/27/2026", "INTERAC e-Transfer To: Riley Exa", "INTERAC e-Transfer To: Riley Example", "-60.00"),
        ("09/30/2026", "INTERAC e-Transfer To: Riley Exa", "INTERAC e-Transfer To: Riley Example", "-60.00"),
        ("07/20/2026", "INTERAC e-Transfer From: RILEY", "INTERAC e-Transfer From: RILEY EXAMPLE", "500.00"),
    ]))
    items = call(app, "inbox")["items"]
    assert len(items) == 3
    assert all(i["group"] == "" and i["category_id"] is None for i in items)  # no grouping, no pre-filled guess
    assert all("refund" not in i["reason"].lower() for i in items)           # a person isn't a store
    first = next(i for i in items if i["date"] == "2026-09-30")
    assert call(app, "approve", txn_id=first["id"], category_id=ids["Food"])["also_filed"] == 0
    other = next(i for i in call(app, "inbox")["items"] if i["date"] == "2026-07-27")
    assert other["status"] == "review" and other["category_id"] is None
    assert other["suggestions"][0]["name"] == "Food"  # suggested, not assumed


def test_an_e_transfer_payee_asks_until_always_ask_is_switched_off(app):
    ids = categories(app, {**CATS, "Home": ["Rent"]})
    rent = lambda month: (f"{month:02d}/01/2026", "INTERAC e-Transfer To: SAMPLE LA",
                          "INTERAC e-Transfer To: SAMPLE LANDLORD", "-1234.00")
    imp(app, "Chequing.CSV", bank_csv([rent(7), rent(8)]))
    items = call(app, "inbox")["items"]
    assert len(items) == 2 and all(i["always_ask"] and i["group"] == "" and i["category_id"] is None for i in items)
    call(app, "approve", txn_id=items[0]["id"], category_id=ids["Rent"])
    [left] = call(app, "inbox")["items"]  # still asked: Always ask is on
    assert left["suggestions"][0]["name"] == "Rent"
    assert not any(s["name"] == "Sample Landlord" for s in call(app, "subscriptions")["suggested"])

    call(app, "set_always_ask", left["payee_id"], False)
    assert call(app, "inbox")["items"] == []  # learnt, so categorized without asking
    imp(app, "Chequing.CSV", bank_csv([rent(7), rent(8), rent(9)]))
    september = txns(app, month="2026-09")[0]
    assert (september["category_name"], september["status"]) == ("Rent", "done")
    [landlord] = [s for s in call(app, "subscriptions")["suggested"] if s["name"] == "Sample Landlord"]
    assert (landlord["amount"], landlord["kind"]) == (123400, "bill")


def test_one_at_a_time_never_files_the_others(app):
    ids = categories(app, CATS)
    imp(app, "Chequing.CSV", bank_csv([
        ("07/09/2026", "EFT Withdrawal to SAMPLE UTILI", "EFT Withdrawal to SAMPLE UTILITY", "-233.10"),
        ("08/11/2026", "EFT Withdrawal to SAMPLE UTILI", "EFT Withdrawal to SAMPLE UTILITY", "-198.40"),
    ]))
    first = call(app, "inbox")["items"][0]
    result = call(app, "approve", txn_id=first["id"], category_id=ids["Utilities"], refile=False)
    assert result["also_filed"] == 0 and result["inbox"] == 1


def test_filing_into_subscriptions_asks_what_it_is(app):
    ids = categories(app, CATS)
    imp(app, "Chequing.CSV", bank_csv([
        (f"{m:02d}/24/2026", "EFT Withdrawal to SAMPLE PAYPLAN", "EFT Withdrawal to SAMPLE PAYPLAN", "-38.50")
        for m in (7, 8, 9)]))
    items = call(app, "inbox")["items"]
    result = call(app, "approve_many", items=[{"id": i["id"], "category_id": ids["Subscriptions"]} for i in items])
    prompt = result["subscription_prompt"]
    assert prompt and prompt["amount"] == 3850 and prompt["count"] == 3 and prompt["cadence"] == "monthly"
    assert prompt["kind"] == "bill"  # paid from chequing by EFT
    out = call(app, "subscription_from_transaction", txn_id=prompt["txn_id"], name="Couch",
               category_id=ids["Subscriptions"], cadence="monthly", kind="bill", note="Ends after 12 payments")
    assert {t["subscription_name"] for t in txns(app)} == {"Couch"}
    [bill] = call(app, "subscriptions")["bills"]
    assert (bill["id"], bill["note"], bill["usual_amount"]) == (out["subscription_id"], "Ends after 12 payments", 3850)
    # A new charge at that price files itself under its name.
    imp(app, "Chequing-2.CSV", bank_csv([
        ("10/24/2026", "EFT Withdrawal to SAMPLE PAYPLAN", "EFT Withdrawal to SAMPLE PAYPLAN", "-38.50")]))
    october = txns(app, month="2026-10")[0]
    assert (october["subscription_name"], october["status"]) == ("Couch", "done")


def test_not_recurring_stops_the_question(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([("09/05/2026", "SAMPLE SOFTWARE TORONTO ON", "-29.99", "Other")]))
    item = call(app, "inbox")["items"][0]
    prompt = call(app, "approve", txn_id=item["id"], category_id=ids["Subscriptions"])["subscription_prompt"]
    assert prompt and "Subscriptions" in prompt["why"]
    call(app, "not_a_subscription", prompt["txn_id"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([("10/05/2026", "SAMPLE SOFTWARE TORONTO ON", "-29.99", "Other")]))
    again = next(t for t in txns(app) if t["date"] == "2026-10-05")
    if again["status"] == "review":
        assert call(app, "approve", txn_id=again["id"], category_id=ids["Subscriptions"])["subscription_prompt"] is None


def test_always_ask_separates_a_payee_and_turning_it_off_groups_it_again(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, card_csv([
        ("07/06/2026", "AMZN Mktp CA*P583Y1613 TORONTO O", "-47.14", "Other"),
        ("07/07/2026", "AMZN Mktp CA*HQ1F64JX3 TORONTO O", "-10.47", "Other"),
        ("07/08/2026", "AMZN Mktp CA*O23JY2NY3 TORONTO O", "-23.09", "Other"),
    ]))
    first = call(app, "inbox")["items"][0]
    call(app, "approve", txn_id=first["id"], category_id=ids["Household"], refile=False)
    second = call(app, "inbox")["items"][0]
    call(app, "approve", txn_id=second["id"], category_id=ids["Fun Money"], refile=False)  # now a mixed payee
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), card_csv([
        ("08/02/2026", "AMZN Mktp CA*5N17737T1 TORONTO O", "-16.47", "Other"),
        ("08/03/2026", "AMZN Mktp CA*569BD7OE0 TORONTO O", "-97.48", "Other"),
    ]))
    groups = lambda: [i["group"] for i in call(app, "inbox")["items"]]
    assert len(groups()) == 3 and len(set(groups())) == 1 and groups()[0]  # one card, even though it's mixed
    payee = call(app, "inbox")["items"][0]["payee_id"]
    call(app, "set_always_ask", payee, True)
    assert groups() == ["", "", ""]  # one card each
    call(app, "set_always_ask", payee, False)
    assert len(set(groups())) == 1 and groups()[0]  # together again
