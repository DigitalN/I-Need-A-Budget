"""Where the app runs from, and relaunching it.

It has to run from an Applications folder to update itself: opened from a disk image
or Downloads, macOS runs a temporary hidden copy ("app translocation") that can't be
replaced. On launch from anywhere else, it offers to move.

Everything here is a no-op when running from source (`python3 main.py`).
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("inab")

APP_NAME = "I Need A Budget"
REPOSITORY = "DigitalN/I-Need-A-Budget"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"


def bundle_path():
    """The running .app, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    from Foundation import NSBundle

    return Path(NSBundle.mainBundle().bundlePath())


def in_applications_folder(path):
    resolved = str(Path(path).resolve())
    folders = ("/Applications/", str(Path.home() / "Applications") + "/")
    return resolved.startswith(folders)


def activate_app():
    import AppKit

    app = AppKit.NSApp
    if hasattr(app, "activate"):
        app.activate()
    else:
        app.activateIgnoringOtherApps_(True)


def offer_to_move(source):
    """Asks to move the app into Applications. Returns True if it's relaunching from there."""
    import AppKit
    from Foundation import NSURL, NSFileManager

    activate_app()
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
        relaunch(destination)
        return True

    failure = AppKit.NSAlert.alloc().init()
    failure.setMessageText_(f"{APP_NAME} couldn't move itself")
    failure.setInformativeText_("Quit it, drag it into your Applications folder in Finder, then open it from there.")
    failure.runModal()
    return False


def relaunch(app):
    """Opens `app` once this process has exited. The caller quits right after.

    Waiting for the exit matters: until then this copy holds the single-instance
    lock, and the new one would quit straight away.
    """
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
