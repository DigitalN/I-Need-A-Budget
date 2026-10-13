"""Keeps the app up to date from its GitHub releases.

GitHub only shows a private repository's releases to someone with access, so the updater
uses a token when it finds one, in order:
1. a token saved in the Keychain from Settings (service "I Need A Budget", account
   "GitHub"): a fine-grained token with read access to this repository's contents is
   enough. This is how a second Mac (or a second person) gets updates;
2. the GitHub CLI's login on this Mac (`gh auth token`).
Without one it asks GitHub anonymously, which works once the repository is public. When
GitHub won't show the releases to this copy, `needs_access` is set and Settings offers
the token field. The token goes to curl on stdin (`--config -`), never on the command
line, where other processes could see it.

When: once, a few seconds after each launch (unless "Check for updates at launch" is off
in Settings), and when you press Check now. Never in between: the maintainer's choice.

What: the repository's latest release (not drafts or pre-releases), tagged `v<version>`,
with the .dmg from scripts/package.sh attached. It's downloaded, the app is copied out,
and `codesign --verify` must show it's signed by the same team as this copy, for the
same bundle identifier, and newer. Then it waits: it's installed when you quit (the
window's close button or ⌘Q), or straight away with "Restart now".

This and nothing else talks to the network. It goes through /usr/bin/curl, which uses
the system's certificates (the bundled Python has none of its own).
"""

import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

from launch import APP_NAME, REPOSITORY, in_applications_folder, relaunch

log = logging.getLogger("inab")

IDLE, CHECKING, DOWNLOADING, READY = "idle", "checking", "downloading", "ready"
FEED_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
REPOSITORY_URL = f"https://api.github.com/repos/{REPOSITORY}"
LAUNCH_CHECK_DELAY = 5   # seconds after launch, so the window comes up first
KEYCHAIN_SERVICE = APP_NAME
KEYCHAIN_ACCOUNT = "GitHub"
GH_PATHS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_]{20,255}$")


class UpdateError(Exception):
    pass


# ── pure helpers (unit-tested) ──────────────────────────────

def version_key(version):
    """(1, 10, 0) for '1.10' or 'v1.10.0', so 1.10 sorts after 1.9 and 1.1 equals 1.1.0."""
    parts = [int(m.group()) if (m := re.match(r"\d+", p)) else 0 for p in str(version).lstrip("v").split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(version, other):
    return version_key(version) > version_key(other)


def parse_release(data):
    """(version, URL to download the disk image) from GitHub's latest-release response.
    The API URL of the asset, which works for a private repository with a token."""
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
        raise UpdateError("GitHub's answer didn't describe a release.")
    tag = data["tag_name"]
    version = tag[1:] if tag.startswith("v") else tag
    for asset in data.get("assets") or []:
        if str(asset.get("name", "")).lower().endswith(".dmg"):
            url = asset.get("url") or asset.get("browser_download_url")
            if url:
                return version, url
    raise UpdateError("The latest release has no disk image attached.")


def team_identifier(codesign_details):
    """The team from `codesign -d --verbose=2` output, or None if it isn't team-signed."""
    match = re.search(r"^TeamIdentifier=(\S+)$", codesign_details, re.MULTILINE)
    return match.group(1) if match and match.group(1) != "not" else None


def signing_requirement(bundle_id, team):
    return (f'=identifier "{bundle_id}" and anchor apple generic'
            f' and certificate leaf[subject.OU] = "{team}"')


def curl_config(token):
    """curl options read from stdin, so the token isn't in the process list."""
    if not token:
        return ""
    return f'header = "Authorization: Bearer {token}"\nheader = "X-GitHub-Api-Version: 2022-11-28"\n'


def valid_token(token):
    return bool(TOKEN_PATTERN.match(str(token or "")))


# ── the token ───────────────────────────────────────────────

def _run(arguments, **kwargs):
    return subprocess.run(arguments, capture_output=True, **kwargs)


def keychain_token():
    result = _run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                  text=True)
    token = result.stdout.strip() if result.returncode == 0 else ""
    return token if valid_token(token) else None


