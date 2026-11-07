"""Which sign means "you owe" in a card's file, settled with what the app already knows about the account
(issue #119). Made-up data only (card numbers built in code)."""

from helpers import call, card_number, write
from test_card_signs import card_ofx


def _file(app, name, text):
    return str(write(app.test_downloads, name, text))


def _balance(app, account=0):
    return call(app, "accounts")[account]["balance"]


def _amounts(app):
    return sorted(t["amount"] for t in call(app, "transactions", {})["items"])


SEPTEMBER = [("20260905", "-60.00", "SAMPLE GROCER", "S1"), ("20260912", "-40.00", "SAMPLE CAFE", "S2")]


def test_an_overpaid_card_is_in_your_favour(app):
    """The issue's case: a payment bigger than what was owed leaves the card $30.00 in credit, in the usual signs."""
    call(app, "import_files", [_file(app, "card-sep.ofx", card_ofx(SEPTEMBER, "-100.00", "20260930"))])
    october = [("20261005", "150.00", "PAYMENT - THANK YOU", "O1"), ("20261008", "-12.00", "SAMPLE BAKERY", "O2"),
               ("20261009", "-8.00", "SAMPLE DINER", "O3")]
    call(app, "import_files", [_file(app, "card-oct.ofx", card_ofx(october, "30.00", "20261010"))])
    assert _balance(app) == 3000


def test_a_bank_that_writes_what_you_owe_as_positive_is_read_by_the_balance_the_app_has(app):
    call(app, "import_files", [_file(app, "card-sep.ofx", card_ofx(SEPTEMBER, "-100.00", "20260930"))])
    october = [("20261005", "-30.00", "SAMPLE BAKERY", "O1"), ("20261006", "-20.00", "SAMPLE DINER", "O2")]
    call(app, "import_files", [_file(app, "card-oct.ofx", card_ofx(october, "150.00", "20261010"))])
    assert _balance(app) == -15000  # the rows take the $100.00 owed to $150.00 owed: the file's 150.00 is what you owe


def test_the_first_file_with_charges_negative_and_a_positive_balance_asks_once(app):
    app.db.set_meta("onboarded", "1")
    path = _file(app, "card.ofx", card_ofx(SEPTEMBER, "120.50", "20260930"))
    out = call(app, "import_files", [path])
    assert out["results"] == [] and [f["needs"] for f in out["needs_setup"]] == ["balance_sign"]
    assert call(app, "accounts") == []  # nothing is imported until it's answered
    [question] = call(app, "file_balance_question", path)["questions"]
    assert question["amount"] == 12050 and question["name"]
    out = call(app, "answer_balance", path, "owed")
    assert out["results"][0]["added"] == 2 and _balance(app) == -12050


def test_in_your_favour_is_an_answer_too(app):
    app.db.set_meta("onboarded", "1")
    path = _file(app, "card.ofx", card_ofx(SEPTEMBER, "120.50", "20260930"))
    call(app, "import_files", [path])
    call(app, "answer_balance", path, "favour")
    assert _balance(app) == 12050


def test_a_negative_balance_or_an_account_with_a_balance_isnt_asked_about(app):
    app.db.set_meta("onboarded", "1")
    first = call(app, "import_files", [_file(app, "card-sep.ofx", card_ofx(SEPTEMBER, "-100.00", "20260930"))])
    assert first["needs_setup"] == [] and _balance(app) == -10000
    october = [("20261008", "-12.00", "SAMPLE BAKERY", "O2")]
    second = call(app, "import_files", [_file(app, "card-oct.ofx", card_ofx(october, "88.00", "20261010"))])
    assert second["needs_setup"] == [] and second["results"][0]["added"] == 1
    # The app has -$112.00; 88.00 is neither sign's: a real difference (#43), kept as the file wrote it, not asked.
    assert _balance(app) == 8800


def test_the_answer_is_remembered_for_the_account(app):
    app.db.set_meta("onboarded", "1")
    path = _file(app, "card.ofx", card_ofx(SEPTEMBER, "120.50", "20260930"))
    call(app, "import_files", [path])
    call(app, "answer_balance", path, "owed")
    october = [("20261012", "-10.00", "SAMPLE BAKERY", "O1")]
    out = call(app, "import_files", [_file(app, "card-oct.ofx", card_ofx(october, "40.00", "20261015"))])
    assert out["needs_setup"] == [] and _balance(app) == -4000  # 40.00 agrees with neither sign: the account's habit decides


def test_a_card_files_flipped_signs_are_decided_once_per_account(app):
    inverted = [("20261003", "30.00", "SAMPLE ARCADE", "A1"), ("20261005", "45.50", "SAMPLE HOTEL", "A2"),
                ("20261008", "-100.00", "PAYMENT - THANK YOU", "A3")]
    call(app, "import_files", [_file(app, "card-1.ofx", card_ofx(inverted, "0.00", "20261010"))])
    assert _amounts(app) == [-4550, -3000, 10000]
    # one purchase alone says nothing about the file's signs: the account's earlier answer does
    later = [("20261012", "12.00", "SAMPLE TAXI", "A4"), ("20261013", "-50.00", "PAYMENT - THANK YOU", "A5")]
    call(app, "import_files", [_file(app, "card-2.ofx", card_ofx(later, "0.00", "20261014"))])
    assert _amounts(app) == [-4550, -3000, -1200, 5000, 10000]


def test_rows_the_account_already_has_stop_a_file_of_refunds_being_flipped(app):
    credits = [("20261003", "30.00", "SAMPLE SHOP", "R1"), ("20261005", "45.50", "SAMPLE STORE", "R2"),
               ("20261008", "-20.00", "SAMPLE ARCADE", "R3")]
    call(app, "import_files", [_file(app, "card-1.ofx", card_ofx(credits, "0.00", "20261010"))])
    assert _amounts(app) == [-2000, 3000, 4550]
    again = credits + [("20261009", "10.00", "SAMPLE SHOP", "R4")]  # 3 of its 4 "purchases" are positive: it looks flipped
    call(app, "import_files", [_file(app, "card-2.ofx", card_ofx(again, "0.00", "20261010"))])
    assert _amounts(app) == [-2000, 1000, 3000, 4550]


def test_the_page_asks_about_a_card_balance_it_cant_read():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")
    assert "f.needs === 'balance_sign' ? askBalanceSign(f)" in page
    assert "call('file_balance_question'" in page.replace("tryCall('file_balance_question'", "call('file_balance_question'")
    assert "call('answer_balance', [f.path, v])" in page
