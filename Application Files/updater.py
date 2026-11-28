# SPDX-License-Identifier: GPL-3.0-only
"""Keeps the app up to date from its GitHub releases.

GitHub only shows a private repository's releases to someone with access, so the updater
uses a token when it finds one, in order:
1. a token saved from Settings (on a Mac, in the Keychain: computer_mac.py; on Windows, in
   Credential Manager): a fine-grained token with read access to this repository's contents
   is enough. This is how a second computer (or a second person) gets updates;
2. the GitHub CLI's login on this computer (`gh auth token`).
Without one it asks GitHub anonymously, which works once the repository is public. When
GitHub won't show the releases to this copy, `needs_access` is set and Settings offers
the token field. The token goes to curl on stdin (`--config -`), never on the command
line, where other processes could see it.

When: once, a few seconds after each launch (unless "Check for updates at launch" is off
in Settings), and when you press Check now. Never in between: the maintainer's choice.

What: the repository's latest release (not drafts or pre-releases), tagged `v<version>`,
with this computer's file attached: a Mac's .dmg from scripts/make-dmg.py, Windows'
`I-Need-A-Budget-<version>-Setup.exe` with its signature beside it (`….exe.sig`). It's
downloaded, and this computer checks it's this app, intact, from the same developer (on a
Mac, `codesign --verify`: signed by the same team as this copy, for the same bundle
identifier; on Windows, our own release key's signature: windows_update.py) and newer. Then
it waits: it's installed when you quit (the window's close button or ⌘Q), or straight away
with "Restart now". How each of those steps is done is computer.py's. Windows' installer runs
once the app has gone, so it's never started while Windows is logging off or shutting down:
the checked download is kept instead, checked again at the next launch and installed then,
before the window opens.

This and nothing else talks to the network. It goes through the system's curl, which uses
the system's certificates (the bundled Python has none of its own).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date
from pathlib import Path

log = logging.getLogger("inab")

REPOSITORY = "DigitalN/I-Need-A-Budget"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
IDLE, CHECKING, DOWNLOADING, READY = "idle", "checking", "downloading", "ready"
FEED_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
REPOSITORY_URL = f"https://api.github.com/repos/{REPOSITORY}"
LAUNCH_CHECK_DELAY = 5   # seconds after launch, so the window comes up first
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_]{20,255}$")


class UpdateError(Exception):
    pass


class InstallError(OSError):
    """swap_in couldn't put the update in, and its message says what failed (Settings shows it)."""


class LoggingOff(InstallError):
    """swap_in found the computer logging off or shutting down: the update waits for the next launch."""


# ── pure helpers (unit-tested) ──────────────────────────────

