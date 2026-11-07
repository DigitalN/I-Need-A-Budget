"""CSV set-up guesses: what it can tell from a file, and what it leaves for the person to pick.
Made-up data only."""

from pathlib import Path

import pytest

import csvlayout
from helpers import call, write
from util import parse_date

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")

AMEX_CHARGES = """Date,Description,Amount
10/01/2026,SAMPLE AIRLINE,500.00
10/03/2026,SAMPLE HOTEL,200.00
10/05/2026,SAMPLE RESTAURANT,64.20
10/06/2026,SAMPLE TAXI,18.00
"""
AMEX_WITH_A_REFUND = AMEX_CHARGES + "10/07/2026,SAMPLE SHOP,-25.00\n"
YY_MM_DD = """Date,Description,Amount
26-10-01,SAMPLE GROCER,-61.20
26-10-02,SAMPLE EMPLOYER PAY,1500.00
26-10-05,SAMPLE CAFE,-4.50
"""
AMBIGUOUS = """Date,Description,Amount
01/02/2026,SAMPLE GROCER,-61.20
03/04/2026,SAMPLE EMPLOYER PAY,1500.00
05/06/2026,SAMPLE CAFE,-4.50
"""
# TD-style, no header: date, description, debit, credit, balance
WITH_DEPOSITS = ("10/01/2026,SAMPLE GROCER,61.20,,938.80\n10/02/2026,SAMPLE EMPLOYER PAY,,1500.00,2438.80\n"
                 "10/03/2026,SAMPLE CAFE,4.50,,2434.30\n")
NO_DEPOSITS = ("11/01/2026,SAMPLE GROCER,61.20,,2373.10\n11/02/2026,SAMPLE GYM,30.00,,2343.10\n"
               "11/03/2026,SAMPLE CAFE,4.50,,2338.60\n")


def test_a_file_of_mostly_positive_amounts_says_so_for_a_card(app):
    """Issue #61: an Amex-style file of only charges came in as money in."""
    for text in (AMEX_CHARGES, AMEX_WITH_A_REFUND):
        sniffed = csvlayout.sniff(text)
        assert sniffed["mostly_positive"] is True
    assert csvlayout.sniff(YY_MM_DD)["mostly_positive"] is False
    path = write(app.test_downloads, "Amex.csv", AMEX_WITH_A_REFUND)
    assert call(app, "csv_setup", str(path))["mostly_positive"] is True
    # The pop-up makes "Money out shows as Positive" the choice for a card with such a file.
    assert "info.mostly_positive" in PAGE and "isCard" in PAGE


def test_a_two_digit_year_first_is_year_month_day():
    """Issue #61: 26-10-01 was read as 2001-10-26."""
    sniffed = csvlayout.sniff(YY_MM_DD)
    assert sniffed["guess"]["date_order"] == "ymd"
    assert csvlayout.read(YY_MM_DD, sniffed["guess"]) == [
        ("2026-10-01", -6120, "SAMPLE GROCER"), ("2026-10-02", 150000, "SAMPLE EMPLOYER PAY"),
        ("2026-10-05", -450, "SAMPLE CAFE")]
    assert parse_date("26-10-01", "ymd") == "2026-10-01" and parse_date("2026-10-01", "ymd") == "2026-10-01"


def test_a_file_that_cant_say_which_comes_first_leaves_it_to_the_person():
    assert csvlayout.sniff(AMBIGUOUS)["guess"]["date_order"] is None
    assert csvlayout.sniff(AMBIGUOUS.replace("03/04/2026", "13/04/2026"))["guess"]["date_order"] == "dmy"
    assert csvlayout.sniff(AMBIGUOUS.replace("03/04/2026", "04/13/2026"))["guess"]["date_order"] == "mdy"
    layout = {**csvlayout.sniff(AMBIGUOUS)["guess"]}
    with pytest.raises(csvlayout.LayoutError, match="how the dates are written"):
        csvlayout.clean_layout(layout, 3)
    assert csvlayout.clean_layout({**layout, "date_order": "mdy"}, 3)["date_order"] == "mdy"
    assert "value=\"\"" in PAGE[PAGE.index('id="su-order"'):][:400] or "Pick…" in PAGE[PAGE.index('id="su-order"'):][:400]


def test_an_empty_column_matches_a_money_column_so_the_next_download_finds_its_layout():
    deposits, none = csvlayout.sniff(WITH_DEPOSITS), csvlayout.sniff(NO_DEPOSITS)
    assert csvlayout.signature(deposits) == csvlayout.signature(none)
    assert (none["guess"]["out_col"], none["guess"]["in_col"]) == (2, 3)  # debit and the empty credit column


def test_the_next_download_with_no_deposits_imports_on_its_own(app):
    app.db.set_meta("onboarded", "1")
    first = write(app.test_downloads, "accountactivity.csv", WITH_DEPOSITS)
    setup = call(app, "csv_setup", str(first))
    call(app, "setup_csv", str(first), setup["layout"], {"name": "Sample Chequing", "kind": "chequing", "bank": "TD"})
    later = write(app.test_downloads, "accountactivity.csv", NO_DEPOSITS)
    [result] = call(app, "import_files", [str(later)])["results"]
    assert (result["account_name"], result["added"]) == ("Sample Chequing", 3)
    amounts = sorted(t["amount"] for t in call(app, "transactions", {})["items"])
    assert amounts == [-6120, -6120, -3000, -450, -450, 150000]  # debits are money out in both


def test_a_layout_saved_with_the_old_signature_is_still_found(app):
    """Layouts saved before the empty-column rule have 'e' in their signature."""
    app.db.set_meta("onboarded", "1")
    first = write(app.test_downloads, "accountactivity.csv", NO_DEPOSITS)
    sniffed = csvlayout.sniff(NO_DEPOSITS)
    old = f"n:{sniffed['width']}:" + "".join(sniffed["types"])
    assert "e" in old and old != csvlayout.signature(sniffed)
    layout = {**sniffed["guess"], "out_col": 2, "in_col": 3, "amount_col": None}
    with app.db.write() as c:
        c.execute("INSERT INTO csv_layouts (signature, has_header, date_col, date_order, desc_cols, amount_col, out_col, "
                  "in_col, negate) VALUES (?, 0, ?, 'mdy', '1', NULL, 2, 3, 0)", (old, layout["date_col"]))
    import importer
    with app.db.read() as c:
        assert importer.saved_layout(c, sniffed)["out_col"] == 2
