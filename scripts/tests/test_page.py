"""Static checks on the page: every button action has code that handles it.

A button the click handler doesn't recognise does nothing at all when clicked, with no
error anywhere (the suggestion chips once did exactly that), so check it here.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


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
    handled_elsewhere = {"data-month", "data-theme-set", "data-confirm", "data-hide-cat",
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
    assert "main.scrollTop = scrolled" in PAGE
    # selected on mouse-down, also in the box Return moved you to; a second click places the cursor
    assert "if (el === document.activeElement && el === clickedInto) return;" in PAGE


def test_used_categories_and_accounts_are_hidden_or_closed_not_deleted():
    """A category's ✕ (on every one you made) always asks: one never used is deleted, a
    used one can only be hidden. An empty group's ✕ asks too. An account's ✕ shows only
    for one never used; Edit… closes or reopens an account."""
    assert "${c.own ? NO_DELETE : `<button type=\"button\" class=\"icon-btn danger\" data-delete-cat=" in PAGE
    assert "It's never been used, so nothing else changes." in PAGE and "so it can't be deleted." in PAGE
    assert "(g.categories.length ? NO_DELETE : `<button type=\"button\" class=\"icon-btn danger\" data-delete-group=" in PAGE
    assert "Delete the group" in PAGE
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


def test_amount_boxes_do_math():
    """Issue #28: in an Assigned box, +50 adds 50 to what's there (−, * and / too), and any
    amount box takes a sum. Elsewhere a leading minus is a negative amount. Runs the page's
    own parseMoney in Node, so it needs Node installed."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node")
    source = re.search(r"^function parseMoney\(.*?^}$", PAGE, re.S | re.M).group(0)
    cases = [  # (typed, what the box had before, cents)
        ("120.00", None, 12000), ("1,234.56", None, 123456), ("$45", None, 4500), ("", None, None),
        ("-45", None, -4500), ("−45", None, -4500), ("+50", None, 5000), ("abc", None, "NaN"),
        ("120+35.50", None, 15550), ("3*45", None, 13500), ("300 / 4", None, 7500), ("(120+30)/2", None, 7500),
        ("100/3", None, 3333), ("1.005", None, 101), ("-0.004", None, 0),
        ("5/0", None, "NaN"), ("50+", None, "NaN"), ("(50", None, "NaN"), ("50)", None, "NaN"),
        ("+50", 12000, 17000), ("-20", 12000, 10000), ("−20", 12000, 10000), ("*2", 12000, 24000),
        ("/2", 12000, 6000), ("÷4", 12000, 3000), ("80", 12000, 8000), ("+10+5", 12000, 13500),
        ("-50", 0, -5000), ("+25.25", -1000, 1525),
    ]
    script = (source + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([text, base]) => "
              "parseMoney(text, base === null ? undefined : base)).map(v => Number.isNaN(v) ? 'NaN' : v)));")
    got = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    assert [(text, base, value) for (text, base, _), value in zip(cases, got)] == cases


def _box_script(body):
    """Node script: the page's own amount-box helpers, a fake box, then `body`."""
    parts = [re.search(rf"^function {name}\(.*?^}}$", PAGE, re.S | re.M).group(0)
             for name in ("parseMoney", "putIn", "putBack", "noteBefore", "amountIn", "isMath")]
    parts += [re.search(rf"^const {name} = [^\n]*$", PAGE, re.M).group(0)
              for name in ("moneyPlain", "isRelative", "shownIn", "typedIn")]
    fake = """
function box(text, { relative = true } = {}) {  // an <input> the page drew with `text`
  return { value: text, defaultValue: text, dataset: {}, matches: s => relative && s === '[data-relative]' };
}
function focusIn(el) { noteBefore(el); }
function typeIn(el, text) { el.value = text; }
"""
    return "\n".join(parts) + fake + body


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node")
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)


def test_a_negative_assigned_amount_is_an_amount_until_you_type():
    """Issue #45: an Assigned box drawn with -50.00 is an amount, not "subtract 50": clicking
    in and out, Tab, Escape or quitting leave it at -$50.00. Only text you typed is math."""
    got = _run_node(_box_script("""
const out = {};
let a = box('-50.00');                                   // untouched, never focused
out.untouched = [amountIn(a), isMath(a)];
a = box('-50.00'); focusIn(a);                           // clicked into, nothing typed
out.focused = [amountIn(a), isMath(a)];
a = box('-50.00'); focusIn(a); typeIn(a, '+10');
out.plus = amountIn(a);
a = box('-50.00'); focusIn(a); typeIn(a, '-10');
out.minus = amountIn(a);
a = box('-50.00'); focusIn(a); typeIn(a, '-10'); putBack(a);
out.escape = [a.value, amountIn(a), isMath(a)];
a = box('-50.00'); focusIn(a); typeIn(a, '*2');
out.times = amountIn(a);
a = box(''); focusIn(a); typeIn(a, '-50');
out.empty = amountIn(a);
a = box('120.00'); focusIn(a); typeIn(a, '-20');
out.food = amountIn(a);
a = box('120.00'); focusIn(a); typeIn(a, '120+35.50');
out.sum = [amountIn(a), isMath(a)];
// the page puts a resolved amount in the box: a later pass starts from it
a = box('-50.00'); focusIn(a); typeIn(a, '+10'); putIn(a, moneyPlain(-4000), -4000); focusIn(a);
out.resolved = [amountIn(a), isMath(a)];
a = box('-40.00'); focusIn(a); typeIn(a, '-10');
out.resolvedThenMinus = amountIn(a);
console.log(JSON.stringify(out));
"""))
    assert got["untouched"] == [-5000, False]
    assert got["focused"] == [-5000, False]
    assert got["plus"] == -4000
    assert got["minus"] == -6000
    assert got["escape"] == ["-50.00", -5000, False]
    assert got["times"] == -10000
    assert got["empty"] == -5000
    assert got["food"] == 10000
    assert got["sum"] == [15550, True]
    assert got["resolved"] == [-4000, False]
    assert got["resolvedThenMinus"] == -5000


