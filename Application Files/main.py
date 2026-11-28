# SPDX-License-Identifier: GPL-3.0-only
"""I Need A Budget: envelope budgeting from your bank's downloads, as a desktop app.

Launcher: logging, the single-instance lock, the database, the Downloads watcher, the
budget folder's watcher, the updater, and the pywebview window. The page talks to Python
through pywebview's in-process bridge (`window.pywebview.api.call(name, args)` →
service.BudgetService), so the app opens no network ports. Its only network access is the
update check (updater.py); "Release notes" in Settings opens GitHub in your browser.

The budget lives in the budget folder the person chooses (budget_folder.py, docs/adr/0008):
this computer works on its own copy in its data folder (~/Library/Application Support/I Need
A Budget on a Mac), and saves it into the folder a few seconds after changes stop and at every
quit, after the backup. Until a folder is chosen (a copy updating from 1.4 asks first), backups
go to Documents/I Need A Budget Backups as before (backups.py). What a Mac or a Windows PC does
differently is computer.py's.

Closing the window quits the app entirely (it isn't meant to keep running in the
background), and so does ⌘Q: the page saves what you're typing first, and asks only about
a pop-up with changes, an entry it can't save, or a budget changed on two computers. A
downloaded update is installed on quit, after the backup and the save.
"""

import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import computer

APP_NAME = "I Need A Budget"
VERSION = "1.5.0"  # the build reads it from here; see the release checklist in docs/DEVELOPMENT.md
THIS_COMPUTER = computer.this()  # the Mac's or Windows' side of everything below
if os.environ.get("INAB_DATA_DIR"):  # for development/testing against a throwaway folder
    DATA_DIR = Path(os.environ["INAB_DATA_DIR"]).expanduser()
    LOG_DIR = DATA_DIR / "Logs"
    BACKUP_DIR = DATA_DIR / f"{APP_NAME} Backups"  # never the real Documents
    SUGGESTED_FOLDER = DATA_DIR.with_name(f"{DATA_DIR.name} budget folder")  # beside it, not in it
else:
    DATA_DIR = THIS_COMPUTER.data_dir()
    LOG_DIR = THIS_COMPUTER.log_dir()
    BACKUP_DIR = THIS_COMPUTER.documents_dir() / f"{APP_NAME} Backups"  # iCloud Drive can back it up
    SUGGESTED_FOLDER = THIS_COMPUTER.documents_dir() / APP_NAME  # the budget folder the window suggests
LOG_PATH = LOG_DIR / "app.log"
WATCH_EVERY = 4  # seconds between looks in Downloads, and in the budget folder
MIN_SIZE = (1000, 660)
WINDOW_SHARE = 0.85            # the first time, the window takes this much of the screen
WINDOW_FRAME_NAME = "Main window"  # macOS remembers the window's size and place under this name

log = logging.getLogger("inab")


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
    return THIS_COMPUTER.lock(DATA_DIR / ".lock")  # held open (and locked) for the life of the process


def _backups_before_the_folder_says(folder):
    """Where the copy made before upgrading the database goes, as it's opened (the budget folder
    points every backup after that, budget_folder._point_backups): its folder's Backups/, the data
    folder's while that can't be reached, and Documents' while no budget folder is chosen yet."""
    import backups
    import budget_folder

    if not folder:
        return BACKUP_DIR
    return Path(folder) / budget_folder.BACKUPS if _is_dir(folder) else DATA_DIR / backups.OLD_FOLDER_NAME


def _is_dir(path):
    try:
        return Path(path).is_dir()
    except OSError:  # a network share that's gone, a drive that won't answer
        return False


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

    def quit(self):
        """The page has saved what you were typing (or you chose to quit without it)."""
        self._app._computer.on_main_thread(self._app._quit_now)

    def asking_before_quit(self):
        """The page is asking about a pop-up with changes or an entry it can't save."""
        log.info("Quit waits: the page is asking about unsaved changes")
        self._app._computer.on_main_thread(self._app._computer.bring_forward)

    def choose_folder(self, start=None):
        """The system's folder dialog, for the budget folder: the folder chosen, or None. Not a
        request (service.dispatch): the budget isn't held up while the dialog is open."""
        return self._app._choose_folder(start)

    def look_before_quit(self):
        """As the page quits, once what you were typing is saved: the budget is saved into its
        folder now, and the state says whether the page asks first (changed on two computers, or
        an older one put back: only once you've chosen can it be saved). Not a request: a save
        waits for the requests under way (budget_folder.Turns)."""
        try:
            return self._app._service.budget_folder_look(now=True)
        except Exception:
            log.exception("Saving the budget before quitting failed")
            return None


