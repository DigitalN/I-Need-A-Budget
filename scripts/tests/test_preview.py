# SPDX-License-Identifier: GPL-3.0-only
"""The dev preview never reads or moves files from the real Downloads folder. Made-up files only,
in a throwaway HOME."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "preview.py"


def _preview():
    spec = importlib.util.spec_from_file_location("inab_preview", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads" / "SAMPLE statement.csv").write_text("Date,Description,Amount\n2026-10-01,SAMPLE,-1.00\n")
    monkeypatch.setenv("HOME", str(home))
    return home


def test_a_preview_without_a_downloads_folder_gets_its_own_empty_one(home, tmp_path):
    """Issue #74: it used ~/Downloads, and onboarding moved the bank files there into the preview's folder."""
    preview = _preview()
    data = tmp_path / "preview-data"
    settings = preview.prepare(data)
    assert settings.downloads_dir() == data / "downloads"
    assert (data / "downloads").is_dir() and list((data / "downloads").iterdir()) == []
    assert settings.downloads_dir() != Path.home() / "Downloads"
    assert [p.name for p in (home / "Downloads").iterdir()] == ["SAMPLE statement.csv"]  # untouched
    # The next start keeps it.
    assert preview.prepare(data).downloads_dir() == data / "downloads"


def test_a_folder_chosen_in_the_preview_settings_is_kept(home, tmp_path):
    preview = _preview()
    data = tmp_path / "preview-data"
    data.mkdir()
    mine = tmp_path / "copies of bank files"
    mine.mkdir()
    (data / "settings.json").write_text('{"downloads_dir": "%s"}' % mine)
    assert preview.prepare(data).downloads_dir() == mine


def test_the_preview_refuses_the_real_downloads_folder(home, tmp_path):
    preview = _preview()
    for chosen in (str(home / "Downloads"), "~/Downloads", str(home / "Downloads") + "/"):
        data = tmp_path / ("data" + str(abs(hash(chosen))))
        data.mkdir()
        (data / "settings.json").write_text('{"downloads_dir": "%s"}' % chosen)
        with pytest.raises(SystemExit, match="Downloads"):
            preview.prepare(data)
