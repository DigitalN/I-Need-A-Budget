# SPDX-License-Identifier: GPL-3.0-only
"""The updater's decisions: versions, GitHub's release format, signing, the token, when it checks,
and the steps it leaves to this computer (computer.py)."""

import sys

import pytest

from computer_mac import Mac, in_applications_folder, signing_requirement, team_identifier
from updater import UpdateError, curl_config, is_newer, parse_release, valid_token

DMG = Mac.update_file


def test_versions_compare_number_by_number():
    assert is_newer("1.10.0", "1.9.0")
    assert is_newer("1.1", "1.0.9")
    assert is_newer("v2.0", "1.99")
    assert not is_newer("1.1.0", "1.1")
    assert not is_newer("1.0.0", "1.1.0")


def test_private_release_uses_the_assets_api_url():
    release = {"tag_name": "v1.2.0", "assets": [
        {"name": "notes.txt", "url": "https://api.github.com/x/assets/1"},
        {"name": "I-Need-A-Budget-1.2.0.dmg", "url": "https://api.github.com/x/assets/2",
         "browser_download_url": "https://github.com/x/download/I-Need-A-Budget-1.2.0.dmg"},
    ]}
    assert parse_release(release, DMG) == ("1.2.0", "https://api.github.com/x/assets/2", None)


def test_release_without_disk_image_or_tag_is_refused():
    with pytest.raises(UpdateError, match="no disk image"):
        parse_release({"tag_name": "v1.2.0", "assets": [{"name": "app.zip", "url": "x"}]}, DMG)
    with pytest.raises(UpdateError):
        parse_release({"message": "Not Found"}, DMG)


def test_team_and_signing_requirement():
    assert team_identifier("Identifier=x\nTeamIdentifier=ABCDE12345\n") == "ABCDE12345"
    assert team_identifier("TeamIdentifier=not set\n") is None
    requirement = signing_requirement("com.digitaln.ineedabudget", "ABCDE12345")
    assert requirement.startswith('=identifier "com.digitaln.ineedabudget" and anchor apple generic')
    assert requirement.endswith('certificate leaf[subject.OU] = "ABCDE12345"')


def test_token_goes_in_curls_stdin_config():
    assert curl_config(None) == ""
    config = curl_config("ghp_" + "a" * 36)
    assert 'header = "Authorization: Bearer ghp_' in config


def test_only_token_shaped_strings_are_accepted():
    assert valid_token("github_pat_" + "A1_" * 20)
    assert not valid_token("")
    assert not valid_token('abc" ; rm -rf ~ "' + "a" * 30)


def test_checks_only_at_launch_and_when_asked(monkeypatch):
    """One check shortly after launch (if switched on) that then returns: no timer while open."""
    import updater

    class Settings:
        def __init__(self, on):
            self.on = on

        def get(self):
            return {"update_automatically": self.on}

    monkeypatch.setattr(updater, "LAUNCH_CHECK_DELAY", 0)
    checks = []
    for on in (True, False):
        u = updater.Updater.__new__(updater.Updater)
        u._settings, u.status = Settings(on), updater.IDLE
        u._check = lambda on=on: checks.append(on)
        u._check_at_launch()  # returns: nothing keeps checking afterwards
    assert checks == [True]


@pytest.mark.skipif(sys.platform != "darwin", reason="Applications is a Mac's folder, read as a Mac reads paths")
def test_applications_folder(tmp_path):
    assert in_applications_folder("/Applications/I Need A Budget.app")
    assert not in_applications_folder("/Applications Old/I Need A Budget.app")
    assert not in_applications_folder(tmp_path / "I Need A Budget.app")


def test_no_releases_is_up_to_date_but_no_access_is_an_error(tmp_path, monkeypatch):
    """GitHub answers 404 both when there's no release yet and when the token can't see the
    private repository; only the first may be reported as up to date."""
    import updater

    answers = {}
    monkeypatch.setattr(updater, "_curl", lambda url, *a, **k: (answers[url], "{}"))
    u = updater.Updater.__new__(updater.Updater)
    u._feed, u._computer = updater.FEED_URL, Mac()
    answers[updater.FEED_URL], answers[updater.REPOSITORY_URL] = 404, 200
    assert u._latest_release("token") is None
    answers[updater.REPOSITORY_URL] = 404
    with pytest.raises(UpdateError, match="can't see the app's repository"):
        u._latest_release("token")


