# SPDX-License-Identifier: GPL-3.0-only
"""What differs between a Mac and a Windows PC, behind one small interface (issue #182).

`this()` is the one place that picks: `computer_mac.Mac` on a Mac, `computer_windows.Windows`
on Windows. Nothing outside those two files imports AppKit, Foundation, objc, PyObjCTools,
fcntl or the Windows APIs, or runs a command-line tool only one system has. Both modules
import anywhere (what each system needs is imported inside the method that uses it), so the
pure parts are tested on either.

Everything that's the same on both (when to back up, asking the page before quitting, when
to check for updates, GitHub's answers, the token's shape) stays where it was: main.py,
updater.py, settings.py. A Windows method that raises NotImplementedError belongs to the
Windows app (#187).
"""

import sys

APP_NAME = "I Need A Budget"


class Computer:
    """The interface. Each method says what it's for; computer_mac.py has how a Mac does it."""

    name = None                  # "mac" or "windows"
    update_file = (None, None)   # what each release has for this system: (suffix, what it's called)
    update_signature = None      # what's added to that file's name to name its signature, when it has one beside it
    installs_after_quitting = False  # an installer puts the update in once the app has gone (so never while logging out)
    curl = None                  # the curl the updater runs, which uses the system's certificates
    no_window = {}               # how a command-line tool starts without a console window flashing up (Windows')
    gh = ()                      # where the GitHub CLI may be: an app opened from Finder or Start lacks the shell's PATH

    # ── folders ──

    def data_dir(self):
        """This computer's data folder: the budget, settings.json, Imported Files."""
        raise NotImplementedError

    def log_dir(self):
        raise NotImplementedError

    def documents_dir(self):
        """The person's Documents folder, as the system has it (where backups go)."""
        raise NotImplementedError

    def downloads_dir(self):
        """The person's Downloads folder, as the system has it (Settings' default)."""
        raise NotImplementedError

    # ── one copy at a time ──

    def lock(self, path):
        """Locks `path` for as long as the returned handle is kept; None when another copy
        has it."""
        raise NotImplementedError

    def already_running(self):
        """A second launch, just before it exits: whatever this system needs to bring the
        copy that's running forward."""
        raise NotImplementedError

    # ── the window ──

    def on_main_thread(self, fn, *args):
        """Runs fn(*args) on the window's thread, later; the window's calls must go there."""
        raise NotImplementedError

    def first_window_size(self, minimum, share):
        """(width, height) the first time the window opens: `share` of the screen, never
        below `minimum` unless the screen is smaller."""
        raise NotImplementedError

    def restore_window(self, native, name):
        """Just before the window first shows: puts it back the size and place it was left
        (remembered under `name`), kept on a screen. Returns (width, height), or None."""
        raise NotImplementedError

    def dark(self):
        """Whether the system is in Dark mode now."""
        raise NotImplementedError

    def set_theme(self, native, theme):
        """The window in "light", "dark" or the system's ("system")."""
        raise NotImplementedError

    def can_draw_the_page(self):
        """Before the window: whether this computer can draw the page. When it can't (Windows
        without Microsoft's WebView2), it has told the person what's missing and where to get it,
        and the app stops."""
        raise NotImplementedError

    def when_asked_to_quit(self, should_quit, called_off):
        """Every way the system asks the app to quit, besides the close button (⌘Q, Quit in
        the Dock, logging out, shutting down), calls `should_quit(logging_out)`: True quits
        now, False keeps the app open (the page is asking first). When the app said it may and
        the system then doesn't quit after all (Windows: another app stopped the log-off),
        `called_off()`."""
        raise NotImplementedError

    def quit(self):
        raise NotImplementedError

    def bring_forward(self):
        raise NotImplementedError

    # ── opening things ──

    def open_path(self, path):
        """A folder in the system's file manager, or a file in its default app."""
        raise NotImplementedError

    def open_web_page(self, url):
        """In the person's browser: the app itself never goes online but to update."""
        raise NotImplementedError

    # ── where the app is installed ──

    def installed_app(self):
        """The installed app this copy runs from, or None when it runs from source."""
        raise NotImplementedError

    def moved_to_install_folder(self, app):
        """Before the window: True when the app was somewhere it can't update itself, has
        been moved where it can and is relaunching from there (this copy then stops)."""
        raise NotImplementedError

    def relaunch(self, app):
        """Opens `app` once this process has exited (and an update swap_in started has gone in).
        The caller quits right after."""
        raise NotImplementedError

    # ── updates ──

    def saved_token(self):
        """The GitHub token saved from Settings, or None."""
        raise NotImplementedError

    def save_token(self, token):
        """Saves the token, or with "" removes it. Raises updater.UpdateError when it can't."""
        raise NotImplementedError

    def why_updates_off(self, app):
        """Why this installed copy can't update itself, or None when it can."""
        raise NotImplementedError

    def installed_version(self, app):
        raise NotImplementedError

    def folder_for_update(self, app):
        """A new empty folder to download an update into (on a Mac beside `app`, so it can be
        swapped in)."""
        raise NotImplementedError

    def take_update(self, download, app, folder):
        """Unpacks the downloaded update into `folder` and checks it's this app, intact and
        from the same developer. Returns (its version, what swap_in puts in place). Raises
        updater.UpdateError for anything else."""
        raise NotImplementedError

    def swap_in(self, app, update):
        """Puts the update where `app` is: in one step on a Mac, and on Windows by its installer,
        once this process has exited. Raises OSError when it can't."""
        raise NotImplementedError


_this = None


def this():
    """This computer: the one place the system is picked."""
    global _this
    if _this is None:
        if sys.platform == "darwin":
            from computer_mac import Mac
            _this = Mac()
        elif sys.platform == "win32":
            from computer_windows import Windows
            _this = Windows()
        else:
            raise RuntimeError(f"{APP_NAME} runs on a Mac or a Windows PC, not {sys.platform}.")
    return _this
