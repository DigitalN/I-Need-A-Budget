# SPDX-License-Identifier: GPL-3.0-only
"""Moving the budget's start month is one undo step, and the page asks first. Made-up data only."""

from pathlib import Path

from helpers import call

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def test_start_here_is_an_undo_step(app):
    """Issue #54: ⌘Z used to undo the step before it, and the start stayed moved."""
    call(app, "add_account", "Wallet", "cash", 100000, "2026-07-31")
    call(app, "finish_onboarding", "2026-09")
    call(app, "set_budget_start", "2026-08")
    assert call(app, "bootstrap")["budget_start"] == "2026-08"
    assert call(app, "undo")["undone"] == "Change budget start"
    assert call(app, "bootstrap")["budget_start"] == "2026-09"
    assert call(app, "undo")["undone"] == "Add account Wallet"  # the step before it is still there


def test_finishing_onboarding_doesnt_add_a_change_budget_start_step(app):
    call(app, "add_account", "Wallet", "cash", 100000, "2026-07-31")
    call(app, "finish_onboarding", "2026-09")
    assert call(app, "bootstrap")["budget_start"] == "2026-09"
    assert call(app, "undo")["undone"] != "Change budget start"


def test_a_start_month_has_to_be_a_month(app):
    for bad in ("", "2026", "2026-13", "soon", None, "2026-1-1"):
        assert not app.dispatch("set_budget_start", [bad])["ok"], bad


def test_the_start_here_button_asks_first_and_offers_undo():
    handler = PAGE[PAGE.index("act === 'start-here'"):]
    handler = handler[:handler.index("act === 'toggle-hidden'")]
    assert "confirmBox(" in handler and "Every month from" in handler
    assert "action: undoAction" in handler
