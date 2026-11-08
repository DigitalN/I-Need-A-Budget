# SPDX-License-Identifier: GPL-3.0-only
"""Redraws don't replace what you're typing in, and a slow response doesn't draw over a newer one.
Static checks on the page (the page's behaviour is tried with the mouse in the dev preview)."""

import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def _function(name):
    return re.search(rf"^(?:async )?function {name}\(.*?^}}", PAGE, re.S | re.M).group(0)


def test_an_import_or_update_redraw_waits_for_any_box_in_the_page():
    """Issue #70: it waited only for a category or group name, so a balance, rate or payment box,
    the Inbox's search and the GitHub token box lost what you'd typed."""
    guard = PAGE[PAGE.index("const typingInMain"):PAGE.index("window.appEvent = (")]
    assert "closest('#main')" in guard and "input:not([type=checkbox])" in guard and "textarea" in guard
    assert "state.dragging" in guard and "#main .inline-name" in guard and "#dlg').open" in guard
    assert "focusout" in guard and "refreshLater" in guard  # and it runs when you leave the box
    event = PAGE[PAGE.index("window.appEvent = ("):PAGE.index("// ── wiring")]
    assert event.count("refreshWhenIdle()") == 2  # an import's and an update's
    assert "refresh();" not in event.replace("refreshWhenIdle()", "")


def test_the_budget_page_draws_only_its_newest_response_and_writes_to_the_month_on_screen():
    render = _function("renderBudget")
    assert "const seq = ++budgetSeq" in render and "if (seq !== budgetSeq) return" in render
    assert render.index("if (seq !== budgetSeq) return") < render.index("state.view = view")
    for write in ("set_assigned", "move_money", "assign_plan", "apply_plan", "catch_up", "category_detail"):
        for m in re.finditer(rf"call\('{write}',[^\n]*", PAGE):
            assert "state.month" not in m.group(0), m.group(0)
    assert "const shownMonth = () => (state.view && state.view.month)" in PAGE