class BudgetApp:
    def __init__(self):
        import budget_folder
        from db import Database
        from service import BudgetService
        from settings import Settings
        from updater import Updater

        self._computer = THIS_COMPUTER
        self._computer_name = budget_folder.this_computer()
        self._settings = Settings(DATA_DIR / "settings.json")
        folder = self._settings.get()["budget_folder"]
        self._db = Database(DATA_DIR / "budget.db", backup_dir=_backups_before_the_folder_says(folder),
                            backup_tag=budget_folder.tag_in(DATA_DIR, self._computer_name) if folder else None)
        self._app_path = self._computer.installed_app()
        self._updater = Updater(self._app_path, VERSION, settings=self._settings, data_dir=DATA_DIR,
                                on_status=self._on_update_status, computer=self._computer)
        self._service = BudgetService(self._db, self._settings, DATA_DIR, computer_name=self._computer_name, hooks={
            "app_info": self._app_info,
            "choose_files": self._choose_files,
            "open_data": lambda: self._open_folder(DATA_DIR),
            "open_imports": lambda: self._open_folder(self._service.folder.imports_dir()),
            "show_budget_folder": self._show_budget_folder,
            "open_log": lambda: self._open_folder(LOG_DIR),
            "open_release_notes": self._open_release_notes,
            "open_license": self._open_license,
            "update_state": self._updater.state,
            "check_updates": self._check_updates,
            "install_update": self._restart_into_update,
            "set_github_token": self._set_github_token,
        })
        self._folder_round = threading.Lock()  # one look at the budget folder at a time: the watcher's, or quit's
        self._folder_watching = False          # the watcher looks once the window is there, and stops at quit
        # Before anything else can change the budget: backups.at_launch, then another computer's newer save is
        # taken, or what a session that didn't quit normally changed is saved. It never stops the app opening.
        try:
            self._service.budget_folder_look(at_launch=True)
        except Exception:
            log.exception("Looking in the budget folder at launch failed")
        # Once, after the upgrade to version 12: which of the kept files have each transaction (issue #148). Before the
        # window opens, so nothing you do can meet it. It never stops the app opening: it's tried again next launch.
        try:
            self._service.read_imported_files()
        except Exception:
            log.warning("Couldn't read the imported files again", exc_info=True)
        # Once, at the first launch of 1.4.3: the budget takes the currency its kept files say (issue #76).
        try:
            self._service.read_currency()
        except Exception:
            log.warning("Couldn't read the imported files' currency", exc_info=True)
        self._window = None
        self._quitting = False
        self._quit_asked = None  # when the page was last asked to quit and hasn't answered yet
        self._settings.on_change(self._on_settings_changed)

    # ── hooks for the page ──

    def _app_info(self):
        return {"version": VERSION, "data_dir": str(DATA_DIR), "log_path": str(LOG_PATH),
                "updates": self._updater.state(), "platform": self._computer.name,
                "computer": self._computer_name, "suggested_folder": str(SUGGESTED_FOLDER)}

    def _choose_files(self):
        import webview

        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True,
            directory=str(self._settings.downloads_dir()),
            file_types=("Bank exports (*.csv;*.CSV;*.qfx;*.QFX;*.ofx;*.OFX;*.qbo;*.QBO)", "All files (*.*)"))
        return list(result or [])

    def _choose_folder(self, start=None):
        """The folder dialog, opening at `start` (or the nearest folder above it that's there)."""
        import webview

        place = Path(str(start)) if start else SUGGESTED_FOLDER
        while not _is_dir(place) and place.parent != place:
            place = place.parent
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(place))
        return str(result[0]) if result else None

    def _open_folder(self, path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)  # Imported Files waits for the first import
        self._computer.on_main_thread(self._computer.open_path, path)
        return True

    def _show_budget_folder(self):
        """Settings' Open beside the budget folder. Never made here: a drive's folder that isn't
        mounted would be made on the disk it mounts on (budget_folder.py)."""
        from errors import UserError

        folder = self._service.folder.folder
        if folder is None or not _is_dir(folder):
            raise UserError("Your budget folder isn't available just now.")
        self._computer.on_main_thread(self._computer.open_path, folder)
        return True

    def _open_release_notes(self):
        """In the browser: the app itself only ever talks to GitHub to update."""
        from updater import RELEASES_URL

        self._computer.on_main_thread(self._computer.open_web_page, RELEASES_URL)
        return True

    def _open_license(self):
        """The license shipped with the app, as a read-only copy in the default text app
        (TextEdit, Notepad): saving into the app itself would break its signature."""
        import shutil
        import tempfile

        frozen = getattr(sys, "frozen", False)
        source = _app_files_dir() / "LICENSE.txt" if frozen else _app_files_dir().parent / "LICENSE"
        copy = Path(tempfile.gettempdir()) / APP_NAME / "LICENSE.txt"
        copy.parent.mkdir(parents=True, exist_ok=True)
        if copy.exists():
            copy.chmod(0o644)  # Windows refuses to delete a read-only file: the second click would fail
        copy.unlink(missing_ok=True)
        shutil.copyfile(source, copy)
        copy.chmod(0o444)
        self._computer.on_main_thread(self._computer.open_path, copy)
        return True

    def _check_updates(self):
        from updater import UpdateError

        try:
            return self._updater.check_now()
        except UpdateError as e:
            from errors import UserError
            raise UserError(str(e)) from None

    def _restart_into_update(self):
        def go():
            self._back_up()  # first: the new copy starts as soon as this one has quit
            if self._updater.install(relaunch_after=True):
                self._quit_now(backed_up=True)
            else:
                self._folder_watching = True  # it carries on
        self._computer.on_main_thread(go)
        return True

    def _set_github_token(self, token):
        from errors import UserError
        from updater import UpdateError

        try:
            self._updater.save_token(str(token or "").strip())
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
        self._computer.on_main_thread(self._apply_theme, values["theme"])

    # ── Downloads watcher ──

    def _watch(self):
        while True:
            if not self._quitting:  # a log-off Windows calls off carries on watching (_quit_called_off)
                try:
                    outcome = self._service.watch_downloads()
                    if outcome:
                        self._send("imported", outcome)
                except Exception:
                    log.exception("Watching Downloads failed")
            time.sleep(WATCH_EVERY)

    # ── the budget folder's watcher ──

    def _watch_folder(self):
        """The budget folder, every few seconds (budget_folder.py): this computer's budget saved once
        changes stop, another computer's save taken, or what the page asks about. Its own thread,
        never inside a request: a take waits for the requests under way (budget_folder.Turns). The
        page hears of every change, and of every take, which it draws once you're not typing."""
        shown = None
        while True:
            state = None
            with self._folder_round:
                if self._folder_watching:
                    try:
                        state = self._service.budget_folder_look()
                    except Exception:
                        log.exception("Watching the budget folder failed")
            now = state and {k: v for k, v in state.items() if k != "took"}
            if state and (state.get("took") or now != shown):
                shown = now
                self._send("budget_folder", state)  # outside the round: the page's thread may be waiting for it
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
        if self._window is None or self._window.native is None:
            return
        self._computer.set_theme(self._window.native, theme)

    def _background_color(self):
        theme = self._settings.get()["theme"]
        if theme == "system":
            theme = "dark" if self._computer.dark() else "light"
        return "#1c1c1e" if theme == "dark" else "#f5f5f7"

    def _on_loaded(self):
        from webview.dom import DOMEventHandler

        self._computer.on_main_thread(self._apply_theme, self._settings.get()["theme"])
        try:
            document = self._window.dom.document
            document.events.dragover += DOMEventHandler(lambda e: None, prevent_default=True)
            document.events.drop += DOMEventHandler(self._on_drop, prevent_default=True, stop_propagation=True)
        except Exception:
            log.exception("Couldn't set up dropping files on the window")

    def _on_closing(self):
        """The close button quits the whole app (and installs a waiting update), once the
        page has saved what you're typing: see _ask_page_to_quit."""
        if self._quitting:
            return True
        if self._ask_page_to_quit():
            self._before_quit()
            return True
        return False

    def _ask_page_to_quit(self):
        """Close and ⌘Q (on the main thread): the page saves what you're typing, or asks
        about a pop-up with changes or an entry it can't save, then calls Api.quit. True
        to quit now instead: a page that isn't there, or hasn't answered by the next try
        3 s later, doesn't keep the app open."""
        loaded = self._window is not None and self._window.events.loaded.is_set()
        stuck = self._quit_asked is not None and time.monotonic() - self._quit_asked > 3
        if not loaded or stuck:
            return True
        if self._quit_asked is not None:
            return False
        self._quit_asked = time.monotonic()

        def ask():
            try:
                answered = self._window.evaluate_js("window.quitRequested ? window.quitRequested() : false")
            except Exception:
                log.exception("Couldn't ask the page before quitting")
                answered = False
            self._quit_asked = None
            if not answered:
                self._computer.on_main_thread(self._quit_now)
        threading.Thread(target=ask, name="quit", daemon=True).start()
        return False

    def _before_quit(self, backed_up=False, logging_out=False):
        """Once, whichever way the app quits: a backup (if the budget changed) and the save into
        the budget folder, then a downloaded update is installed (while logging out only where
        that's one step: Updater.install)."""
        if self._quitting:
            return
        self._quitting = True
        if not backed_up:
            self._back_up()
        try:
            self._updater.install(relaunch_after=False, logging_out=logging_out)
        except Exception:
            log.exception("Installing the update on quit failed")
        log.info("Quitting")

    def _back_up(self):
        """A backup if the budget changed since the newest one, then a save into the budget folder
        if it changed since the last save, and this computer's note taken out of the folder
        (budget_folder.at_quit). The watcher stops first: a look after this would leave the note
        again."""
        with self._folder_round:
            self._folder_watching = False
        try:
            self._service.budget_folder_at_quit()
        except Exception:
            log.exception("Backing up and saving at quit failed")

    def _restore_frame(self):
        """Just before the window first shows (on the main thread): puts it back the size and
        place it was left, which the system remembers under WINDOW_FRAME_NAME from then on, and
        keeps it on a screen: a place that no longer fits (a laptop without its monitor) is
        moved and shrunk to fit."""
        try:
            size = self._computer.restore_window(self._window.native, WINDOW_FRAME_NAME)
            if size:
                log.info("Window %d×%d", *size)
        except Exception:
            log.exception("Couldn't put the window back where it was")

    def _quit_now(self, backed_up=False):
        self._before_quit(backed_up)
        self._computer.quit()

    def _should_quit(self, logging_out):
        """⌘Q (or Quit in the Dock) goes the close button's way, through the page. Logging
        out, restarting or shutting down (on Windows Task Manager and installers too) quits
        straight away: holding it up would stop the computer. True quits now."""
        if self._quitting:
            return True
        if logging_out or self._ask_page_to_quit():
            self._before_quit(logging_out=logging_out)
            return True
        return False

    def _quit_called_off(self):
        """Windows asked to quit for a log-off or shutdown, the app said it may (backed up and
        quitting), then another app stopped it and the person cancelled: the app carries on as it
        was. The backup stays; the next quit makes another only after a change. An update the
        log-off kept for the next launch goes in at the next quit again (Updater.log_off_called_off).
        The budget folder's watcher looks again (its next look leaves this computer's note again)."""
        if not self._quitting:
            return
        log.info("The log-off or shutdown was called off: carrying on")
        self._quitting = False
        self._quit_asked = None
        self._folder_watching = True
        try:
            self._updater.log_off_called_off()
        except Exception:
            log.exception("Taking back the update the log-off kept failed")

    def run(self):
        import webview

        if self._app_path and self._computer.moved_to_install_folder(self._app_path):
            return  # it's relaunching from there
        if self._updater.take_kept():  # an update a log-off kept, checked again: in before the window opens
            self._back_up()            # as Restart now does it
            if self._updater.install(relaunch_after=True):
                return                 # the app reopens once its installer is done
        if not self._computer.can_draw_the_page():
            return  # it has said what's missing
        self._computer.when_asked_to_quit(self._should_quit, self._quit_called_off)
        html = (_app_files_dir() / "index.html").read_text(encoding="utf-8")
        width, height = self._computer.first_window_size(MIN_SIZE, WINDOW_SHARE)
        self._window = webview.create_window(
            APP_NAME, html=html, js_api=Api(self), width=width, height=height,
            min_size=MIN_SIZE, background_color=self._background_color(), text_select=True)
        self._window.events.before_show += self._restore_frame
        if self._computer.name == "windows":  # its title bar is drawn before the page: in the chosen Appearance
            self._window.events.before_show += lambda: self._apply_theme(self._settings.get()["theme"])
        self._window.events.closing += self._on_closing
        self._window.events.loaded += self._on_loaded
        threading.Thread(target=self._watch, name="downloads-watcher", daemon=True).start()
        self._folder_watching = True
        threading.Thread(target=self._watch_folder, name="budget-folder-watcher", daemon=True).start()
        # The re-check: what's waiting in the inbox follows the current rules (they can change in an
        # update). Never what you decided (docs/adr/0005).
        threading.Thread(target=self._service.dispatch, args=("recheck",), name="recheck", daemon=True).start()
        self._updater.start()
        log.info("%s %s started (data in %s)", APP_NAME, VERSION, DATA_DIR)
        webview.start(private_mode=True, debug=bool(os.environ.get("INAB_DEBUG")))
        self._before_quit()  # already done, unless the window closed some other way
        os._exit(0)


def main():
    _setup_logging()
    lock = _acquire_single_instance_lock()
    if lock is None:
        log.info("Another copy is already running; exiting")
        THIS_COMPUTER.already_running()
        return
    sys.path.insert(0, str(_app_files_dir()))
    try:
        BudgetApp().run()
    except Exception:
        log.exception("%s failed to start", APP_NAME)
        raise


if __name__ == "__main__":
    main()
