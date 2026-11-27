# SPDX-License-Identifier: GPL-3.0-only
"""What a Mac or a Windows PC does differently, behind one interface (computer.py, issue #182).
Throwaway folders only: nothing here writes to the real data, Documents or Downloads folders."""

import inspect
import os
import re
import sys
from pathlib import Path

import pytest

import computer
from computer_mac import Mac
from computer_windows import Windows

APP_FILES = Path(__file__).resolve().parents[2] / "Application Files"
MAC_ONLY = re.compile(r"^\s*(import|from)\s+(AppKit|Foundation|WebKit|Quartz|objc|PyObjCTools|fcntl)\b"
                      r"|webview\.platforms\.cocoa|/usr/bin/|/usr/libexec/|/bin/sh\b", re.M)
WINDOWS_ONLY = re.compile(r"^\s*(import|from)\s+(ctypes|msvcrt|winreg|winsound|_winapi)\b|windll", re.M)


def test_the_mac_and_windows_each_keep_to_their_own_file():
    """Issue #182: outside computer_mac.py, nothing uses AppKit, Foundation, objc, PyObjCTools or
    fcntl, or runs a command-line tool only a Mac has; outside computer_windows.py, nothing uses
    Windows' APIs."""
    for path in sorted(APP_FILES.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name != "computer_mac.py":
            assert not MAC_ONLY.search(text), f"{path.name}: {MAC_ONLY.search(text).group(0)!r} is the Mac's"
        if path.name != "computer_windows.py":
            assert not WINDOWS_ONLY.search(text), f"{path.name}: {WINDOWS_ONLY.search(text).group(0)!r} is Windows'"


def test_the_system_is_picked_in_one_place():
    for path in sorted(APP_FILES.glob("*.py")):
        if path.name != "computer.py":
            assert "sys.platform" not in path.read_text(encoding="utf-8"), path.name
    assert isinstance(computer.this(), {"darwin": Mac, "win32": Windows}[sys.platform])
    assert computer.this() is computer.this()


def test_each_system_does_only_what_the_interface_has():
    """Nothing is called on one system that the other doesn't have, even as NotImplementedError."""
    interface = {name for name, _ in inspect.getmembers(computer.Computer) if not name.startswith("_")}
    for system in (Mac, Windows):
        extra = {name for name in vars(system) if not name.startswith("_")} - interface
        assert not extra, f"{system.__name__} has {sorted(extra)}, which computer.Computer doesn't"
    assert {name for name in vars(Mac) if not name.startswith("_")} == interface  # the Mac does it all


def test_one_copy_at_a_time(tmp_path):
    this = computer.this()
    first = this.lock(tmp_path / ".lock")
    assert first is not None
    assert this.lock(tmp_path / ".lock") is None  # a second copy, while the first is open
    first.close()                                  # the first one quit
    again = this.lock(tmp_path / ".lock")
    assert again is not None
    again.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="AppKit is the Mac's")
def test_a_mac_quits_through_nsapp_on_its_main_thread(monkeypatch):
    """What test_backups' quit routes leave to the fake computer, on a real Mac."""
    import AppKit
    from PyObjCTools import AppHelper

    calls = []

    class NSApp:
        def terminate_(self, sender):
            calls.append("terminate")

    monkeypatch.setattr(AppKit, "NSApp", NSApp())
    monkeypatch.setattr(AppHelper, "callAfter", lambda fn, *args: calls.append("later") or fn(*args))
    mac = Mac()
    mac.on_main_thread(mac.quit)
    assert calls == ["later", "terminate"]


def test_a_macs_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mac = Mac()
    assert mac.data_dir() == tmp_path / "Library" / "Application Support" / "I Need A Budget"
    assert mac.log_dir() == tmp_path / "Library" / "Logs" / "I Need A Budget"
    assert mac.documents_dir() == tmp_path / "Documents"
    assert mac.downloads_dir() == tmp_path / "Downloads"
    assert list(tmp_path.iterdir()) == []  # working them out makes nothing


def test_windows_folders_are_where_windows_says(tmp_path, monkeypatch):
    """Never worked out from the home folder: OneDrive can move Documents, and `~/Documents` is then
    another, unsynced folder that backups would quietly go to."""
    import computer_windows

    said = {computer_windows.DOCUMENTS: tmp_path / "OneDrive" / "Documents",
            computer_windows.DOWNLOADS: tmp_path / "D" / "Downloads",
            computer_windows.LOCAL_APP_DATA: tmp_path / "AppData" / "Local"}
    monkeypatch.setattr(computer_windows, "known_folder", said.__getitem__)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    windows = Windows()
    assert windows.documents_dir() == tmp_path / "OneDrive" / "Documents"
    assert windows.downloads_dir() == tmp_path / "D" / "Downloads"
    assert windows.data_dir() == tmp_path / "AppData" / "Local" / "I Need A Budget"
    assert windows.log_dir() == windows.data_dir() / "Logs"
    assert list(tmp_path.iterdir()) == []  # working them out makes nothing


@pytest.mark.skipif(sys.platform != "win32", reason="asks Windows itself")
def test_windows_answers_for_each_folder():
    windows = Windows()
    for folder in (windows.documents_dir(), windows.downloads_dir()):
        assert folder.is_absolute() and folder.name
    assert windows.data_dir() == Path(os.environ["LOCALAPPDATA"]) / "I Need A Budget"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own folders")
def test_a_folder_windows_doesnt_know_is_an_error_not_a_guess():
    from computer_windows import known_folder
    import uuid

    with pytest.raises(OSError, match="didn't say where"):
        known_folder(uuid.UUID(int=1))  # no folder of Windows' has this ID


def test_settings_default_to_the_systems_downloads_folder(tmp_path, monkeypatch):
    from settings import Settings

    monkeypatch.setattr(computer.this(), "downloads_dir", lambda: tmp_path / "Downloads from the system")
    assert Settings(tmp_path / "settings.json").downloads_dir() == tmp_path / "Downloads from the system"
