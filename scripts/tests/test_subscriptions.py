"""Finding subscriptions, naming them, and asking about new prices."""

from helpers import CARD_FILE, call, card_csv, categories, txns, write
from subscriptions import _day_spread

CATS = {"Bills": ["Subscriptions"], "Everyday": ["Food", "Fun Money"]}


def imp(app, name, rows):
    return call(app, "import_files", [str(write(app.test_downloads, name, card_csv(rows)))])


def suggested(app):
    return {(s["name"], s["amount"]): s for s in call(app, "subscriptions")["suggested"]}


def test_monthly_charges_are_found_but_a_regular_cafe_is_not(app):
    categories(app, CATS)
    imp(app, CARD_FILE, [
        ("07/13/2026", "NEWSGROUP SAMPLE WINTER PARK FL U", "-11.49", "Foreign Currency Spend"),
        ("08/13/2026", "NEWSGROUP SAMPLE WINTER PARK FL U", "-11.31", "Foreign Currency Spend"),
        ("09/13/2026", "NEWSGROUP SAMPLE WINTER PARK FL U", "-11.25", "Foreign Currency Spend"),
        ("07/29/2026", "SQ *SAMPLE COFFEE CALGARY", "-10.50", "Restaurant"),
        ("08/14/2026", "SQ *SAMPLE COFFEE CALGARY", "-10.25", "Restaurant"),
        ("08/27/2026", "SQ *SAMPLE COFFEE CALGARY", "-10.98", "Restaurant"),
        ("09/24/2026", "SQ *SAMPLE COFFEE CALGARY", "-10.60", "Restaurant"),
    ])
    found = suggested(app)
    assert ("Newsgroup Sample", 1125) in found
    assert found[("Newsgroup Sample", 1125)]["cadence"] == "monthly"
    assert not any(name == "Sample Coffee" for name, _ in found)


def test_two_prices_from_one_payee_are_two_subscriptions_with_their_own_names(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, [
        ("07/04/2026", "APPLE.COM/BILL TORONTO ON", "-36.99", "Bill Payment"),
        ("08/04/2026", "APPLE.COM/BILL TORONTO ON", "-36.99", "Bill Payment"),
        ("09/04/2026", "APPLE.COM/BILL TORONTO ON", "-38.99", "Bill Payment"),
        ("07/28/2026", "APPLE.COM/BILL TORONTO ON", "-12.99", "Bill Payment"),
        ("08/28/2026", "APPLE.COM/BILL TORONTO ON", "-12.99", "Bill Payment"),
        ("09/28/2026", "APPLE.COM/BILL TORONTO ON", "-12.99", "Bill Payment"),
    ])
    found = suggested(app)
    bundle, storage = found[("Apple", 3899)], found[("Apple", 1299)]
    assert bundle["previous_amount"] == 3699  # the price went up
    call(app, "track_subscription", candidate=bundle, name="Apple One", category_id=ids["Subscriptions"])
    call(app, "track_subscription", candidate=storage, name="iCloud+", category_id=ids["Subscriptions"])
    names = {(t["amount"], t["subscription_name"]) for t in txns(app)}
    assert (-1299, "iCloud+") in names and (-3899, "Apple One") in names
    assert all(t["status"] == "done" and t["category_name"] == "Subscriptions" for t in txns(app))

    # Next month: the known prices file themselves; anything else from Apple is a question.
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), [
        ("10/28/2026", "APPLE.COM/BILL TORONTO ON", "-12.99", "Bill Payment"),
        ("10/04/2026", "APPLE.COM/BILL TORONTO ON", "-42.99", "Bill Payment"),
        ("10/05/2026", "APPLE.COM/BILL TORONTO ON", "-1.39", "Bill Payment"),
    ])
    october = {t["amount"]: t for t in txns(app, month="2026-10")}
    assert (october[-1299]["subscription_name"], october[-1299]["status"]) == ("iCloud+", "done")
    items = {i["amount"]: i for i in call(app, "inbox")["items"]}
    assert set(items) == {-4299, -139}
    rise = items[-4299]
    assert rise["subscription_choices"][0]["name"] == "Apple One" and rise["subscription_choices"][0]["due_now"]

    call(app, "link_subscription", rise["id"], rise["subscription_choices"][0]["id"])  # a price rise
    call(app, "categorize", txn_ids=[items[-139]["id"]], category_id=ids["Fun Money"], recheck_payee=True)      # an app: one-off
    october = {t["amount"]: t for t in txns(app, month="2026-10")}
    assert (october[-4299]["subscription_name"], october[-4299]["category_name"]) == ("Apple One", "Subscriptions")
    assert (october[-139]["subscription_name"], october[-139]["category_name"]) == (None, "Fun Money")
    tracked = {s["name"]: s for s in call(app, "subscriptions")["subscriptions"]}
    assert tracked["Apple One"]["amount"] == 4299 and tracked["Apple One"]["previous_amount"] == 3899


def test_foreign_currency_drift_still_matches(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, [
        ("08/13/2026", "NEWSGROUP SAMPLE WINTER PARK FL U", "-11.49", "Foreign Currency Spend"),
        ("09/13/2026", "NEWSGROUP SAMPLE WINTER PARK FL U", "-11.31", "Foreign Currency Spend"),
    ])
    call(app, "track_subscription", candidate=suggested(app)[("Newsgroup Sample", 1131)], category_id=ids["Subscriptions"])
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), [("10/11/2026", "NEWSGROUP SAMPLE WINTER PARK FL U", "-11.80", "Foreign Currency Spend")])
    october = txns(app, month="2026-10")[0]
    assert (october["subscription_name"], october["status"]) == ("Newsgroup Sample", "done")