def test_without_a_token_it_asks_anonymously_and_says_when_it_needs_one(monkeypatch):
    """A public repository needs no token; a private one tells Settings to ask for one."""
    import updater

    answers = {}
    sent = []

    def fake_curl(url, *a, token=None, **k):
        sent.append(token)
        return answers[url], '{"tag_name": "v9.0.0", "assets": [{"name": "I.dmg", "url": "https://x/1"}]}'

    monkeypatch.setattr(updater, "_curl", fake_curl)
    u = updater.Updater.__new__(updater.Updater)
    u._feed, u.needs_access, u._computer, u._current = updater.FEED_URL, False, Mac(), "1.4.3"
    answers[updater.FEED_URL] = 200
    assert u._latest_release(None) == ("9.0.0", "https://x/1", None) and sent == [None] and not u.needs_access
    answers[updater.FEED_URL], answers[updater.REPOSITORY_URL] = 404, 404
    with pytest.raises(UpdateError, match="private"):
        u._latest_release(None)
    assert u.needs_access
    answers[updater.FEED_URL] = 200
    u._latest_release("token")
    assert not u.needs_access


# ── the steps this computer does ──

class Computer:
    """A computer whose steps are noted, standing in for the Mac's (computer_mac.py)."""
    update_file, update_signature, installs_after_quitting, curl, gh, no_window = DMG, None, False, "curl", (), {}

    def __init__(self, folder, version="9.9"):
        self.folder, self.version, self.steps, self.token, self.refuse = folder, version, [], None, None

    def saved_token(self):
        return self.token

    def save_token(self, token):
        self.token = token or None

    def folder_for_update(self, app):
        self.folder.mkdir()
        return self.folder

    def take_update(self, download, app, folder):
        self.steps.append(("take", download.name))
        return self.version, folder / app.name

    def swap_in(self, app, update):
        if self.refuse:
            raise OSError(self.refuse)
        self.steps.append(("swap", update.name))

    def relaunch(self, app):
        self.steps.append(("relaunch", app.name))


def updater_on(computer, tmp_path, current="1.4.3"):
    import threading

    import updater

    u = updater.Updater.__new__(updater.Updater)
    u._computer, u._app, u._current, u._staged = computer, tmp_path / "I Need A Budget.app", current, None
    u._feed, u._last_check_file, u._on_status = "file:///feed.json", tmp_path / "last-update-check", lambda state: None
    u.status, u.version, u.last_error, u.unavailable_reason, u.needs_access = updater.IDLE, None, None, None, False
    u._lock = threading.Lock()
    return u


def test_a_release_that_isnt_newer_is_up_to_date_whatever_it_has_attached(tmp_path, monkeypatch, caplog):
    """1.4.3 has only the Mac's disk image: a Windows copy of 1.4.3 or later is up to date, not
    "The latest release has no installer attached." Only a newer release without this computer's
    file is worth an error. A Mac reads it the same way."""
    import json

    import updater
    from computer_windows import Windows

    class WindowsComputer(Computer):
        update_file, update_signature, installs_after_quitting = Windows.update_file, Windows.update_signature, True

    def check(computer, current, tag, *assets):
        release = {"tag_name": tag, "assets": [{"name": name, "url": f"https://x/{name}"} for name in assets]}
        monkeypatch.setattr(updater, "_curl", lambda *a, **k: (0, json.dumps(release)))
        u = updater_on(computer, tmp_path, current)
        caplog.clear()
        with caplog.at_level("INFO", logger="inab"):
            u._check()
        assert u.status == updater.IDLE
        return u.last_error, [(r.levelname, r.getMessage()) for r in caplog.records]

    mac_only = "I-Need-A-Budget-1.4.3.dmg"
    windows = WindowsComputer(tmp_path / "download")
    assert check(windows, "1.4.3", "v1.4.3", mac_only) == (None, [("INFO", "Up to date at 1.4.3")])
    assert check(windows, "1.5.0", "v1.4.3", mac_only) == (None, [("INFO", "Up to date at 1.5.0")])
    assert check(windows, "1.5.0", "v1.5.0") == (None, [("INFO", "Up to date at 1.5.0")])
    error = "The latest release has no installer attached."
    assert check(windows, "1.5.0", "v1.5.1", "I-Need-A-Budget-1.5.1.dmg") == (
        error, [("ERROR", f"Update check failed: {error}")])
    assert check(windows, "1.5.0", "v1.5.1", "I-Need-A-Budget-1.5.1-Setup.exe")[0] == "The latest release's installer isn't signed."
    assert not windows.steps  # nothing downloaded

    mac = Computer(tmp_path / "download")
    assert check(mac, "1.5.0", "v1.5.0", "I-Need-A-Budget-1.5.0-Setup.exe") == (None, [("INFO", "Up to date at 1.5.0")])
    assert check(mac, "1.5.0", "v1.5.1", "I-Need-A-Budget-1.5.1-Setup.exe")[0] == "The latest release has no disk image attached."
    assert not mac.steps


