"""The updater's decisions: versions, GitHub's release format, signing, the token, when it checks."""

import pytest

from launch import in_applications_folder
from updater import (UpdateError, curl_config, is_newer, parse_release, signing_requirement,
                     team_identifier, valid_token)


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
    assert parse_release(release) == ("1.2.0", "https://api.github.com/x/assets/2")


def test_release_without_disk_image_or_tag_is_refused():
    with pytest.raises(UpdateError, match="no disk image"):
        parse_release({"tag_name": "v1.2.0", "assets": [{"name": "app.zip", "url": "x"}]})
    with pytest.raises(UpdateError):
        parse_release({"message": "Not Found"})


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
    u._feed = updater.FEED_URL
    answers[updater.FEED_URL], answers[updater.REPOSITORY_URL] = 404, 200
    assert u._latest_release("token") is None
    answers[updater.REPOSITORY_URL] = 404
    with pytest.raises(UpdateError, match="can't see the app's repository"):
        u._latest_release("token")
