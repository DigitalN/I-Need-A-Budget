# SPDX-License-Identifier: GPL-3.0-only
"""The Windows app (issue #187): its installer and build, and what the app does on Windows
besides quitting (test_backups.py) and computer_windows' own (test_computer.py). Throwaway
folders only."""

import importlib.util
import re
import stat
from pathlib import Path

import computer_windows

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
SPEC = (ROOT / "Application Files" / "INeedABudget.spec").read_text(encoding="utf-8")


def setup(name):
    """A [Setup] directive of the installer."""
    found = re.search(rf"^{name}=(.*)$", INSTALLER, re.MULTILINE)
    assert found, f"The installer has no {name}"
    return found.group(1).strip()


def test_the_installer_installs_for_this_person_only_into_their_programs_folder():
    assert setup("PrivilegesRequired") == "lowest"  # no administrator
    assert setup("DefaultDirName") == r"{autopf}\{#Name}"  # without administrator: %LOCALAPPDATA%\Programs
    assert setup("DisableDirPage") == "yes"  # always there, where an update finds it
    assert not re.search(r"^PrivilegesRequiredOverridesAllowed=", INSTALLER, re.MULTILINE)
    assert re.search(r'^Name: "\{autoprograms\}\\\{#Name\}"; Filename: "\{app\}\\\{#Exe\}"', INSTALLER, re.MULTILINE)


def test_the_installer_is_the_one_an_update_looks_for():
    """Windows updates (#188) pick I-Need-A-Budget-<version>-Setup.exe from a release and run it quietly
    over the copy that's installed: the same AppId, always."""
    assert setup("OutputBaseFilename") == "I-Need-A-Budget-{#Version}-Setup"
    assert setup("VersionInfoVersion") == "{#Version}"  # its ProductVersion, which the updater reads
    assert setup("AppId") == "{{BA0375DB-A1F9-4F91-A81B-A0B01ABF18B6}"  # never change it
    assert "skipifsilent" in re.search(r"^\[Run\]\n(.*)$", INSTALLER, re.MULTILINE).group(1)  # the update reopens it


def test_the_installer_waits_for_the_app_to_be_closed():
    assert setup("AppMutex") == "{#Name} is running"
    assert computer_windows.RUNNING_MUTEX == "I Need A Budget is running"
    assert setup("CloseApplications") == "no"  # never closed under the person: the app quits itself, backing up


def test_the_installer_and_uninstaller_leave_the_persons_data():
    """Only the app's own folder: never this computer's data folder, the budget folder or the backups."""
    removed = re.findall(r'^Type: \w+; Name: "([^"]+)"', INSTALLER, re.MULTILINE)
    assert removed == [r"{app}\_internal"]  # the old version's files, before the new ones go in
    assert "[UninstallDelete]" not in INSTALLER and "[Registry]" not in INSTALLER
    destinations = set(re.findall(r'DestDir: "([^"]+)"', INSTALLER))
    assert destinations == {"{app}"}


def test_the_installer_checks_for_webview2():
    assert "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in INSTALLER  # Microsoft's WebView2 Runtime
    assert "function InitializeSetup: Boolean;" in INSTALLER


def test_the_build_has_each_systems_side_only():
    (mac, not_mac), (windows, not_windows) = re.findall(r"SYSTEMS = \[(.*?)\]\n\s*OTHERS = \[(.*?)\]", SPEC, re.DOTALL)
    for side, other in (("computer_mac", "computer_windows"), ("webview.platforms.cocoa", "webview.platforms.winforms")):
        assert f"'{side}'" in mac and f"'{side}'" in not_windows
        assert f"'{other}'" in windows and f"'{other}'" in not_mac
    assert "INeedABudget.ico" in SPEC and (ROOT / "Application Files" / "INeedABudget.ico").exists()
    assert "© 2026 DigitalN. Free software under the GNU GPL v3." in SPEC


def test_the_license_opens_a_second_time(tmp_path, monkeypatch):
    """The copy is read-only, and Windows refuses to delete a read-only file: the second click of
    License in Settings must still open it."""
    import tempfile

    monkeypatch.setenv("INAB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    spec = importlib.util.spec_from_file_location("inab_main_for_license", ROOT / "Application Files" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)

    opened = []

    class Computer:
        def on_main_thread(self, fn, *args):
            fn(*args)

        def open_path(self, path):
            opened.append(path)

    app = main.BudgetApp.__new__(main.BudgetApp)
    app._computer = Computer()
    assert app._open_license() and app._open_license()
    copy = tmp_path / "temp" / "I Need A Budget" / "LICENSE.txt"
    assert opened == [copy, copy]
    assert not copy.stat().st_mode & stat.S_IWRITE  # read-only: nothing saves into it
    assert copy.read_text(encoding="utf-8") == (ROOT / "LICENSE").read_text(encoding="utf-8")
