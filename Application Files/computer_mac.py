# SPDX-License-Identifier: GPL-3.0-only
"""How a Mac does what computer.py's interface asks: AppKit, Foundation and the Mac's own
command-line tools, and nothing else outside this file uses them.

Where the app runs from: it has to run from an Applications folder to update itself. Opened
from a disk image or Downloads, macOS runs a temporary hidden copy ("app translocation") that
can't be replaced, so on launch from anywhere else it offers to move.

Updates: each release has a .dmg (scripts/package.sh). The app is copied out of it, and
`codesign --verify` must show it's signed by the same team as this copy, for the same bundle
identifier. The two are swapped in one step at quit. The token is in the Keychain (service
"I Need A Budget", account "GitHub"), through `security`, and goes to it on stdin.

AppKit, Foundation, objc and PyObjCTools are imported in the methods that use them, so this
file imports on any system and its pure parts are tested anywhere.
"""

import logging
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

from computer import APP_NAME, Computer

log = logging.getLogger("inab")

KEYCHAIN_SERVICE = APP_NAME
KEYCHAIN_ACCOUNT = "GitHub"


def _app():
    """NSApp, as pywebview sets it up: importing its Cocoa side makes the application (a normal
    one, with a Dock icon) and lets it follow Dark mode."""
    import AppKit
    import webview.platforms.cocoa  # noqa: F401

    return AppKit.NSApp


def _run(arguments, **kwargs):
    return subprocess.run(arguments, capture_output=True, **kwargs)