def test_an_update_is_downloaded_then_checked_and_swapped_in_by_the_computer(tmp_path, monkeypatch):
    import updater

    fetched = []
    monkeypatch.setattr(updater, "_curl", lambda url, output, *a, curl, **k: fetched.append((output.name, curl)) or (200, ""))
    computer = Computer(tmp_path / "download")
    u = updater_on(computer, tmp_path)
    u._staged = u._download("https://x/1", None)
    assert fetched == [("update.dmg", "curl")] and computer.steps == [("take", "update.dmg")]
    assert u.install(relaunch_after=True)
    assert computer.steps[1:] == [("swap", "I Need A Budget.app"), ("relaunch", "I Need A Budget.app")]
    assert not computer.folder.exists()  # nothing left of the download
    assert not u.install()               # and only once


def test_an_update_that_isnt_newer_or_cant_go_in_leaves_the_app_as_it_is(tmp_path, monkeypatch):
    import updater

    monkeypatch.setattr(updater, "_curl", lambda *a, **k: (200, ""))
    older = Computer(tmp_path / "older", version="1.4.3")
    with pytest.raises(UpdateError, match="isn't newer"):
        updater_on(older, tmp_path)._download("https://x/1", None)
    assert not older.folder.exists()

    refused = Computer(tmp_path / "refused")
    u = updater_on(refused, tmp_path)
    u._staged = u._download("https://x/1", None)
    refused.refuse = "Permission denied"
    assert not u.install(relaunch_after=True)
    assert u.last_error.startswith("Couldn't replace the app in") and u.status == updater.IDLE
    assert ("relaunch", "I Need A Budget.app") not in refused.steps and not refused.folder.exists()


def test_the_token_is_kept_where_the_computer_keeps_it(tmp_path):
    import updater

    computer = Computer(tmp_path / "unused")
    u = updater_on(computer, tmp_path)
    token = "github_pat_" + "A1_" * 20
    assert u.save_token(token) and updater.find_token(computer) == (token, "the token saved in Settings")
    with pytest.raises(UpdateError, match="doesn't look like a GitHub token"):
        u.save_token('abc" ; rm -rf ~ "' + "a" * 30)
    assert computer.token == token
    u.save_token("")
    assert computer.token is None
    computer.token = "not a token"  # whatever's saved, only a token's shape is sent
    assert updater.saved_token(computer) is None


# ── the Mac's own steps (computer_mac.py), with its tools stood in for ──

def test_a_mac_checks_every_update_against_the_app_as_it_was_at_launch(tmp_path, monkeypatch):
    """The team and bundle identifier are read once, at launch (why_updates_off): an unsigned build
    put in the app's place while it runs changes nothing about what an update must match."""
    import computer_mac

    app = tmp_path / "I Need A Budget.app"
    app.mkdir()
    signed, reads, verified = {"team": "ABCDE12345"}, [], []
    monkeypatch.setattr(computer_mac, "own_team", lambda a: reads.append("codesign") or signed["team"])
    monkeypatch.setattr(computer_mac, "info_value", lambda a, key: "com.example.sample")
    monkeypatch.setattr(computer_mac, "in_applications_folder", lambda a: True)
    monkeypatch.setattr(computer_mac, "copy_app_from_image", lambda image, destination, bundle_id: None)
    monkeypatch.setattr(computer_mac, "verify", lambda update, team, bundle_id: verified.append((team, bundle_id)) or "9.9")
    mac = Mac()
    assert mac.why_updates_off(app) is None
    signed["team"] = None  # an ad-hoc build dragged into Applications meanwhile
    download = tmp_path / "update.dmg"
    download.write_bytes(b"sample")
    assert mac.take_update(download, app, tmp_path) == ("9.9", tmp_path / app.name)
    assert verified == [("ABCDE12345", "com.example.sample")] and reads == ["codesign"]
    assert not download.exists()


def test_a_mac_puts_only_a_tokens_shape_on_securitys_command_line(monkeypatch):
    import computer_mac

    ran = []
    monkeypatch.setattr(computer_mac, "_run", lambda arguments, **k: ran.append(arguments))
    with pytest.raises(UpdateError, match="doesn't look like a GitHub token"):
        Mac().save_token('abc" ; rm -rf ~ "' + "a" * 30)
    assert ran == []