def test_use_puts_an_amount_in_the_budgeted_box_even_when_negative():
    """Issue #45: Use with a negative average (refunds make one) puts that amount in the
    Budgeted box. It isn't "take 12 off what was there"."""
    got = _run_node(_box_script("""
const budgeted = box('100.00'); focusIn(budgeted);
putIn(budgeted, moneyPlain(-1200), -1200);               // what the Use button does
const out = { used: [budgeted.value, amountIn(budgeted), isMath(budgeted)] };
const cleared = box('100.00'); focusIn(cleared);
putIn(cleared, '', 0);                                   // Clear
out.cleared = [cleared.value, amountIn(cleared)];
const plain = box('100.00', { relative: false }); typeIn(plain, '-45');
out.plain = amountIn(plain);
console.log(JSON.stringify(out));
"""))
    assert got["used"] == ["-12.00", -1200, False]
    assert got["cleared"] == ["", None]
    assert got["plain"] == -4500


def test_relative_boxes_and_escape():
    """Assigned, Budgeted and Assign money…'s Assign column work from what's in them;
    leaving a box with math in it shows the amount; Escape puts an Assigned box back."""
    assert 'data-assign="${c.id}" data-relative' in PAGE
    assert 'id="monthly-input"' in PAGE and re.search(r'id="monthly-input"[^>]*data-relative', PAGE)
    assert 'data-i="${i}" data-relative' in PAGE
    assert "const cents = amountIn(input);" in PAGE and "const cents = amountIn(monthlyInput);" in PAGE
    assert "amount: amountIn(el) || 0" in PAGE
    assert "putBack(e.target);" in PAGE


def test_categories_are_edited_on_the_budget_page():
    """No Edit categories… pop-up: categories are dragged (⋮⋮), added (+) and renamed (✎)
    on the Budget page, each group its own <tbody> so dragging one takes its categories
    along. Onboarding keeps its own list, bound to an element made each time it's drawn
    (bound to the shared pop-up, its handlers piled up and one Return added a category
    once per opening)."""
    assert "edit-categories" not in PAGE and "openCategoryEditor" not in PAGE
    assert '<tbody class="grp" data-group="${g.id}" data-role="${g.role}">' in PAGE
    for tool in ("data-drag-group", "data-drag-cat", "data-group-plus", "data-cat-plus", "data-group-pencil", "data-cat-pencil",
                 "data-group-x"):
        assert tool in PAGE, tool
    assert ".btable tr:hover .tool, .btable tr:focus-within .tool { opacity: 1; transition: none;" in PAGE  # the row you're on
    assert ".btable .tool { opacity: 0; transition: opacity .5s ease;" in PAGE  # fading over 0.5 s
    assert "nearTools" not in PAGE  # no reach around the pointer: too busy
    assert "beginDrag(e, { root: () => $('.btable'), redraw: renderBudget })" in PAGE
    assert "bindCategoryEditor($('#ob-cats'))" in PAGE and "body.dataset.bound" not in PAGE


def test_keys_the_page_acts_on_are_marked_handled():
    """A key the page acts on calls preventDefault: otherwise the app's window (WebKit)
    passes it on, nothing takes it, and macOS plays its warning sound. It did for Escape
    closing the category pop-over."""
    lines = PAGE.splitlines()
    for i, line in enumerate(lines):
        if "key === 'Escape'" in line:
            assert "preventDefault" in " ".join(lines[i:i + 3]), f"line {i + 1}: {line.strip()}"



def test_quitting_saves_what_you_are_typing_and_asks_only_when_it_cant():
    """The close button and ⌘Q don't ask "Quit?": the page saves the box you're in, and
    asks only about a pop-up with changes or an entry that can't be saved."""
    main = (Path(__file__).resolve().parents[2] / "Application Files" / "main.py").read_text(encoding="utf-8")
    assert "NSAlert" not in main and "Quit {APP_NAME}?" not in main
    assert "window.quitRequested" in main and "def quit(self)" in main and "def asking_before_quit(self)" in main
    assert "window.quitRequested = () =>" in PAGE
    # "finish", not "save": nothing suggests what isn't asked about might not be saved
    assert "You need to finish this before quitting" in PAGE and "${esc(d.quitCheck.label)} and quit" in PAGE
    for label in ("Keep editing", "Quit anyway", "Go back"):
        assert label in PAGE, label
    # a refused name stays in its box to fix, rather than going back
    assert "input.commitTyping = () => finish('enter')" in PAGE and "nameInput.commitTyping = () => saveName(true)" in PAGE
    # a pop-up without changes doesn't ask; a category picked counts as a change
    assert "fields() !== opened" in PAGE and "openDialog.dataset.picked = '1'" in PAGE
    # call() and tryCall() are tracked, so quitting waits for their answers
    assert "await track(api.call(" in PAGE and "await track(window.pywebview.api.call(" in PAGE