def save_keychain_token(token):
    """Saves (or with an empty token, removes) the token. Sent to `security` on stdin."""
    if not token:
        _run(["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT])
        return True
    if not valid_token(token):
        raise UpdateError("That doesn't look like a GitHub token.")
    command = f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{KEYCHAIN_ACCOUNT}" -w "{token}"\n'
    result = _run(["/usr/bin/security", "-i"], input=command, text=True)
    if result.returncode != 0 or keychain_token() != token:
        raise UpdateError("Couldn't save the token in the Keychain.")
    return True


def gh_token():
    for path in GH_PATHS:
        if Path(path).exists():
            result = _run([path, "auth", "token"], text=True)
            token = result.stdout.strip()
            if result.returncode == 0 and valid_token(token):
                return token
    return None


def find_token():
    """(token, where it came from) or (None, None)."""
    token = keychain_token()
    if token:
        return token, "the token saved in Settings"
    token = gh_token()
    if token:
        return token, "your GitHub CLI sign-in"
    return None, None


# ── command line tools ──────────────────────────────────────

def _curl(url, output, max_time, token=None, accept=None):
    """Fetches `url` into `output` ('-' for stdout). Returns (HTTP status, body); 0 for file://."""
    arguments = ["/usr/bin/curl", "--silent", "--show-error", "--location", "--max-time", str(max_time),
                 "--output", str(output), "--write-out", "\n%{http_code}", "--config", "-"]
    if accept:
        arguments += ["--header", f"Accept: {accept}"]
    result = _run([*arguments, url], input=curl_config(token), text=True)
    if result.returncode != 0:
        raise UpdateError(f"Couldn't reach GitHub ({result.stderr.strip() or result.returncode}).")
    body, _, code = result.stdout.rpartition("\n")
    return int(code or 0), body


def own_team(app):
    result = _run(["/usr/bin/codesign", "-d", "--verbose=2", str(app)], text=True)
    return team_identifier(result.stderr)


def info_value(app, key):
    """Reads Info.plist directly: NSBundle caches by path, which would go stale here."""
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get(key)
    except (OSError, plistlib.InvalidFileException):
        return None


def _attach(image, attempts=3):
    """Mounts the disk image in /Volumes without showing it in Finder; returns where.
    A first attach of a freshly written image can fail with "Resource temporarily
    unavailable" and leave it half attached, so detach whatever is left and retry."""
    for attempt in range(attempts):
        result = _run(["/usr/bin/hdiutil", "attach", str(image), "-nobrowse", "-readonly", "-noautoopen", "-plist"])
        if result.returncode == 0:
            entities = plistlib.loads(result.stdout).get("system-entities", [])
            mount = next((e["mount-point"] for e in entities if e.get("mount-point")), None)
            if mount:
                return mount
        _detach_image(image)
        if attempt < attempts - 1:
            time.sleep(3)
    raise UpdateError(f"hdiutil couldn't open the disk image ({result.returncode}).")


def _detach_image(image):
    try:
        images = plistlib.loads(_run(["/usr/bin/hdiutil", "info", "-plist"]).stdout).get("images", [])
    except (plistlib.InvalidFileException, ValueError):
        return
    for entry in images:
        if Path(entry.get("image-path", "")).resolve() != Path(image).resolve():
            continue
        devices = [e["dev-entry"] for e in entry.get("system-entities", []) if e.get("dev-entry")]
        if devices:
            _run(["/usr/bin/hdiutil", "detach", min(devices, key=len), "-force"])


def copy_app_from_image(image, destination, bundle_id):
    mount = _attach(image)
    try:
        apps = [p for p in Path(mount).iterdir() if p.suffix == ".app" and not p.is_symlink()]
        app = next((p for p in apps if info_value(p, "CFBundleIdentifier") == bundle_id), None)
        if app is None:
            raise UpdateError(f"The latest release's disk image doesn't contain {APP_NAME}.")
        copied = _run(["/usr/bin/ditto", str(app), str(destination)])
        if copied.returncode != 0:
            raise UpdateError(f"ditto failed ({copied.returncode}).")
    finally:
        _run(["/usr/bin/hdiutil", "detach", mount, "-force"])


def verify(app, team, bundle_id):
    """Checks the whole app is intact and signed by this team for this bundle identifier;
    returns its version."""
    result = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                   "-R", signing_requirement(bundle_id, team), str(app)], text=True)
    if result.returncode != 0:
        log.warning("Rejected update: %s", result.stderr.strip())
        raise UpdateError("The downloaded app was changed or isn't signed by the same developer.")
    version = info_value(app, "CFBundleShortVersionString")
    if not version:
        raise UpdateError(f"The latest release's disk image doesn't contain {APP_NAME}.")
    return version


