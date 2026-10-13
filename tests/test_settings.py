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


def test_corrupt_file_uses_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{not json")
    assert Settings(tmp_path / "settings.json").get() == DEFAULTS


def test_downloads_folder(tmp_path):
    store = Settings(tmp_path / "settings.json")
    assert store.downloads_dir().name == "Downloads"
    store.update({"downloads_dir": str(tmp_path)})
    assert store.downloads_dir() == tmp_path