def test_a_cancelled_subscription_that_charges_again_is_flagged(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, [("08/10/2026", "SAMPLE STREAMING TORONTO ON", "-16.99", ""),
                         ("09/10/2026", "SAMPLE STREAMING TORONTO ON", "-16.99", "")])
    sub = call(app, "track_subscription", candidate=suggested(app)[("Sample Streaming", 1699)], category_id=ids["Fun Money"])
    call(app, "cancel_subscription", sub, "2026-09-20")
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"), [("10/10/2026", "SAMPLE STREAMING TORONTO ON", "-16.99", "")])
    [item] = call(app, "inbox")["items"]
    assert "charged you again" in item["reason"]
    cancelled = call(app, "subscriptions")["cancelled"][0]
    assert cancelled["charged_after_cancel"][0]["date"] == "2026-10-10"


def test_adding_one_by_hand_links_past_charges(app):
    ids = categories(app, CATS)
    imp(app, CARD_FILE, [("09/01/2026", "Kindle Svcs TORONTO ON", "-9.99", "Other")])
    call(app, "add_subscription", payee="Kindle", name="Kindle Unlimited", amount=999, category_id=ids["Fun Money"])
    [t] = txns(app)
    assert (t["subscription_name"], t["category_name"], t["status"]) == ("Kindle Unlimited", "Fun Money", "done")


def test_day_of_month_spread_wraps_around_month_ends():
    assert _day_spread(["2026-07-30", "2026-08-01", "2026-08-31"]) == 2
    assert _day_spread(["2026-07-08", "2026-08-04", "2026-09-01"]) == 7


def bank(app, name, rows):
    from helpers import bank_csv
    return call(app, "import_files", [str(write(app.test_downloads, name, bank_csv(rows)))])


def test_a_power_bill_that_changes_every_month_is_found_and_tracked(app):
    ids = categories(app, {"Bills": ["Utilities"]})
    util = "EFT Withdrawal to SAMPLE POWER"
    bank(app, "Chequing.CSV", [(d, util, util, a) for d, a in (
        ("06/24/2026", "-409.65"), ("07/21/2026", "-158.18"), ("08/18/2026", "-147.84"), ("09/17/2026", "-158.40"))])
    found = {s["name"]: s for s in call(app, "subscriptions")["suggested"]}
    power = found["Sample Power"]
    assert (power["pricing"], power["kind"], power["low"], power["high"]) == ("varies", "bill", 14784, 40965)
    call(app, "track_subscription", candidate=power, name="Power", category_id=ids["Utilities"])
    # Next month's bill is a new amount: it's still the power bill, with no question.
    bank(app, "Chequing-2.CSV", [("10/16/2026", util, util, "-201.37")])
    october = txns(app, month="2026-10")[0]
    assert (october["subscription_name"], october["category_name"], october["status"]) == ("Power", "Utilities", "done")
    tracked = call(app, "subscriptions")["bills"][0]
    assert tracked["pricing"] == "varies" and tracked["previous_amount"] is None
    assert tracked["low"] == 14784 and tracked["last_amount"] == 20137


def test_currency_wobble_is_not_a_price_change_but_a_real_rise_is(app):
    ids = categories(app, CATS)
    rows = [("08/13/2026", "-11.49"), ("09/13/2026", "-11.31"), ("10/11/2026", "-11.80")]
    imp(app, CARD_FILE, [(d, "NEWSGROUP SAMPLE WINTER PARK FL U", a, "Foreign Currency Spend") for d, a in rows])
    candidate = next(s for s in call(app, "subscriptions")["suggested"] if s["name"] == "Newsgroup Sample")
    assert candidate["pricing"] == "currency" and candidate["previous_amount"] is None
    call(app, "track_subscription", candidate=candidate, category_id=ids["Subscriptions"])
    assert call(app, "subscriptions")["subscriptions"][0]["previous_amount"] is None  # no false "was"
    # A real price rise (well past the exchange-rate wobble) is still a question.
    imp(app, CARD_FILE.replace(".CSV", "-2.CSV"),
        [("11/12/2026", "NEWSGROUP SAMPLE WINTER PARK FL U", "-15.99", "Foreign Currency Spend")])
    [item] = call(app, "inbox")["items"]
    assert item["subscription_choices"][0]["name"] == "Newsgroup Sample"


def test_a_charge_after_the_end_date_is_flagged(app):
    ids = categories(app, CATS)
    plan = "EFT Withdrawal to SAMPLE PAYPLAN"
    bank(app, "Chequing.CSV", [(f"{m:02d}/24/2026", plan, plan, "-38.50") for m in (7, 8, 9)])
    candidate = next(s for s in call(app, "subscriptions")["suggested"] if s["name"].startswith("Sample Payplan"))
    call(app, "track_subscription", candidate=candidate, name="Couch", category_id=ids["Subscriptions"],
         ends_on="2026-09-30")
    bank(app, "Chequing-2.CSV", [("10/24/2026", plan, plan, "-38.50")])
    [item] = call(app, "inbox")["items"]
    assert "should have ended on 2026-09-30" in item["reason"]
    bill = (call(app, "subscriptions")["bills"] + call(app, "subscriptions")["subscriptions"])[0]
    assert bill["ended"] and bill["charged_after_end"][0]["date"] == "2026-10-24" and bill["next_date"] is None
    call(app, "recheck")  # re-checking the inbox keeps the warning
    assert "should have ended" in call(app, "inbox")["items"][0]["reason"]