class Updater:
    """Checks and downloads on a worker thread; installs on the main thread at quit."""

    def __init__(self, app, version, settings, data_dir, on_status):
        self.status = IDLE
        self.version = None          # the version being downloaded or ready to install
        self.last_error = None
        self._app = app
        self._current = (info_value(app, "CFBundleShortVersionString") if app else None) or version
        self._bundle_id = info_value(app, "CFBundleIdentifier") if app else None
        self._settings = settings
        self._last_check_file = Path(data_dir) / "last-update-check"
        self._on_status = on_status
        # For testing, INAB_UPDATE_FEED can point somewhere else (a file:// URL works);
        # the signature check still applies to whatever it finds there.
        self._feed = os.environ.get("INAB_UPDATE_FEED") or FEED_URL
        self._team = None
        self._staged = None          # (version, app, folder) once downloaded and verified
        self._lock = threading.Lock()
        self.needs_access = False    # GitHub wouldn't show this copy the releases: ask for a token
        self.unavailable_reason = self._why_unavailable()

    def _why_unavailable(self):
        if self._app is None:
            return "running from source"
        self._team = own_team(self._app)
        if self._team is None:
            return "this copy isn't signed by a developer team, so updates can't be verified"
        if not in_applications_folder(self._app):
            return f"{APP_NAME} isn't in the Applications folder"
        if not (os.access(self._app, os.W_OK) and os.access(self._app.parent, os.W_OK)):
            return f"{APP_NAME} can't replace itself in {self._app.parent}"
        return None

    def state(self):
        token, source = (None, None) if self._feed.startswith("file:") else find_token()
        try:
            checked = self._last_check_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            checked = None
        return {"status": self.status, "version": self.version, "current": self._current,
                "unavailable": self.unavailable_reason, "error": self.last_error, "checked": checked,
                "access": source, "needs_access": self.needs_access}

    def start(self):
        if self.unavailable_reason:
            log.info("Automatic updates off: %s", self.unavailable_reason)
            return
        threading.Thread(target=self._check_at_launch, name="update-at-launch", daemon=True).start()

    def _check_at_launch(self):
        """The one automatic check: shortly after launch, if it's switched on."""
        time.sleep(LAUNCH_CHECK_DELAY)
        try:
            if self._settings.get()["update_automatically"] and self.status == IDLE:
                self._check()
        except Exception:
            log.exception("Update check at launch failed")

    def check_now(self):
        """Settings → Check for Updates. Runs in the background; status comes via on_status."""
        if self.unavailable_reason:
            raise UpdateError(f"Automatic updates are off: {self.unavailable_reason}.")
        if self.status == IDLE:
            threading.Thread(target=self._check, kwargs={"user_initiated": True}, name="update-check",
                             daemon=True).start()
        return self.state()

    def _set_status(self, status, version=None):
        self.status, self.version = status, version
        try:
            self._on_status(self.state())
        except Exception:
            log.exception("Update status listener failed")

    def _check(self, user_initiated=False):
        with self._lock:
            self.last_error = None
            self._set_status(CHECKING)
            try:
                token = None
                if not self._feed.startswith("file:"):
                    token, _ = find_token()  # None: ask anonymously (a public repository)
                release = self._latest_release(token)
                self._last_check_file.write_text(date.today().isoformat(), encoding="utf-8")
                if release is None or not is_newer(release[0], self._current):
                    log.info("Up to date at %s", self._current)
                    self._set_status(IDLE)
                    return
                version, url = release
                log.info("Downloading %s", version)
                self._set_status(DOWNLOADING, version)
                self._staged = self._download(url, token)
                log.info("%s is ready to install", version)
                self._set_status(READY, version)
            except Exception as e:
                self.last_error = str(e)
                log.error("Update check failed: %s", e)
                self._set_status(IDLE)

    def _latest_release(self, token):
        code, body = _curl(self._feed, "-", max_time=30, token=token, accept="application/vnd.github+json")
        if code == 404:
            # GitHub says the same when there are no releases and when this access can't see
            # the (private) repository at all. Only the first is "up to date".
            if not self._feed.startswith("file:"):
                repo_code, _ = _curl(REPOSITORY_URL, "-", max_time=30, token=token, accept="application/vnd.github+json")
                if repo_code in (401, 403, 404):
                    self.needs_access = True
                    raise UpdateError("This GitHub access can't see the app's repository. Check the token in Settings."
                                      if token else "The app's repository is private: add a GitHub token in Settings.")
            self.needs_access = False
            return None  # no releases yet
        if code in (401, 403) and token:
            self.needs_access = True
            raise UpdateError("GitHub refused the token. Check it in Settings.")
        if code in (401, 403):
            raise UpdateError("GitHub is turning away checks for now. Try again later.")  # its anonymous limit
        if code not in (200, 0):
            raise UpdateError(f"GitHub answered with an error ({code}).")
        self.needs_access = False
        try:
            return parse_release(json.loads(body))
        except ValueError:
            raise UpdateError("GitHub's answer didn't describe a release.") from None

    def _download(self, url, token):
        from Foundation import NSURL, NSFileManager, NSItemReplacementDirectory, NSUserDomainMask

        # A temporary folder on the same volume as the installed app, so the swap is a rename.
        folder_url, error = NSFileManager.defaultManager().URLForDirectory_inDomain_appropriateForURL_create_error_(
            NSItemReplacementDirectory, NSUserDomainMask, NSURL.fileURLWithPath_(str(self._app)), True, None)
        if folder_url is None:
            raise UpdateError(f"Couldn't make a folder for the download ({error}).")
        folder = Path(folder_url.path())
        try:
            image = folder / "update.dmg"
            # The asset's API URL redirects to storage; curl drops the token on the redirect.
            code, _ = _curl(url, image, max_time=600, token=token, accept="application/octet-stream")
            if code not in (200, 0):
                raise UpdateError(f"GitHub answered with an error ({code}).")
            app = folder / self._app.name
            copy_app_from_image(image, app, self._bundle_id)
            image.unlink(missing_ok=True)
            version = verify(app, self._team, self._bundle_id)
            if not is_newer(version, self._current):
                raise UpdateError(f"The latest release contains version {version}, which isn't newer than this one.")
            return version, app, folder
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def install(self, relaunch_after=False):
        """Swaps the downloaded app in. Called when quitting, or by "Restart now".
        Returns True if it installed."""
        from Foundation import NSURL, NSFileManager, NSFileManagerItemReplacementUsingNewMetadataOnly

        if self._staged is None:
            return False
        (version, app, folder), self._staged = self._staged, None
        # Swaps the two folders in one step, so there's never a moment without an app.
        ok, _, error = NSFileManager.defaultManager().replaceItemAtURL_withItemAtURL_backupItemName_options_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(self._app)), NSURL.fileURLWithPath_(str(app)), None,
            NSFileManagerItemReplacementUsingNewMetadataOnly, None, None)
        shutil.rmtree(folder, ignore_errors=True)
        if not ok:
            log.error("Couldn't install %s: %s", version, error)
            self.last_error = f"Couldn't replace the app in {self._app.parent}."
            self._set_status(IDLE)
            return False
        log.info("Installed %s%s", version, "; relaunching" if relaunch_after else "")
        if relaunch_after:
            relaunch(self._app)
        return True
