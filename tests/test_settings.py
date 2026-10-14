"""Settings validation and persistence."""

from settings import DEFAULTS, Settings, validate


def test_validate_fills_defaults_and_drops_junk():
    assert validate(None) == DEFAULTS
    assert validate({"theme": "neon", "auto_import": "yes", "average_months": 5, "unknown": 1}) == DEFAULTS


def test_changes_persist(tmp_path):
    store = Settings(tmp_path / "settings.json")
    store.update({"theme": "dark", "auto_import": False, "average_months": 6})
    again = Settings(tmp_path / "settings.json").get()
    assert (again["theme"], again["auto_import"], again["average_months"]) == ("dark", False, 6)


def test_overspending_comes_out_this_month_unless_chosen_otherwise(tmp_path):
    assert DEFAULTS["overspending"] == "this_month"
    assert validate({"overspending": "sometime"})["overspending"] == "this_month"
    for rule in ("this_month", "next_month", "carry_over"):
        assert validate({"overspending": rule})["overspending"] == rule


def test_the_old_carry_over_switch_is_kept(tmp_path):
    """1.0.3 and 1.0.4 had an on/off switch: on means carry over, off the default."""
    (tmp_path / "settings.json").write_text('{"carry_overspending": true}')
    store = Settings(tmp_path / "settings.json")
    assert store.get()["overspending"] == "carry_over" and "carry_overspending" not in store.get()
    assert validate({"carry_overspending": False})["overspending"] == "this_month"
    store.update({"overspending": "next_month"})
    assert Settings(tmp_path / "settings.json").get()["overspending"] == "next_month"


def test_corrupt_file_uses_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    assert Settings(tmp_path / "settings.json").get() == DEFAULTS


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
