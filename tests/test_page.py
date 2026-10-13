"""Static checks on the page: every button action has code that handles it.

A button the click handler doesn't recognise does nothing at all when clicked, with no
error anywhere (the suggestion chips once did exactly that), so check it here.
"""

import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parent.parent / "Application Files" / "index.html").read_text(encoding="utf-8")


def test_every_action_name_is_handled():
    acts = set(re.findall(r'data-act="([a-z-]+)"', PAGE))
    assert acts, "no actions found"
    unhandled = [a for a in acts if f"act === '{a}'" not in PAGE]
    assert unhandled == []


def test_clickable_data_attributes_are_listened_for():
    """Attributes that make a button do something must appear in a click selector."""
    on_buttons = set(re.findall(r'<button[^>]*?\b(data-[a-z-]+)=', PAGE))
    carry_values = {"data-id", "data-index", "data-i", "data-dir", "data-yes", "data-kind", "data-account",
                    "data-sub", "data-what", "data-amount", "data-group", "data-import", "data-payee", "data-on",
                    "data-hidden", "data-cat"}  # read by the handler of the button's action, not clicked on
    selectors = " ".join(re.findall(r"closest\('([^']+)'\)", PAGE))
    handled_elsewhere = {"data-month", "data-theme-set", "data-avg", "data-move-group", "data-hide-cat",
                         "data-delete-cat", "data-delete-group", "data-split-cat", "data-split-del",
                         "data-split-add"}  # dataset checks in their own handlers
    missing = [a for a in on_buttons - carry_values - handled_elsewhere if f"[{a}" not in selectors]
    assert missing == []
    for attribute in handled_elsewhere:
        camel = re.sub(r"-([a-z])", lambda m: m.group(1).upper(), attribute[5:])
        assert f"dataset.{camel}" in PAGE or f"[{attribute}" in selectors, attribute


def test_suggestion_chips_file_the_card_when_clicked():
    assert re.search(r"closest\('\[data-act\], #main \[data-pick\]'\)", PAGE)
    assert "btn.dataset.pick" in PAGE
