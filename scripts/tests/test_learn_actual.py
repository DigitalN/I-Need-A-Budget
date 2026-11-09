# SPDX-License-Identifier: GPL-3.0-only
"""Learning from an Actual Budget export: once per example, and one undo step. Made-up data only."""

from helpers import call, write

ACTUAL = """Account,Date,Payee,Notes,Category_Group,Category,Amount,Split_Amount,Cleared
Mastercard,2026-07-30,Sample Cafe,SAMPLE CAFE CALGARY AB,Every Day Spending,Eating Out,-12.40,0,Cleared
Mastercard,2026-07-28,Sample Phone,SAMPLE PHONE CO CALGARY,Monthly Bills,Phone,-39.00,0,Cleared
"""
TWINS = """Account,Date,Payee,Notes,Category_Group,Category,Amount,Split_Amount,Cleared
Mastercard,2026-07-30,Sample Cafe,SAMPLE CAFE CALGARY AB,Every Day Spending,Eating Out,-4.00,0,Cleared
Mastercard,2026-07-30,Sample Cafe,SAMPLE CAFE CALGARY AB,Every Day Spending,Eating Out,-4.00,0,Cleared
"""


def _examples(app):
    with app.db.read() as c:
        return c.execute("SELECT COUNT(*) FROM examples").fetchone()[0]


def test_learning_the_same_export_twice_keeps_the_examples_at_two(app):
    """Issue #67: it went from 2 to 4, doubling that file's weight in every suggestion."""
    path = write(app.test_downloads, "Actual export.csv", ACTUAL)
    assert call(app, "import_files", [str(path)])["learnt"]["examples"] == 2
    again = call(app, "import_files", [str(path)])
    assert again["learnt"]["examples"] == 0 and again["learnt"]["already_learnt"] == 2
    assert _examples(app) == 2


def test_two_identical_rows_in_one_export_are_both_learnt_once(app):
    path = write(app.test_downloads, "Actual export.csv", TWINS)
    assert call(app, "import_files", [str(path)])["learnt"]["examples"] == 2
    assert call(app, "import_files", [str(path)])["learnt"]["examples"] == 0
    assert _examples(app) == 2
    # A newer export that has the same two rows and a third: only the new one is added.
    more = write(app.test_downloads, "Actual export 2.csv", TWINS + TWINS.splitlines()[1] + "\n")
    assert call(app, "import_files", [str(more)])["learnt"]["examples"] == 1
    assert _examples(app) == 3


def test_learning_is_one_undo_step_that_takes_its_categories_with_it(app):
    before = {c["name"] for g in call(app, "categories") for c in g["categories"]}
    path = write(app.test_downloads, "Actual export.csv", ACTUAL)
    out = call(app, "import_files", [str(path)])
    assert out["learnt"]["categories_created"] == ["Eating Out", "Phone"]
    assert call(app, "bootstrap")["undo"] == "Learn from Actual"
    step = call(app, "undo_step")  # a big step: ⌘Z asks first (issue #42)
    assert step["big"] and step["undoes"] == ("What the app learnt from Actual export.csv is forgotten. "
                                              "The 2 categories it made are deleted.")
    call(app, "undo")
    assert _examples(app) == 0
    assert {c["name"] for g in call(app, "categories") for c in g["categories"]} == before


def test_what_the_recheck_categorizes_with_it_goes_back_with_it(app):
    """The re-check after learning is part of the step: undoing it sends those back to the Inbox."""
    from helpers import CARD_FILE, card_csv
    call(app, "import_files", [str(write(app.test_downloads, CARD_FILE,
                                         card_csv([("08/02/2026", "SAMPLE CAFE CALGARY AB", "-12.40", "Restaurant")])))])
    assert len(call(app, "inbox")["items"]) == 1
    out = call(app, "import_files", [str(write(app.test_downloads, "Actual export.csv", ACTUAL))])
    assert out["rechecked"] == 1 and call(app, "inbox")["items"] == []
    assert call(app, "undo_step")["undoes"].endswith("1 transaction it categorized goes back to the Inbox.")
    call(app, "undo")
    assert len(call(app, "inbox")["items"]) == 1