def version_key(version):
    """(1, 10, 0) for '1.10' or 'v1.10.0', so 1.10 sorts after 1.9 and 1.1 equals 1.1.0."""
    parts = [int(m.group()) if (m := re.match(r"\d+", p)) else 0 for p in str(version).lstrip("v").split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(version, other):
    return version_key(version) > version_key(other)


def parse_release(data, update_file, signature=None):
    """(version, URL to download this computer's file, URL of its signature or None) from
    GitHub's latest-release response. `update_file` is the computer's (suffix, what it's
    called): (".dmg", "disk image") on a Mac, ("-setup.exe", "installer") on Windows.
    `signature` is what's added to the file's name to name its signature (".sig" on Windows;
    None on a Mac, whose app is signed inside): a release without one is refused before
    anything is downloaded. The API URL of each asset, which works for a private repository
    with a token."""
    suffix, kind = update_file
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
        raise UpdateError("GitHub's answer didn't describe a release.")
    tag = data["tag_name"]
    version = tag[1:] if tag.startswith("v") else tag
    assets = data.get("assets") or []
    for asset in assets:
        name = str(asset.get("name", ""))
        if name.lower().endswith(suffix):
            url = asset.get("url") or asset.get("browser_download_url")
            if url:
                if not signature:
                    return version, url, None
                for other in assets:
                    signed = other.get("url") or other.get("browser_download_url")
                    if str(other.get("name", "")).lower() == (name + signature).lower() and signed:
                        return version, url, signed
                raise UpdateError(f"The latest release's {kind} isn't signed.")
    raise UpdateError(f"The latest release has no {kind} attached.")


def curl_config(token):
    """curl options read from stdin, so the token isn't in the process list."""
    if not token:
        return ""
    return f'header = "Authorization: Bearer {token}"\nheader = "X-GitHub-Api-Version: 2022-11-28"\n'


def valid_token(token):
    return bool(TOKEN_PATTERN.match(str(token or "")))


# ── the token ───────────────────────────────────────────────

def _run(arguments, no_window, **kwargs):
    """A command-line tool, without the console window Windows would flash up for one started
    from a windowed app (`no_window`: the computer's)."""
    return subprocess.run(arguments, capture_output=True, **no_window, **kwargs)


def saved_token(computer):
    token = computer.saved_token()
    return token if valid_token(token) else None


def gh_token(computer):
    for path in computer.gh:
        if Path(path).exists():
            result = _run([path, "auth", "token"], computer.no_window, text=True)
            token = result.stdout.strip()
            if result.returncode == 0 and valid_token(token):
                return token
    return None


def find_token(computer):
    """(token, where it came from) or (None, None)."""
    token = saved_token(computer)
    if token:
        return token, "the token saved in Settings"
    token = gh_token(computer)
    if token:
        return token, "your GitHub CLI sign-in"
    return None, None


# ── command line tools ──────────────────────────────────────

def _curl(url, output, max_time, token=None, accept=None, *, curl, no_window):
    """Fetches `url` into `output` ('-' for stdout) with the `curl` given (the computer's, and
    its `no_window`). Returns (HTTP status, body); 0 for file://."""
    arguments = [curl, "--silent", "--show-error", "--location", "--max-time", str(max_time),
                 "--output", str(output), "--write-out", "\n%{http_code}", "--config", "-"]
    if accept:
        arguments += ["--header", f"Accept: {accept}"]
    result = _run([*arguments, url], no_window, input=curl_config(token), text=True)
    if result.returncode != 0:
        raise UpdateError(f"Couldn't reach GitHub ({result.stderr.strip() or result.returncode}).")
    body, _, code = result.stdout.rpartition("\n")
    return int(code or 0), body


class Updater:
    """Checks and downloads on a worker thread; installs on the main thread at quit."""

    def __init__(self, app, version, settings, data_dir, on_status, computer):
        self.status = IDLE
        self.version = None          # the version being downloaded or ready to install
        self.last_error = None
        self._app = app
        self._computer = computer
        self._current = (computer.installed_version(app) if app else None) or version
        self._settings = settings
        self._last_check_file = Path(data_dir) / "last-update-check"
        self._kept = Path(data_dir) / "Update waiting"  # a checked download a log-off kept for the next launch
        self._on_status = on_status
        # For testing, INAB_UPDATE_FEED can point somewhere else (a file:// URL works);
        # the signature check still applies to whatever it finds there.
        self._feed = os.environ.get("INAB_UPDATE_FEED") or FEED_URL
        self._staged = None          # (version, update, folder) once downloaded and verified
        self._lock = threading.Lock()
        self.needs_access = False    # GitHub wouldn't show this copy the releases: ask for a token
        self.unavailable_reason = self._why_unavailable()

    def _why_unavailable(self):
        """Never stops the app launching: anything that goes wrong finding out turns updates off."""
        if self._app is None:
            return "running from source"
        try:
            return self._computer.why_updates_off(self._app)
        except Exception as e:
            log.warning("Couldn't tell whether this copy can update itself", exc_info=True)
            return f"couldn't tell whether this copy can update itself ({e})"

    def state(self):
        token, source = (None, None) if self._feed.startswith("file:") else find_token(self._computer)
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
                    token, _ = find_token(self._computer)  # None: ask anonymously (a public repository)
                release = self._latest_release(token)
                self._last_check_file.write_text(date.today().isoformat(), encoding="utf-8")
                if release is None or not is_newer(release[0], self._current):
                    log.info("Up to date at %s", self._current)
                    self._set_status(IDLE)
                    return
                version, url, signature_url = release
                log.info("Downloading %s", version)
                self._set_status(DOWNLOADING, version)
                self._staged = self._download(url, token, signature_url)
                version = self._staged[0]  # as the checked download says, not the release's tag
                log.info("%s is ready to install", version)
                self._set_status(READY, version)
            except Exception as e:
                self.last_error = str(e)
                log.error("Update check failed: %s", e)
                self._set_status(IDLE)

    def _latest_release(self, token):
        code, body = _curl(self._feed, "-", max_time=30, token=token, accept="application/vnd.github+json",
                           curl=self._computer.curl, no_window=self._computer.no_window)
        if code == 404:
            # GitHub says the same when there are no releases and when this access can't see
            # the (private) repository at all. Only the first is "up to date".
            if not self._feed.startswith("file:"):
                repo_code, _ = _curl(REPOSITORY_URL, "-", max_time=30, token=token,
                                     accept="application/vnd.github+json", curl=self._computer.curl,
                                     no_window=self._computer.no_window)
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
            return parse_release(json.loads(body), self._computer.update_file, self._computer.update_signature)
        except ValueError:
            raise UpdateError("GitHub's answer didn't describe a release.") from None

    def _download(self, url, token, signature_url=None):
        folder = self._computer.folder_for_update(self._app)
        try:
            download = folder / f"update{self._computer.update_file[0]}"
            # The asset's API URL redirects to storage; curl drops the token on the redirect.
            code, _ = _curl(url, download, max_time=600, token=token, accept="application/octet-stream",
                            curl=self._computer.curl, no_window=self._computer.no_window)
            if code not in (200, 0):
                raise UpdateError(f"GitHub answered with an error ({code}).")
            if signature_url and self._computer.update_signature:  # beside it, where take_update looks
                signature = download.with_name(download.name + self._computer.update_signature)
                code, _ = _curl(signature_url, signature, max_time=60, token=token,
                                accept="application/octet-stream", curl=self._computer.curl,
                                no_window=self._computer.no_window)
                if code not in (200, 0):
                    raise UpdateError(f"GitHub answered with an error ({code}).")
            version, update = self._computer.take_update(download, self._app, folder)
            if not is_newer(version, self._current):
                raise UpdateError(f"The latest release contains version {version}, which isn't newer than this one.")
            return version, update, folder
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def install(self, relaunch_after=False, logging_out=False):
        """Swaps the downloaded app in, or where an installer does that once the app has quit
        (Windows), starts it. Called when quitting, or by "Restart now". Returns True if it's
        in, or going in. While the computer is logging out or shutting down, only where the swap
        is one step (a Mac's): Windows could stop its installer half-way, so the download is kept
        for the next launch (take_kept)."""
        if self._staged is None:
            return False
        (version, update, folder), self._staged = self._staged, None
        if logging_out and self._computer.installs_after_quitting:
            return self._keep(version, folder)
        try:
            self._computer.swap_in(self._app, update)
            error = None
        except LoggingOff:
            return self._keep(version, folder)
        except OSError as e:
            error = e
        shutil.rmtree(folder, ignore_errors=True)
        if error is not None:
            log.error("Couldn't install %s: %s", version, error)
            self.last_error = str(error) if isinstance(error, InstallError) else f"Couldn't replace the app in {self._app.parent}."
            self._set_status(IDLE)
            return False
        if self._computer.installs_after_quitting:  # its installer says how it went, in the log, afterwards
            log.info("Installing %s after quitting%s", version, "; the app reopens then" if relaunch_after else "")
        else:
            log.info("Installed %s%s", version, "; relaunching" if relaunch_after else "")
        if relaunch_after:
            self._computer.relaunch(self._app)
        return True

    def _keep(self, version, folder):
        if Path(folder) != self._kept:  # it is when a called-off log-off took it back (log_off_called_off)
            shutil.rmtree(self._kept, ignore_errors=True)
            try:
                shutil.move(str(folder), str(self._kept))
            except OSError as e:
                shutil.rmtree(folder, ignore_errors=True)
                log.warning("Not installing %s while logging out, and couldn't keep it for the next launch: %s",
                            version, e)
                return False
        log.info("Not installing %s while logging out: it's checked again and installed at the next launch", version)
        return False

    def take_kept(self):
        """At launch, before the window opens: an update a log-off kept (install) is checked again,
        as a download is, and has to be newer than this copy. True when it passed: it's ready to
        install now, the way quitting installs it, and to reopen the app. One that doesn't pass
        is deleted."""
        version = self._take_back()
        if version is None:
            return False
        log.info("Installing %s, kept from logging out, before the window opens", version)
        return True

    def log_off_called_off(self):
        """The log-off that kept the update (install) didn't happen after all, and the app carries
        on (main._quit_called_off): it's checked again, as at the next launch, and waits to go in
        at the next quit or with Restart now, as before the log-off. Taken back from where it was
        kept, so the next launch doesn't install it a second time; never lost, as one kept that
        way stays kept (_keep). Gone when it no longer passes, and then nothing's waiting."""
        if self._staged is not None or not self._kept.is_dir():
            return
        version = self._take_back()
        if version is not None:
            log.info("The log-off was called off: %s goes in at the next quit again", version)
        elif self.status == READY:
            self._set_status(IDLE)

    def _take_back(self):
        """The kept update, checked again and staged as a download is: its version, or None (one
        that doesn't pass is deleted)."""
        if self.unavailable_reason or not self._kept.is_dir():
            return None
        try:
            download = self._kept / f"update{self._computer.update_file[0]}"
            version, update = self._computer.take_update(download, self._app, self._kept)
            if not is_newer(version, self._current):
                raise UpdateError(f"its version, {version}, isn't newer than this one")
        except Exception as e:
            log.warning("Not installing the update kept from logging out: %s", e)
            shutil.rmtree(self._kept, ignore_errors=True)
            return None
        self._staged = (version, update, self._kept)
        return version

    def save_token(self, token):
        """Settings' token: saved where this computer keeps it, or with "" removed."""
        if token and not valid_token(token):
            raise UpdateError("That doesn't look like a GitHub token.")
        self._computer.save_token(token)
        return True
