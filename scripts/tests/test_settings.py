# SPDX-License-Identifier: GPL-3.0-only
"""Settings: validation, persistence, and an update never changing a setting a copy uses
(docs/adr/0007). Overspending and Usual's months are the budget's own (issue #153)."""

import json

import backups
from db import Database
from helpers import call
from service import BudgetService
from settings import (BUDGET_DEFAULTS, BUDGET_RAN_ON, DEFAULTS, RAN_ON, Settings, budget_settings, validate,
                      validate_budget)

# What a copy from before 1.4.1 has in settings.json when it has saved something since 1.1.1.
OLD_FILE = {"theme": "dark", "auto_import": False, "downloads_dir": "/tmp/sample downloads",
            "update_automatically": False, "average_months": 6, "overspending": "carry_over", "dashboard_months": 12}


def _old_budget(folder):
    """A budget from before 1.4.1: Overspending and Usual's months aren't in it."""
    db = Database(folder / "budget.db")
    with db.write() as c:
        c.execute("DELETE FROM meta WHERE key IN ('overspending', 'average_months')")
    db.close()


def _launch(folder):
    """The app starting on this folder: its settings, then its budget."""
    store = Settings(folder / "settings.json")
    return BudgetService(Database(folder / "budget.db"), store, folder / "data")


def _file(folder):
    return json.loads((folder / "settings.json").read_text())


def test_validate_fills_defaults_and_drops_junk():
    assert validate(None) == DEFAULTS
    assert validate({"theme": "neon", "auto_import": "yes", "dashboard_months": 5, "unknown": 1}) == DEFAULTS
    assert validate({"overspending": "carry_over", "average_months": 6}) == DEFAULTS  # the budget's, not this Mac's
    assert validate_budget({"overspending": "sometime", "average_months": 5}, BUDGET_DEFAULTS) == BUDGET_DEFAULTS
    assert validate_budget({"average_months": True}, BUDGET_DEFAULTS) == BUDGET_DEFAULTS
    for rule in ("this_month", "next_month", "carry_over"):
        assert validate_budget({"overspending": rule}, BUDGET_DEFAULTS)["overspending"] == rule


def test_changes_persist(tmp_path):
    store = Settings(tmp_path / "settings.json")
    store.update({"theme": "dark", "auto_import": False, "dashboard_months": 12})
    again = Settings(tmp_path / "settings.json").get()
    assert (again["theme"], again["auto_import"], again["dashboard_months"]) == ("dark", False, 12)
    assert store.update({"theme": "neon"})["theme"] == "dark"  # a value that isn't valid changes nothing


def test_a_new_copy_starts_with_the_defaults_and_a_new_budget_with_next_month(tmp_path):
    assert BUDGET_DEFAULTS == {"overspending": "next_month", "average_months": 3}  # issue #166
    app = _launch(tmp_path)
    assert _file(tmp_path) == DEFAULTS  # only what belongs to this Mac
    with app.db.read() as c:
        assert budget_settings(c) == BUDGET_DEFAULTS
    assert call(app, "get_settings") == {**DEFAULTS, **BUDGET_DEFAULTS}
    assert call(app, "month", "2026-10")["overspending"] == "next_month"


def test_an_upgrade_leaves_every_saved_setting_alone(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(OLD_FILE))
    _old_budget(tmp_path)
    app = _launch(tmp_path)
    mine = {k: v for k, v in OLD_FILE.items() if k in DEFAULTS}
    assert app.settings.get() == mine
    assert _file(tmp_path) == OLD_FILE  # the two stay where 1.4.0 reads them
    assert call(app, "get_settings") == {**mine, "overspending": "carry_over", "average_months": 6}
    assert call(app, "month", "2026-10")["overspending"] == "carry_over"
    # The next launch changes nothing: it's the budget's now, and this Mac's file stays as it is.
    app.db.close()
    written = (tmp_path / "settings.json").read_text()
    app = _launch(tmp_path)
    assert (tmp_path / "settings.json").read_text() == written
    assert call(app, "get_settings")["overspending"] == "carry_over"


def test_a_missing_setting_keeps_what_the_copy_was_using(tmp_path):
    """1.0.0's settings.json, never saved again: no Overspending (it ran on This month, the
    default from 1.0.5 to 1.4.0) and no Dashboard months."""
    (tmp_path / "settings.json").write_text(json.dumps(
        {"theme": "system", "auto_import": True, "downloads_dir": "", "update_automatically": True, "average_months": 3}))
    _old_budget(tmp_path)
    app = _launch(tmp_path)
    assert _file(tmp_path) == {**RAN_ON, "overspending": "this_month", "average_months": 3}
    assert call(app, "month", "2026-10")["overspending"] == "this_month"
    assert call(app, "get_settings")["dashboard_months"] == 6