class Mac(Computer):
    name = "mac"
    update_file = (".dmg", "disk image")
    curl = "/usr/bin/curl"
    gh = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")

    def __init__(self):
        self._signatures = {}  # installed app → (team, bundle identifier), as it was at launch

    # ── folders ──

    def data_dir(self):
        return Path.home() / "Library" / "Application Support" / APP_NAME

    def log_dir(self):
        return Path.home() / "Library" / "Logs" / APP_NAME

    def documents_dir(self):
        return Path.home() / "Documents"  # iCloud Drive can back it up

    def downloads_dir(self):
        return Path.home() / "Downloads"

    # ── one copy at a time ──

    def lock(self, path):
        import fcntl

        handle = open(path, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    def already_running(self):
        """Nothing: opening the app again brings the copy that's running forward by itself.
        Only a second process started another way (`open -n`) gets this far."""

    # ── the window ──

    def on_main_thread(self, fn, *args):
        from PyObjCTools import AppHelper

        AppHelper.callAfter(fn, *args)

    def first_window_size(self, minimum, share):
        """Most of the space between the menu bar and the Dock."""
        import AppKit

        _app()
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:
            return minimum
        area = screen.visibleFrame().size
        return (int(min(area.width, max(minimum[0], area.width * share))),
                int(min(area.height, max(minimum[1], area.height * share))))

    def restore_window(self, native, name):
        """macOS saves the size and place under `name` from then on (the frame autosave, kept in
        the app's defaults). A place that no longer fits (a laptop without its monitor) is moved
        and shrunk onto a screen."""
        import AppKit

        native.setFrameUsingName_(name)
        native.setFrameAutosaveName_(name)
        frame, screens = native.frame(), AppKit.NSScreen.screens()
        if not screens:
            return None

        def overlap(screen):
            both = AppKit.NSIntersectionRect(frame, screen.visibleFrame())
            return both.size.width * both.size.height

        screen = max(screens, key=overlap)
        area = (screen if overlap(screen) else AppKit.NSScreen.mainScreen()).visibleFrame()
        width, height = min(frame.size.width, area.size.width), min(frame.size.height, area.size.height)
        x = min(max(frame.origin.x, area.origin.x), area.origin.x + area.size.width - width)
        y = min(max(frame.origin.y, area.origin.y), area.origin.y + area.size.height - height)
        if (x, y, width, height) != (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height):
            native.setFrame_display_(AppKit.NSMakeRect(x, y, width, height), True)
        return width, height

    def dark(self):
        import AppKit

        best = _app().effectiveAppearance().bestMatchFromAppearancesWithNames_(
            [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
        return best == AppKit.NSAppearanceNameDarkAqua

    def set_theme(self, native, theme):
        import AppKit

        name = {"light": AppKit.NSAppearanceNameAqua, "dark": AppKit.NSAppearanceNameDarkAqua}.get(theme)
        native.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name) if name else None)

    def when_asked_to_quit(self, should_quit):
        """macOS asks the app's delegate (applicationShouldTerminate_), with the Apple event the
        quit came with: logging out, restarting or shutting down gives a reason, ⌘Q none, and
        Quit in the Dock an event without one."""
        import AppKit
        import objc

        _app()
        from webview.platforms import cocoa

        quit_reason = int.from_bytes(b"why?", "big")  # keyAEQuitReason: set when the Mac is logging out

        def applicationShouldTerminate_(self, sender):
            event = AppKit.NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
            logging_out = event is not None and event.attributeDescriptorForKeyword_(quit_reason) is not None
            return AppKit.NSTerminateNow if should_quit(logging_out) else AppKit.NSTerminateCancel

        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [applicationShouldTerminate_])

    def quit(self):
        import AppKit

        AppKit.NSApp.terminate_(None)

    def bring_forward(self):
        app = _app()
        if hasattr(app, "activate"):
            app.activate()
        else:
            app.activateIgnoringOtherApps_(True)

    # ── opening things ──

    def open_path(self, path):
        from AppKit import NSURL, NSWorkspace

        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(path)))

    def open_web_page(self, url):
        from AppKit import NSURL, NSWorkspace

        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

    # ── where the app is installed ──

    def installed_app(self):
        """The running .app."""
        if not getattr(sys, "frozen", False):
            return None
        from Foundation import NSBundle

        return Path(NSBundle.mainBundle().bundlePath())

    def moved_to_install_folder(self, app):
        _app()
        if in_applications_folder(app):
            return False
        return offer_to_move(self, app)

    def relaunch(self, app):
        """Waiting for the exit matters: until then this copy holds the single-instance lock,
        and the new one would quit straight away."""
        arguments = []
        if os.environ.get("INAB_DATA_DIR"):  # keep a test copy on its test folder
            arguments += ["--env", f"INAB_DATA_DIR={os.environ['INAB_DATA_DIR']}"]
        arguments.append(str(app))
        script = (
            'pid=$1; shift; tries=0\n'
            'while kill -0 "$pid" 2>/dev/null && [ $tries -lt 100 ]; do sleep 0.1; tries=$((tries + 1)); done\n'
            'exec /usr/bin/open "$@"\n'
        )
        subprocess.Popen(["/bin/sh", "-c", script, "sh", str(os.getpid()), *arguments],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    # ── updates ──

    def saved_token(self):
        result = _run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                       "-w"], text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    def save_token(self, token):
        """The token goes inside quotes on `security`'s command line, so only a token's shape gets
        that far."""
        from updater import UpdateError, valid_token

        if not token:
            _run(["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT])
            return
        if not valid_token(token):
            raise UpdateError("That doesn't look like a GitHub token.")
        command = f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{KEYCHAIN_ACCOUNT}" -w "{token}"\n'
        result = _run(["/usr/bin/security", "-i"], input=command, text=True)
        if result.returncode != 0 or self.saved_token() != token:
            raise UpdateError("Couldn't save the token in the Keychain.")

    def _signed(self, app):
        """(team, bundle identifier) the installed app is signed with, read the first time they're
        asked for, at launch (why_updates_off): what every update downloaded later must match,
        whatever is put in the app's place meanwhile."""
        if app not in self._signatures:
            self._signatures[app] = (own_team(app), info_value(app, "CFBundleIdentifier"))
        return self._signatures[app]

    def why_updates_off(self, app):
        if self._signed(app)[0] is None:
            return "this copy isn't signed by a developer team, so updates can't be verified"
        if not in_applications_folder(app):
            return f"{APP_NAME} isn't in the Applications folder"
        if not (os.access(app, os.W_OK) and os.access(app.parent, os.W_OK)):
            return f"{APP_NAME} can't replace itself in {app.parent}"
        return None

    def installed_version(self, app):
        return info_value(app, "CFBundleShortVersionString")

    def folder_for_update(self, app):
        """On the same volume as the installed app, so the swap is a rename."""
        from Foundation import NSURL, NSFileManager, NSItemReplacementDirectory, NSUserDomainMask

        from updater import UpdateError

        folder_url, error = NSFileManager.defaultManager().URLForDirectory_inDomain_appropriateForURL_create_error_(
            NSItemReplacementDirectory, NSUserDomainMask, NSURL.fileURLWithPath_(str(app)), True, None)
        if folder_url is None:
            raise UpdateError(f"Couldn't make a folder for the download ({error}).")
        return Path(folder_url.path())

    def take_update(self, download, app, folder):
        team, bundle_id = self._signed(app)
        update = folder / app.name
        copy_app_from_image(download, update, bundle_id)
        download.unlink(missing_ok=True)
        return verify(update, team, bundle_id), update

    def swap_in(self, app, update):
        """Swaps the two folders in one step, so there's never a moment without an app."""
        from Foundation import NSURL, NSFileManager, NSFileManagerItemReplacementUsingNewMetadataOnly

        ok, _, error = NSFileManager.defaultManager().replaceItemAtURL_withItemAtURL_backupItemName_options_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(app)), NSURL.fileURLWithPath_(str(update)), None,
            NSFileManagerItemReplacementUsingNewMetadataOnly, None, None)
        if not ok:
            raise OSError(str(error))


