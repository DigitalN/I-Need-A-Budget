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


# ── the Windows app (issue #187) ──

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="asks Windows itself")


def test_a_windows_frame_is_kept_as_text_and_read_back():
    from computer_windows import frame_text, read_frame

    assert frame_text((192, 104, 2176, 1183), False) == "192 104 2176 1183"
    assert read_frame(frame_text((-1900, 20, 1400, 900), True)) == ((-1900, 20, 1400, 900), True)  # left of the main screen
    for nonsense in (None, "", "1 2 3", "1 2 3 4 5", "1 2 3 4 minimised", "a b c d", "0 0 0 900", "1 2 3 4 maximized x"):
        assert read_frame(nonsense) is None, nonsense


def test_a_windows_frame_is_kept_on_a_screen():
    from computer_windows import keep_on_a_screen

    main_screen, left_screen = (0, 0, 2560, 1392), (-1920, 0, 1920, 1032)  # each without its taskbar
    assert keep_on_a_screen((100, 80, 1600, 900), [main_screen, left_screen], main_screen) == (100, 80, 1600, 900)
    assert keep_on_a_screen((-1800, 50, 1600, 900), [main_screen, left_screen], main_screen) == (-1800, 50, 1600, 900)
    # The monitor on the left unplugged: back onto the main screen.
    assert keep_on_a_screen((-1800, 50, 1600, 900), [main_screen], main_screen) == (0, 50, 1600, 900)
    # Mostly on the left screen, too tall for it: moved and shrunk onto it.
    assert keep_on_a_screen((-1700, 100, 1600, 1200), [main_screen, left_screen], main_screen) == (-1700, 0, 1600, 1032)
    # Under the taskbar, or bigger than a laptop's screen: moved up, and shrunk to fit.
    laptop = (0, 0, 1536, 816)
    assert keep_on_a_screen((200, 300, 2176, 1183), [laptop], laptop) == (0, 0, 1536, 816)
    assert keep_on_a_screen((100, 400, 1000, 660), [laptop], laptop) == (100, 156, 1000, 660)


def test_a_windows_first_window_is_most_of_the_screen_in_the_middle():
    from computer_windows import centred, first_size

    assert first_size((1000, 660), 0.85, (2560, 1392), 96) == (2176, 1183)
    assert first_size((1000, 660), 0.85, (3840, 2088), 144) == (2176, 1183)  # 150%: pywebview scales it back up
    assert first_size((1000, 660), 0.85, (1024, 728), 96) == (1000, 660)     # never below the smallest size
    assert first_size((1000, 660), 0.85, (800, 560), 96) == (800, 560)       # unless the screen is smaller
    assert centred(2176, 1183, (0, 0, 2560, 1392)) == (192, 104, 2176, 1183)
    assert centred(1000, 660, (-1920, 40, 1920, 1032)) == (-1460, 226, 1000, 660)


def test_windows_opens_only_web_pages_in_the_browser():
    with pytest.raises(ValueError):
        Windows().open_web_page("file:///C:/Windows/System32/calc.exe")


def test_windows_knows_where_the_installed_app_is(monkeypatch, tmp_path):
    windows = Windows()
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert windows.installed_app() is None  # from source
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Programs" / "I Need A Budget" / "I Need A Budget.exe"))
    assert windows.installed_app() == (tmp_path / "Programs" / "I Need A Budget").resolve()
    assert windows.moved_to_install_folder(windows.installed_app()) is False  # the installer put it there


@pytest.fixture
def windows_for_tests(monkeypatch):
    """A Windows whose settings are its own, not the app's: removed afterwards."""
    import winreg

    windows = Windows()
    monkeypatch.setattr(windows, "_settings_key", r"Software\I Need A Budget tests")
    yield windows
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, windows._settings_key)
    except OSError:
        pass


@WINDOWS
def test_windows_keeps_the_window_in_this_computers_settings(windows_for_tests):
    assert windows_for_tests._setting("Main window") is None
    windows_for_tests._set_setting("Main window", "192 104 2176 1183 maximized")
    assert windows_for_tests._setting("Main window") == "192 104 2176 1183 maximized"


@WINDOWS
def test_the_copy_holding_the_lock_tells_the_installer_its_running(tmp_path):
    """The installer's AppMutex: it waits for the app to be closed rather than replace it while it runs."""
    import ctypes

    from computer_windows import RUNNING_MUTEX

    windows = Windows()
    held = windows.lock(tmp_path / ".lock")
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    found = kernel32.OpenMutexW(0x00100000, False, RUNNING_MUTEX)  # SYNCHRONIZE: what Inno Setup asks for
    assert found
    kernel32.CloseHandle(ctypes.c_void_p(found))
    held.close()