def test_the_old_carry_over_switch_is_kept(tmp_path):
    """1.0.3 and 1.0.4 had an on/off switch: on means carry over, off what came after it (This month)."""
    for switch, rule in ((True, "carry_over"), (False, "this_month")):
        folder = tmp_path / str(switch)
        folder.mkdir()
        (folder / "settings.json").write_text(json.dumps({"carry_overspending": switch}))
        _old_budget(folder)
        app = _launch(folder)
        assert call(app, "get_settings")["overspending"] == rule
        assert "carry_overspending" not in _file(folder) and _file(folder)["overspending"] == rule
        call(app, "save_settings", {"overspending": "next_month"})
        app.db.close()
        assert call(_launch(folder), "get_settings")["overspending"] == "next_month"


def test_going_back_a_version_and_forward_again_changes_no_setting(tmp_path):
    """After the move, settings.json still has the two where 1.4.0 reads them, as they were, whatever
    the budget is changed to; and a setting this version doesn't know (a later one's) is kept."""
    (tmp_path / "settings.json").write_text(json.dumps({**OLD_FILE, "a_later_setting": [1, 2]}))
    _old_budget(tmp_path)
    app = _launch(tmp_path)
    call(app, "save_settings", {"overspending": "next_month", "theme": "light", "a_later_setting": "junk"})
    assert call(app, "get_settings")["overspending"] == "next_month"
    written = _file(tmp_path)
    assert (written["overspending"], written["average_months"]) == ("carry_over", 6)  # what 1.4.0 runs on
    assert written["theme"] == "light" and written["a_later_setting"] == [1, 2]
    app.db.close()
    assert call(_launch(tmp_path), "get_settings")["overspending"] == "next_month"  # the budget's own


def test_what_copies_ran_on_never_changes():
    """A copy that skips versions keeps what it was using: these are frozen, whatever the defaults become."""
    assert set(RAN_ON) == set(DEFAULTS) and set(BUDGET_RAN_ON) == set(BUDGET_DEFAULTS)
    assert RAN_ON == {"theme": "system", "auto_import": True, "downloads_dir": "", "update_automatically": True,
                      "dashboard_months": 6}
    assert BUDGET_RAN_ON == {"overspending": "this_month", "average_months": 3}


def test_a_backup_from_before_restored_later_takes_this_macs_values(tmp_path):
    """A budget from before 1.4.1 opened on a Mac that has moved its values over (a backup
    restored) takes what that Mac had then; on a new Mac, what a copy without them ran on."""
    (tmp_path / "settings.json").write_text(json.dumps(OLD_FILE))
    _old_budget(tmp_path)
    app = _launch(tmp_path)
    call(app, "save_settings", {"overspending": "next_month"})  # changed since, in the budget
    app.db.close()
    (tmp_path / "budget.db").unlink()
    _old_budget(tmp_path)  # the old backup, restored
    assert call(_launch(tmp_path), "get_settings")["overspending"] == "carry_over"

    new_mac = tmp_path / "new Mac"
    new_mac.mkdir()
    Settings(new_mac / "settings.json")  # a new copy
    _old_budget(new_mac)
    assert call(_launch(new_mac), "get_settings")["overspending"] == "this_month"


def test_changing_overspending_is_no_undo_step_and_counts_for_backups(app):
    group = call(app, "add_group", "Everyday")
    food = call(app, "add_category", group, "Food")
    call(app, "set_assigned", "2026-10", food, 5000)
    backups.at_quit(app.db, None)
    assert not backups.changed(app.db)
    out = app.dispatch("save_settings", [{"overspending": "carry_over"}])
    assert out["ok"] and "step" not in out and out["result"]["overspending"] == "carry_over"
    assert app.db.undo_label() == "Assign money"  # ⌘Z still undoes what came before
    assert backups.changed(app.db)
    call(app, "undo")
    assert call(app, "get_settings")["overspending"] == "carry_over"
    assert call(app, "month", "2026-10")["groups"][0]["categories"][0]["assigned"] == 0


def test_corrupt_file_uses_what_a_copy_without_settings_ran_on(tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    store = Settings(tmp_path / "settings.json")
    assert store.get() == RAN_ON and store.before() == BUDGET_RAN_ON
    assert (tmp_path / "settings.json").read_text() == "{not json"  # left as it is, to look at


def test_downloads_folder(tmp_path):
    store = Settings(tmp_path / "settings.json")
    assert store.downloads_dir().name == "Downloads"
    store.update({"downloads_dir": str(tmp_path)})
    assert store.downloads_dir() == tmp_path


def test_the_dashboards_months_are_remembered(tmp_path):
    assert DEFAULTS["dashboard_months"] == 6
    for junk in (5, True, "12", None):
        assert validate({"dashboard_months": junk})["dashboard_months"] == 6
    store = Settings(tmp_path / "settings.json")
    store.update({"dashboard_months": "all"})
    assert Settings(tmp_path / "settings.json").get()["dashboard_months"] == "all"
