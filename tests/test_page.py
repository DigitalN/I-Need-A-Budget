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
                    "data-hidden", "data-cat", "data-into"}  # read by the handler of the button's action, not clicked on
    selectors = " ".join(re.findall(r"closest\('([^']+)'\)", PAGE))
    handled_elsewhere = {"data-month", "data-theme-set", "data-move-group", "data-hide-cat",
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


def test_a_categorys_monthly_amount_is_called_budgeted():
    """The maintainer's word for it is Budgeted, never "Each month"."""
    assert "Each month" not in PAGE
    assert '<th class="r">Budgeted</th><th class="r">Assigned</th>' in PAGE
    assert "Monthly Budgeted Amount" in PAGE and "data-copy-budget" in PAGE and "data-copy-spent" in PAGE


def test_amount_boxes_start_empty_and_redraws_keep_your_place():
    """An Assigned box with nothing in it is empty (0.00 is only its placeholder), clicking
    into an amount selects it, and redrawing the Budget page doesn't scroll it back up."""
    assert 'value="${moneyOrBlank(c.assigned)}" placeholder="0.00"' in PAGE
    assert "scrollIntoView({ inline: 'center'" not in PAGE  # it scrolled the page to the top
    assert "main.scrollTop = scrolled" in PAGE and "keepSelected" in PAGE


def test_used_categories_and_accounts_are_hidden_or_closed_not_deleted():
    """A ✕ shows only for a category or account never used, and for an empty group;
    Edit… closes or reopens an account."""
    assert "${c.used ? NO_DELETE : `<button type=\"button\" class=\"icon-btn danger\" data-delete-cat=" in PAGE
    assert "${g.categories.length ? NO_DELETE : `<button type=\"button\" class=\"icon-btn danger\" data-delete-group=" in PAGE
    assert "${a.used ? '' : `<button class=\"icon-btn danger\" data-act=\"delete-account\"" in PAGE
    assert "label: 'Close account', value: 'close'" in PAGE and "label: 'Reopen account', value: 'reopen'" in PAGE
    assert "went back to the Inbox" not in PAGE


def test_the_inbox_groups_and_accepts_as_the_inbox_module_says():
    """Inbox.view gives each item its final group key and guess: the page has no
    exceptions of its own, and its calls are the module's (categorize, accept_all, recheck)."""
    assert "if (it.group) {" in PAGE and "filter(i => i.guess)" in PAGE
    assert "startsWith('Looks like a')" not in PAGE and "!i.similar_payee" not in PAGE
    for old in ("'approve'", "'approve_many'", "'refile'", "also_filed", "subscription_prompt"):
        assert old not in PAGE, old
    assert "call('categorize'" in PAGE and "call('accept_all'" in PAGE
    assert PAGE.count("if (r.question) ") >= 3  # "What's this for?" from the Inbox and the Transactions page
