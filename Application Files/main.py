"""I Need A Budget: envelope budgeting from your bank's downloads, as a Mac app.

Launcher: logging, the single-instance lock, the database, the Downloads watcher, the
updater, and the pywebview window. The page talks to Python through pywebview's
in-process bridge (`window.pywebview.api.call(name, args)` → service.BudgetService), so
the app opens no network ports. Its only network access is the update check
(updater.py); "Release notes" in Settings opens GitHub in your browser. All data stays in
~/Library/Application Support/I Need A Budget.

Closing the window asks, then quits the app entirely (it isn't meant to keep running
in the background). ⌘Q quits without asking. A downloaded update is installed on quit.
"""

import fcntl
import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "I Need A Budget"
VERSION = "1.2.1"  # the build reads it from here; see the release checklist in DEVELOPMENT.md
if os.environ.get("INAB_DATA_DIR"):  # for development/testing against a throwaway folder
    DATA_DIR = Path(os.environ["INAB_DATA_DIR"]).expanduser()
    LOG_DIR = DATA_DIR / "Logs"
else:
    DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
    LOG_DIR = Path.home() / "Library" / "Logs" / APP_NAME
LOG_PATH = LOG_DIR / "app.log"
WATCH_EVERY = 4  # seconds between looks in Downloads
MIN_SIZE = (1000, 660)
WINDOW_SHARE = 0.85            # the first time, the window takes this much of the screen
WINDOW_FRAME_NAME = "Main window"  # macOS remembers the window's size and place under this name

log = logging.getLogger("inab")


def _first_window_size():
    """How big the window opens the first time on a Mac: most of the space between the
    menu bar and the Dock, and never below the minimum unless the screen is smaller. From
    then on it opens the size and place it was left (BudgetApp._restore_frame)."""
    import AppKit

    screen = AppKit.NSScreen.mainScreen()
    if screen is None:
        return MIN_SIZE
    area = screen.visibleFrame().size
    return (int(min(area.width, max(MIN_SIZE[0], area.width * WINDOW_SHARE))),
            int(min(area.height, max(MIN_SIZE[1], area.height * WINDOW_SHARE))))