# ── Applications ────────────────────────────────────────────

def in_applications_folder(path):
    resolved = str(Path(path).resolve())
    folders = ("/Applications/", str(Path.home() / "Applications") + "/")
    return resolved.startswith(folders)


def offer_to_move(mac, source):
    """Asks to move the app into Applications. Returns True if it's relaunching from there."""
    import AppKit
    from Foundation import NSURL, NSFileManager

    mac.bring_forward()
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(f"Move {APP_NAME} to your Applications folder?")
    alert.setInformativeText_(f"{APP_NAME} needs to be in Applications to keep itself up to date.")
    alert.addButtonWithTitle_("Move to Applications")
    alert.addButtonWithTitle_("Not Now")
    if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
        return False

    manager = NSFileManager.defaultManager()
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        destination = folder / source.name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                ok, _, error = manager.trashItemAtURL_resultingItemURL_error_(
                    NSURL.fileURLWithPath_(str(destination)), None, None)
                if not ok:
                    raise OSError(str(error))
            subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            log.warning("Couldn't move the app to %s: %s", folder, e)
            continue
        # The user already chose to open this download; don't make them approve the copy again.
        subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(destination)], capture_output=True)
        log.info("Moved the app to %s; relaunching", destination)
        mac.relaunch(destination)
        return True

    failure = AppKit.NSAlert.alloc().init()
    failure.setMessageText_(f"{APP_NAME} couldn't move itself")
    failure.setInformativeText_("Quit it, drag it into your Applications folder in Finder, then open it from there.")
    failure.runModal()
    return False


# ── checking an update (scripts/check-update.py runs these too) ──

def team_identifier(codesign_details):
    """The team from `codesign -d --verbose=2` output, or None if it isn't team-signed."""
    match = re.search(r"^TeamIdentifier=(\S+)$", codesign_details, re.MULTILINE)
    return match.group(1) if match and match.group(1) != "not" else None


def signing_requirement(bundle_id, team):
    return (f'=identifier "{bundle_id}" and anchor apple generic'
            f' and certificate leaf[subject.OU] = "{team}"')


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
    from updater import UpdateError

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
    from updater import UpdateError

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
    from updater import UpdateError

    result = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                   "-R", signing_requirement(bundle_id, team), str(app)], text=True)
    if result.returncode != 0:
        log.warning("Rejected update: %s", result.stderr.strip())
        raise UpdateError("The downloaded app was changed or isn't signed by the same developer.")
    version = info_value(app, "CFBundleShortVersionString")
    if not version:
        raise UpdateError(f"The latest release's disk image doesn't contain {APP_NAME}.")
    return version