def _app_files_dir():
    """Directory holding index.html: the PyInstaller bundle when frozen, else this folder."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])
    logging.getLogger("pywebview").setLevel(logging.WARNING)


def _acquire_single_instance_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(DATA_DIR / ".lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle  # held open (and locked) for the life of the process


class Api:
    """Callable from the page as window.pywebview.api.<name>().

    pywebview exposes every public attribute recursively, so everything that is not
    meant for JavaScript lives behind a leading underscore.
    """

    def __init__(self, app):
        self._app = app

    def call(self, name, args=None):
        if name == "bootstrap":
            log.info("Page connected")
        return self._app._service.dispatch(str(name), args)

    def log_error(self, message):
        """Page errors land in app.log next to the Python ones."""
        log.warning("Page error: %s", str(message)[:2000])


class BudgetApp:
    def __init__(self):
        import launch
        from db import Database
        from service import BudgetService
        from settings import Settings
        from updater import Updater

        self._settings = Settings(DATA_DIR / "settings.json")
        self._db = Database(DATA_DIR / "budget.db")
        try:
            self._db.backup_daily(DATA_DIR / "Backups")
        except Exception:
            log.exception("Daily backup failed")
        self._app_path = launch.bundle_path()
        self._updater = Updater(self._app_path, VERSION, settings=self._settings, data_dir=DATA_DIR,
                                on_status=self._on_update_status)
        self._service = BudgetService(self._db, self._settings, DATA_DIR, hooks={
            "app_info": self._app_info,
            "choose_files": self._choose_files,
            "open_data": lambda: self._open_folder(DATA_DIR),
            "open_imports": lambda: self._open_folder(DATA_DIR / "Imported Files"),
            "open_log": lambda: self._open_folder(LOG_DIR),
            "open_release_notes": self._open_release_notes,
            "update_state": self._updater.state,
            "check_updates": self._check_updates,
            "install_update": self._restart_into_update,
            "set_github_token": self._set_github_token,
        })
        self._window = None
        self._quitting = False
        self._settings.on_change(self._on_settings_changed)

    # ── hooks for the page ──

    def _app_info(self):
        return {"version": VERSION, "data_dir": str(DATA_DIR), "log_path": str(LOG_PATH),
                "updates": self._updater.state()}

    def _choose_files(self):
        import webview

        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True,
            directory=str(self._settings.downloads_dir()),
            file_types=("Bank exports (*.csv;*.CSV;*.qfx;*.QFX;*.ofx;*.OFX;*.qbo;*.QBO)", "All files (*.*)"))
        return list(result or [])

    def _open_folder(self, path):
        from AppKit import NSURL, NSWorkspace
        from PyObjCTools import AppHelper

        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)  # Imported Files waits for the first import
        AppHelper.callAfter(NSWorkspace.sharedWorkspace().openURL_, NSURL.fileURLWithPath_(str(path)))
        return True

    def _open_release_notes(self):
        """In the browser: the app itself only ever talks to GitHub to update."""
        from AppKit import NSURL, NSWorkspace
        from PyObjCTools import AppHelper

        import launch

        AppHelper.callAfter(NSWorkspace.sharedWorkspace().openURL_, NSURL.URLWithString_(launch.RELEASES_URL))
        return True

    def _check_updates(self):
        from updater import UpdateError

        try:
            return self._updater.check_now()
        except UpdateError as e:
            from service import UserError
            raise UserError(str(e)) from None

    def _restart_into_update(self):
        from PyObjCTools import AppHelper

        def go():
            if self._updater.install(relaunch_after=True):
                self._quit_now()
        AppHelper.callAfter(go)
        return True

    def _set_github_token(self, token):
        from service import UserError
        from updater import UpdateError, save_keychain_token

        try:
            save_keychain_token(str(token or "").strip())
        except UpdateError as e:
            raise UserError(str(e)) from None
        return self._updater.state()

    # ── events to the page ──

    def _send(self, event, payload):
        if self._window is None:
            return
        try:
            self._window.evaluate_js(f"window.appEvent && window.appEvent({json.dumps(event)}, {json.dumps(payload)})")
        except Exception:
            log.debug("Couldn't reach the page", exc_info=True)

    def _on_update_status(self, state):
        self._send("update", state)

    def _on_settings_changed(self, values):
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._apply_theme, values["theme"])

    # ── Downloads watcher ──

    def _watch(self):
        while not self._quitting:
            try:
                outcome = self._service.watch_downloads()
                if outcome:
                    self._send("imported", outcome)
            except Exception:
                log.exception("Watching Downloads failed")
            time.sleep(WATCH_EVERY)

    def _on_drop(self, event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        if not paths:
            return
        outcome = self._service.dispatch("import_files", [paths])
        self._send("imported", outcome.get("result") or {"error": outcome.get("error")})

    # ── window (main thread) ──

    def _apply_theme(self, theme):
        import AppKit

        if self._window is None or self._window.native is None:
            return
        name = {"light": AppKit.NSAppearanceNameAqua, "dark": AppKit.NSAppearanceNameDarkAqua}.get(theme)
        self._window.native.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name) if name else None)

    def _background_color(self):
        import AppKit

        theme = self._settings.get()["theme"]
        if theme == "system":
            best = AppKit.NSApp.effectiveAppearance().bestMatchFromAppearancesWithNames_(
                [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
            theme = "dark" if best == AppKit.NSAppearanceNameDarkAqua else "light"
        return "#1c1c1e" if theme == "dark" else "#f5f5f7"

    def _on_loaded(self):
        from PyObjCTools import AppHelper
        from webview.dom import DOMEventHandler

        AppHelper.callAfter(self._apply_theme, self._settings.get()["theme"])
        try:
            document = self._window.dom.document
            document.events.dragover += DOMEventHandler(lambda e: None, prevent_default=True)
            document.events.drop += DOMEventHandler(self._on_drop, prevent_default=True, stop_propagation=True)
        except Exception:
            log.exception("Couldn't set up dropping files on the window")

    def _on_closing(self):
        """The close button asks, then quits the whole app (and installs a waiting update)."""
        import AppKit

        if self._quitting:
            return True
        import launch

        launch.activate_app()
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"Quit {APP_NAME}?")
        ready = self._updater.status == "ready"
        alert.setInformativeText_(
            f"Version {self._updater.version} will be installed as it quits." if ready else
            "Everything is saved. New bank downloads are imported the next time you open it.")
        alert.addButtonWithTitle_("Quit")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return False
        self._before_quit()
        return True

    def _before_quit(self):
        self._quitting = True
        try:
            self._updater.install(relaunch_after=False)
        except Exception:
            log.exception("Installing the update on quit failed")
        log.info("Quitting")

    def _restore_frame(self):
        """Just before the window first shows (on the main thread): puts it back the size and
        place it was left, which macOS saves under WINDOW_FRAME_NAME from then on, and keeps
        it on a screen: a place that no longer fits (a laptop without its monitor) is moved
        and shrunk to fit."""
        import AppKit

        try:
            window = self._window.native
            window.setFrameUsingName_(WINDOW_FRAME_NAME)
            window.setFrameAutosaveName_(WINDOW_FRAME_NAME)
            frame, screens = window.frame(), AppKit.NSScreen.screens()
            if not screens:
                return

            def overlap(screen):
                both = AppKit.NSIntersectionRect(frame, screen.visibleFrame())
                return both.size.width * both.size.height

            screen = max(screens, key=overlap)
            area = (screen if overlap(screen) else AppKit.NSScreen.mainScreen()).visibleFrame()
            width, height = min(frame.size.width, area.size.width), min(frame.size.height, area.size.height)
            x = min(max(frame.origin.x, area.origin.x), area.origin.x + area.size.width - width)
            y = min(max(frame.origin.y, area.origin.y), area.origin.y + area.size.height - height)
            if (x, y, width, height) != (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height):
                window.setFrame_display_(AppKit.NSMakeRect(x, y, width, height), True)
            log.info("Window %d×%d", width, height)
        except Exception:
            log.exception("Couldn't put the window back where it was")

    def _quit_now(self):
        import AppKit

        self._before_quit()
        AppKit.NSApp.terminate_(None)

    def _install_quit_handler(self):
        """⌘Q quits straight away (pywebview's default would ask through the close handler)."""
        import AppKit
        import objc
        from webview.platforms import cocoa

        app = self

        def applicationShouldTerminate_(self, sender):
            if not app._quitting:
                app._before_quit()
            return AppKit.NSTerminateNow

        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [applicationShouldTerminate_])

    def run(self):
        import webview
        import webview.platforms.cocoa  # noqa: F401 (sets up NSApp)

        import launch

        if self._app_path and not launch.in_applications_folder(self._app_path):
            if launch.offer_to_move(self._app_path):
                return
        self._install_quit_handler()
        html = (_app_files_dir() / "index.html").read_text(encoding="utf-8")
        width, height = _first_window_size()
        self._window = webview.create_window(
            APP_NAME, html=html, js_api=Api(self), width=width, height=height,
            min_size=MIN_SIZE, background_color=self._background_color(), text_select=True)
        self._window.events.before_show += self._restore_frame
        self._window.events.closing += self._on_closing
        self._window.events.loaded += self._on_loaded
        threading.Thread(target=self._watch, name="downloads-watcher", daemon=True).start()
        # The re-check: what's waiting in the inbox follows the current rules (they can change in an
        # update). Never what you decided (docs/adr/0005).
        threading.Thread(target=self._service.dispatch, args=("recheck",), name="recheck", daemon=True).start()
        self._updater.start()
        log.info("%s %s started (data in %s)", APP_NAME, VERSION, DATA_DIR)
        webview.start(private_mode=True, debug=bool(os.environ.get("INAB_DEBUG")))
        self._quitting = True
        os._exit(0)


def main():
    _setup_logging()
    lock = _acquire_single_instance_lock()
    if lock is None:
        log.info("Another copy is already running; exiting")
        return
    sys.path.insert(0, str(_app_files_dir()))
    try:
        BudgetApp().run()
    except Exception:
        log.exception("%s failed to start", APP_NAME)
        raise


if __name__ == "__main__":
    main()
