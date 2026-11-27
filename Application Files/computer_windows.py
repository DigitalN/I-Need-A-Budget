# SPDX-License-Identifier: GPL-3.0-only
"""How a Windows PC does what computer.py's interface asks.

The folders come from Windows itself (its Known Folders), never from the home folder: on a
PC whose Documents OneDrive has moved, `~/Documents` is another, unsynced folder, and the
backups would quietly go there.

The window is pywebview's WinForms form drawing the page with WebView2 (Microsoft Edge's
engine). ✕ and Alt+F4 come to the app as the Mac's close button does (pywebview's closing
event, which asks the page first); logging off, shutting down, restarting, Task Manager and
an installer closing the app (Windows' Restart Manager) quit straight away: Windows is
waiting, and would show the app as "preventing shutdown". A log-off another app stops is
called off, and the app carries on. Without WebView2 the app says so and where to get it,
rather than failing with Windows' own error box. Its size and place are kept in
this computer's settings in the registry (HKEY_CURRENT_USER\\Software\\I Need A Budget), as
a Mac keeps them in its defaults.

Updates (#188): each release's installer is checked with our own release key, and runs once
the app has quit; what needs no Windows API for that is windows_update.py. The token is kept
in Credential Manager, as a generic credential named "I Need A Budget" (user name "GitHub").

The installer (scripts/installer.iss) puts the app in the person's own Programs folder,
%LOCALAPPDATA%\\Programs\\I Need A Budget, and refuses to run while the app is open: it looks
for the mutex RUNNING_MUTEX, which the copy holding the lock keeps.

Windows' APIs are reached through ctypes, msvcrt, winreg and pythonnet, imported in the
methods that use them, so this file imports on any system and its pure parts are tested
anywhere.
"""

import contextlib
import logging
import os
import sys
import uuid
from pathlib import Path

from computer import APP_NAME, Computer

log = logging.getLogger("inab")

# Known Folder IDs (KnownFolders.h).
DOCUMENTS = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
DOWNLOADS = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
LOCAL_APP_DATA = uuid.UUID("{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}")
DONT_VERIFY = 0x00004000  # KF_FLAG_DONT_VERIFY: where it is even if it's been deleted, rather than an error

RUNNING_MUTEX = f"{APP_NAME} is running"  # the installer's AppMutex: it waits for the app to be closed
SETTINGS_KEY = rf"Software\{APP_NAME}"     # this computer's settings for the app, under HKEY_CURRENT_USER
WINDOW_CLASS = "WindowsForms10.Window."    # what every WinForms window's class name starts with
APPS_THEME = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
WEBVIEW2_PAGE = "https://developer.microsoft.com/microsoft-edge/webview2/"  # Microsoft's, where the runtime is
WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION, WM_NCDESTROY, WM_SYSCOMMAND = 0x0010, 0x0011, 0x0016, 0x0082, 0x0112
SC_CLOSE = 0xF060


def known_folder(folder_id):
    """Where Windows says one of its folders is, wherever the person (or OneDrive) moved it."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    guid, path = GUID.from_buffer_copy(folder_id.bytes_le), ctypes.c_void_p()
    result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), DONT_VERIFY, None, ctypes.byref(path))
    try:
        if result != 0:
            raise OSError(f"Windows didn't say where the folder {folder_id} is ({result & 0xFFFFFFFF:#010x}).")
        return Path(ctypes.wstring_at(path.value))
    finally:
        ole32.CoTaskMemFree(path)


# ── the window's size and place (pure: tested anywhere) ──

def frame_text(frame, maximized):
    """How a frame is kept: "x y width height", and " maximized" when it was."""
    return " ".join(str(int(n)) for n in frame) + (" maximized" if maximized else "")


def read_frame(text):
    """((x, y, width, height), maximized) from frame_text, or None for anything else."""
    parts = str(text or "").split()
    maximized = parts[4:] == ["maximized"]
    if len(parts) != (5 if maximized else 4):
        return None
    try:
        x, y, width, height = (int(n) for n in parts[:4])
    except ValueError:
        return None
    return ((x, y, width, height), maximized) if width > 0 and height > 0 else None


def screen_for(frame, areas, primary):
    """The area (a screen without its taskbar) the frame overlaps most, or the primary one
    when it's on none: a laptop without its monitor. Rectangles are (x, y, width, height)."""
    def overlap(area):
        width = min(frame[0] + frame[2], area[0] + area[2]) - max(frame[0], area[0])
        height = min(frame[1] + frame[3], area[1] + area[3]) - max(frame[1], area[1])
        return max(width, 0) * max(height, 0)

    best = max(areas, key=overlap, default=primary)
    return best if overlap(best) else primary


def keep_on_a_screen(frame, areas, primary):
    """The frame moved and shrunk onto its screen (screen_for)."""
    area = screen_for(frame, areas, primary)
    width, height = min(frame[2], area[2]), min(frame[3], area[3])
    x = min(max(frame[0], area[0]), area[0] + area[2] - width)
    y = min(max(frame[1], area[1]), area[1] + area[3] - height)
    return x, y, width, height


def smallest_window(page, frame, area):
    """The window's smallest size: the page's smallest, with the frame and title bar around it,
    but never bigger than the screen's area without the taskbar (a laptop at 150% has about
    670 of the page's pixels' height)."""
    return min(page[0] + frame[0], area[2]), min(page[1] + frame[1], area[3])


def centred(width, height, area):
    """A frame of this size in the middle of the area."""
    return area[0] + (area[2] - width) // 2, area[1] + (area[3] - height) // 2, width, height


def first_size(minimum, share, area, dpi):
    """`share` of the screen's area without the taskbar (in pixels at `dpi`), in the 96-dpi
    units pywebview's window is sized in (it scales them for the screen), never below
    `minimum` unless the screen is smaller."""
    width, height = area[0] * 96 / dpi, area[1] * 96 / dpi
    return (int(min(width, max(minimum[0], width * share))),
            int(min(height, max(minimum[1], height * share))))


# ── Windows' own calls ──

def _user32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    return user32


def _bring_window_forward(hwnd):
    """Restored if it's minimized, then in front. Windows only lets the app that has the
    person's attention do that; otherwise its taskbar button flashes."""
    user32 = _user32()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: as it was before, maximized too
    user32.SetForegroundWindow(hwnd)


def _app_windows(other_than_pid):
    """The app's windows that another process (the copy that's running) shows."""
    import ctypes
    from ctypes import wintypes

    user32, found = _user32(), []

    def text(get, hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        get(hwnd, buffer, 256)
        return buffer.value

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if (pid.value != other_than_pid and user32.IsWindowVisible(hwnd)
                and text(user32.GetWindowTextW, hwnd) == APP_NAME
                and text(user32.GetClassNameW, hwnd).startswith(WINDOW_CLASS)):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return found


def _ask(text, yes_or_no=True):
    """A message box of Windows' own, before there's a window: Yes / No (True for Yes), or OK."""
    import ctypes

    MB_YESNO, MB_ICONWARNING, MB_ICONERROR, MB_SETFOREGROUND, IDYES = 0x4, 0x30, 0x10, 0x10000, 6
    style = (MB_YESNO | MB_ICONWARNING) if yes_or_no else MB_ICONERROR
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, style | MB_SETFOREGROUND) == IDYES


def webview2_missing(error):
    """Whether importing pywebview's Windows side failed for want of WebView2: it then imports
    Internet Explorer's engine instead (webview.platforms.mshtml), which the build leaves out.
    Anything else is a copy that's broken."""
    return getattr(error, "name_from", None) == "mshtml" or (error.name or "").endswith(".mshtml")


class _Closes:
    """Who's closing the window, worked out from the message it's handling, and whether a log-off
    the app said yes to really happens. WinForms' CloseReason stays as it was after a close that
    was cancelled (✕, then Keep editing): a later Task Manager close would read as the person's.
    And after WM_QUERYENDSESSION Windows sends WM_ENDSESSION, false when another app stopped the
    log-off and the person cancelled, which WinForms keeps to itself. So the window is subclassed
    (comctl32) from when it's made."""

    PERSON = "the person"              # SC_CLOSE: ✕, Alt+F4, the taskbar's Close window
    TASK_MANAGER = "Task Manager"      # a WM_CLOSE on its own: Task Manager, taskkill, another app
    SESSION = "a log-off or shutdown"  # WM_QUERYENDSESSION, Windows' Restart Manager (an installer) too
    APP = "the app"                    # Windows.quit()

    def __init__(self, called_off):
        import ctypes
        from ctypes import wintypes

        self.by = None             # while a close's message is being handled
        self.app_closing = False   # Windows.quit() has started closing the window
        self.watching = False      # the window's messages are seen
        self._comctl32 = comctl32 = ctypes.windll.comctl32
        procedure = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                       wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, procedure, ctypes.c_size_t, ctypes.c_size_t]
        comctl32.RemoveWindowSubclass.argtypes = [wintypes.HWND, procedure, ctypes.c_size_t]
        comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        comctl32.DefSubclassProc.restype = ctypes.c_ssize_t

        @procedure
        def watching(hwnd, message, wparam, lparam, ident, _):
            if message == WM_ENDSESSION and not wparam:
                try:
                    called_off()
                except Exception:
                    log.exception("Carrying on after a called-off log-off failed")
            elif message == WM_NCDESTROY:
                comctl32.RemoveWindowSubclass(hwnd, watching, ident)
                self.watching = False
            if message == WM_SYSCOMMAND and wparam & 0xFFF0 == SC_CLOSE:
                by = self.PERSON
            elif message == WM_CLOSE:  # also what SC_CLOSE becomes, inside it
                by = self.by or (self.APP if self.app_closing else self.TASK_MANAGER)
            elif message == WM_QUERYENDSESSION:
                by = self.SESSION
            else:
                return comctl32.DefSubclassProc(hwnd, message, wparam, lparam)
            outer, self.by = self.by, by
            try:
                return comctl32.DefSubclassProc(hwnd, message, wparam, lparam)
            finally:
                self.by = outer

        self._watching = watching  # kept: Windows calls it until the window goes

    def watch(self, hwnd):
        self.watching = bool(self._comctl32.SetWindowSubclass(hwnd, self._watching, 1, 0))
        if not self.watching:
            log.warning("Couldn't watch the window's messages: who closes it is WinForms' guess")

    def closing_by(self, reason):
        """Who's closing it now; WinForms' reason when the message wasn't seen."""
        if self.watching and self.by:
            return self.by
        return {"WindowsShutDown": self.SESSION, "TaskManagerClosing": self.TASK_MANAGER}.get(str(reason), self.PERSON)


def _form():
    """pywebview's window (its WinForms form) while it's there, else None."""
    winforms = sys.modules.get("webview.platforms.winforms")
    if winforms is None:
        return None
    form = next(iter(winforms.BrowserView.instances.values()), None)
    if form is None or form.IsDisposed or not form.IsHandleCreated:
        return None
    return form


class Windows(Computer):
    name = "windows"
    curl = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe")  # Windows 10 and later
    gh = (str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"),)

    _settings_key = SETTINGS_KEY  # the tests use their own

    def __init__(self):
        self._running = None  # the mutex the installer looks for, once this copy has the lock
        self._closes = None   # who closes the window (when_asked_to_quit), watched from restore_window

    # ── folders ──

    def data_dir(self):
        """In Local, not Roaming: a working copy of a database doesn't belong in a profile that's
        copied between PCs."""
        return known_folder(LOCAL_APP_DATA) / APP_NAME

    def log_dir(self):
        return self.data_dir() / "Logs"

    def documents_dir(self):
        return known_folder(DOCUMENTS)

    def downloads_dir(self):
        return known_folder(DOWNLOADS)

    # ── one copy at a time ──

    def lock(self, path):
        import ctypes
        import msvcrt

        fd = os.open(path, os.O_RDWR | os.O_CREAT)  # not truncated: another copy may hold it
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return None
        if self._running is None:  # held until the process ends
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            self._running = kernel32.CreateMutexW(None, False, RUNNING_MUTEX)
        return os.fdopen(fd, "r+b")

    def already_running(self):
        """Opening the app again (the Start menu, its pinned taskbar button) starts a second
        process: it brings the running copy's window forward, then exits. Windows lets it, as
        the process the person just started."""
        for hwnd in _app_windows(other_than_pid=os.getpid()):
            _bring_window_forward(hwnd)
            return

    # ── the window ──

    def on_main_thread(self, fn, *args):
        """Queued on the window's thread (WinForms' BeginInvoke); with no window, run now. An
        exception is logged: on the window's thread it would stop the app with Windows' own
        error box."""
        def run():
            try:
                fn(*args)
            except Exception:
                log.exception("%s failed", getattr(fn, "__name__", fn))

        form = _form()
        if form is not None:
            from System import Action

            try:
                form.BeginInvoke(Action(run))
                return
            except Exception:  # closed in between
                log.debug("The window went before %s", getattr(fn, "__name__", fn), exc_info=True)
        run()

    def first_window_size(self, minimum, share):
        """Most of the main screen, less the taskbar. The window is sized in 96-dpi units that
        pywebview scales for the screen (it makes the app DPI-aware when the window opens), so
        the screen's pixels are turned into those, whether the app is DPI-aware yet or not."""
        import ctypes
        from ctypes import wintypes

        area, user32 = wintypes.RECT(), ctypes.windll.user32
        if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):  # SPI_GETWORKAREA
            return minimum
        try:
            dpi = user32.GetDpiForSystem() or 96  # what the app sees: 96 until it's DPI-aware
        except AttributeError:  # before Windows 10 1607
            dpi = 96
        return first_size(minimum, share, (area.right - area.left, area.bottom - area.top), dpi)

    def restore_window(self, native, name):
        """The window as it was left (kept in this computer's settings in the registry, under
        `name`, saved whenever it's moved, resized, maximized or closed), moved and shrunk onto
        a screen if it no longer fits one, and maximized again if it was; the first time, in the
        middle of the main screen. Its smallest size is the page's (pywebview's minimum is the
        whole window, frame and title bar included), unless the screen is smaller."""
        import System.Windows.Forms as WinForms
        from System.Drawing import Rectangle, Size

        self._watch_window(native)
        page = (native.MinimumSize.Width, native.MinimumSize.Height)
        frame = (native.Width - native.ClientSize.Width, native.Height - native.ClientSize.Height)
        self._keep_frame(native, name, page, frame)

        primary = WinForms.Screen.PrimaryScreen.WorkingArea
        primary = (primary.X, primary.Y, primary.Width, primary.Height)
        saved = read_frame(self._setting(name))
        if saved is None:  # the first time: in the middle of the main screen, clear of the taskbar
            saved = (centred(native.Width, native.Height, primary), False)
        (x, y, width, height), maximized = saved
        areas = [(s.WorkingArea.X, s.WorkingArea.Y, s.WorkingArea.Width, s.WorkingArea.Height)
                 for s in WinForms.Screen.AllScreens]
        native.MinimumSize = Size(*smallest_window(page, frame, screen_for((x, y, width, height), areas, primary)))
        x, y, width, height = keep_on_a_screen((x, y, width, height), areas, primary)
        native.StartPosition = WinForms.FormStartPosition.Manual
        native.Bounds = Rectangle(x, y, width, height)
        if maximized:
            native.WindowState = WinForms.FormWindowState.Maximized
        return native.Width, native.Height

    def _watch_window(self, native):
        if self._closes is not None:
            self._closes.watch(native.Handle.ToInt64())

    def _keep_frame(self, native, name, page, frame):
        """Saves its size and place whenever they change, and keeps its smallest size within the
        screen it's on (moved to a smaller one, the page's minimum could go under the taskbar)."""
        import System.Windows.Forms as WinForms
        from System.Drawing import Size

        states = WinForms.FormWindowState
        last = {"state": None}

        def fit(*_):
            try:
                area = WinForms.Screen.FromControl(native).WorkingArea
                native.MinimumSize = Size(*smallest_window(page, frame, (area.X, area.Y, area.Width, area.Height)))
            except Exception:
                log.warning("Couldn't fit the window's smallest size to its screen", exc_info=True)

        def save(*_):
            try:
                state = native.WindowState
                if state == states.Minimized:  # kept as it was before
                    return
                bounds = native.Bounds if state == states.Normal else native.RestoreBounds
                self._set_setting(name, frame_text((bounds.X, bounds.Y, bounds.Width, bounds.Height),
                                                   state == states.Maximized))
                last["state"] = state
            except Exception:
                log.warning("Couldn't keep the window's size and place", exc_info=True)

        def resized(*_):
            if native.WindowState != last["state"]:  # maximized or put back; ResizeEnd has the rest
                save()

        native.ResizeEnd += fit  # after a move or a resize, first: a bigger minimum makes it bigger
        native.ResizeEnd += save
        native.Resize += resized
        native.FormClosing += save

    def dark(self):
        """Windows' app mode (Settings → Personalization → Colors), which Edge's engine follows
        for the page."""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPS_THEME) as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False

    def set_theme(self, native, theme):
        """The title bar: the page does its own colours. pywebview asks `is_dark_theme` whenever
        Windows' colours change, so the Appearance chosen keeps the title bar it chose."""
        native.is_dark_theme = {"light": lambda: False, "dark": lambda: True}.get(theme, self.dark)
        native.update_title_bar_theme()
        if native.Visible:  # Windows draws the title bar again when it's next activated
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.GetForegroundWindow.restype = wintypes.HWND
            hwnd = native.Handle.ToInt64()
            active = user32.GetForegroundWindow() == hwnd  # the page has the focus, in Edge's own window
            user32.SendMessageW(hwnd, 0x0086, not active, 0)  # WM_NCACTIVATE
            user32.SendMessageW(hwnd, 0x0086, active, 0)

    def can_draw_the_page(self):
        """pywebview needs Microsoft's WebView2 Runtime (the installer checks for it, but it can be
        removed later). Without it pywebview falls back on Internet Explorer's engine, which the
        build leaves out, and the app would stop with Windows' own error box. A copy missing
        anything else says it's damaged."""
        try:
            from webview.platforms import winforms

            if winforms.is_chromium:
                return True
        except Exception as error:
            if not (isinstance(error, ImportError) and webview2_missing(error)):
                log.exception("The window can't be made: this copy is missing something")
                _ask(f"{APP_NAME} can't open its window: part of it is missing or damaged.\n\n"
                     f"Install it again from its latest release.", yes_or_no=False)
                return False
        log.error("Microsoft Edge WebView2 isn't installed: the page can't be drawn")
        if _ask(f"{APP_NAME} needs Microsoft Edge WebView2, which this PC doesn't have.\n\n"
                f"Open Microsoft's download page? Install the Evergreen Runtime from there, then open "
                f"{APP_NAME} again."):
            self.open_web_page(WEBVIEW2_PAGE)
        return False

    def when_asked_to_quit(self, should_quit, called_off):
        """Windows closes the window with a reason (WinForms' CloseReason): ✕, Alt+F4 and the
        taskbar's Close window (SC_CLOSE) are the person's, and go the close button's way
        (pywebview's closing event, through the page). Logging off, shutting down or restarting
        (WM_QUERYENDSESSION, also an installer's Restart Manager) and Task Manager (a bare
        WM_CLOSE) quit straight away: the backup is made before Windows is told it may go on. A
        log-off that doesn't happen after all is called off. Who's closing is worked out from the
        message (_Closes), as WinForms' reason can be left from a close that was cancelled.
        pywebview's handler is replaced before the window is made, as its FormClosing is bound
        when it is."""
        from webview.platforms import winforms  # first: it loads .NET's WinForms

        form = winforms.BrowserView.BrowserForm
        pywebviews = getattr(form, "_pywebview_on_closing", None) or form.on_closing
        closes = self._closes = _Closes(called_off)

        def on_closing(window, sender, args):
            by = closes.closing_by(args.CloseReason)
            if by in (closes.SESSION, closes.TASK_MANAGER):
                log.info("Windows is closing the app (%s)", by)
                should_quit(True)  # backs up; quitting, the closing event then lets it close
            pywebviews(window, sender, args)

        form._pywebview_on_closing = pywebviews
        form.on_closing = on_closing

    def quit(self):
        """Closes the window, through pywebview: its closing event lets it close (the app is
        quitting), then WebView2 is shut down and webview.start returns."""
        import webview

        # What pywebview still has for the page as it goes (the answer to the page's own quit
        # call, say) no longer reaches it, and would only be logged as an error.
        logging.getLogger("pywebview").setLevel(logging.CRITICAL)
        if self._closes is not None:
            self._closes.app_closing = True
        for window in list(webview.windows):
            window.destroy()

    def bring_forward(self):
        form = _form()
        if form is not None:
            _bring_window_forward(form.Handle.ToInt64())

    # ── opening things ──

    def open_path(self, path):
        """A folder in File Explorer, a file in its default app (a .txt in Notepad)."""
        os.startfile(str(path))

    def open_web_page(self, url):
        if not str(url).startswith("https://"):
            raise ValueError(f"Not a web page: {url}")
        os.startfile(str(url))

    # ── where the app is installed ──

    def installed_app(self):
        """The folder the installer put the app in (I Need A Budget.exe and its _internal
        folder), %LOCALAPPDATA%\\Programs\\I Need A Budget; None run from source."""
        if not getattr(sys, "frozen", False):
            return None
        return Path(sys.executable).resolve().parent

    def moved_to_install_folder(self, app):
        """Nothing to move: the installer puts the app where it belongs."""
        return False

    # ── this computer's settings (the registry) ──

    def _setting(self, name):
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._settings_key) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def _set_setting(self, name, value):
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._settings_key) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    # ── updates (#188; what needs no Windows API is windows_update.py) ──

    update_file = ("-setup.exe", "installer")  # I-Need-A-Budget-<version>-Setup.exe
    update_signature = ".sig"                  # beside it: our release key's signature (windows_update.py)
    no_window = {"creationflags": 0x08000000}  # CREATE_NO_WINDOW: curl and gh would flash a console up
    installs_after_quitting = True             # its installer: never while logging off, when Windows could stop it half-way
    _CREDENTIAL = APP_NAME                     # the token's name in Credential Manager
    _PROGRAMS = uuid.UUID("{5CD7AEE2-2219-4A67-B85D-6C9CE15660CB}")  # FOLDERID_UserProgramFiles: %LOCALAPPDATA%\Programs
    _key = None                                # the release key as this copy had it at launch (b"": none)
    _waiting = None                            # the folder of the helper that installs an update once the app has quit
    _holding = None                            # the installer it runs, held unchangeable until this process exits

    def saved_token(self):
        import ctypes

        advapi32, Credential = _credential_api()
        found = ctypes.POINTER(Credential)()
        if not advapi32.CredReadW(self._CREDENTIAL, CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
            return None
        try:
            blob = ctypes.string_at(found.contents.CredentialBlob, found.contents.CredentialBlobSize)
            return blob.decode("utf-8", "replace")
        finally:
            advapi32.CredFree(found)

    def save_token(self, token):
        """For this user on this PC only (it doesn't roam with the profile)."""
        import ctypes

        from updater import UpdateError, valid_token

        advapi32, Credential = _credential_api()
        if not token:
            advapi32.CredDeleteW(self._CREDENTIAL, CRED_TYPE_GENERIC, 0)
            return
        if not valid_token(token):
            raise UpdateError("That doesn't look like a GitHub token.")
        blob = token.encode("utf-8")
        kept = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = Credential(Type=CRED_TYPE_GENERIC, TargetName=self._CREDENTIAL, UserName="GitHub",
                                CredentialBlobSize=len(blob), Persist=CRED_PERSIST_LOCAL_MACHINE,
                                CredentialBlob=ctypes.cast(kept, ctypes.POINTER(ctypes.c_ubyte)))
        if not advapi32.CredWriteW(ctypes.byref(credential), 0) or self.saved_token() != token:
            raise UpdateError("Couldn't save the token in Credential Manager.")

    def _release_key(self):
        """Read the first time it's asked for, at launch (why_updates_off): what every update
        downloaded later must be signed with, whatever is put in the app's folder meanwhile."""
        if self._key is None:
            import windows_update

            self._key = windows_update.release_key() or b""
        return self._key or None

    def _programs_folder(self):
        return known_folder(self._PROGRAMS)

    def why_updates_off(self, app):
        """The installer installs for this user only, always into their own Programs folder's
        `I Need A Budget`: a copy anywhere else isn't the one an update would replace."""
        import windows_update

        if self._release_key() is None:
            return "this copy has no release key to check updates with"
        if not windows_update.where_the_installer_installs(app, self._programs_folder()):
            return f"{APP_NAME} isn't installed in your Programs folder"
        return None

    def installed_version(self, app):
        """None: what runs is what's installed, so the app's own VERSION is this copy's."""
        return None

    def folder_for_update(self, app):
        import tempfile

        return Path(tempfile.mkdtemp(prefix=f"{APP_NAME} update "))

    def take_update(self, download, app, folder):
        """Nothing in an installer is run or unpacked before the release key's signature on it
        is checked. Its version is the one it says it is (its version resource, which the
        signature covers), not the release's tag: both are read while nobody can change the file
        (_hold). swap_in is given (the installer, that version), and checks both again."""
        handle = self._hold(download)
        try:
            version = self._checked_version(download)
        finally:
            self._let_go(handle)
        return version, (download, version)

    def _checked_version(self, installer):
        import windows_update
        from updater import UpdateError

        windows_update.check(installer, self._release_key())
        version = self._installer_version(installer)
        if not version:
            raise UpdateError("The latest release's installer doesn't say which version it is.")
        return version

    def _installer_version(self, path):
        """The product version in a program's version resource, as the app writes its versions
        ("1.5.0" for 1.5.0.0), or None."""
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.POINTER(wintypes.UINT)]
        version.VerQueryValueW.restype = wintypes.BOOL

        path = str(Path(path).resolve())  # a bare name would be looked for where Windows looks for DLLs
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        found, length = ctypes.c_void_p(), wintypes.UINT()
        if (not version.GetFileVersionInfoW(path, 0, size, data)
                or not version.VerQueryValueW(data, "\\", ctypes.byref(found), ctypes.byref(length))
                or length.value < 13 * 4):
            return None
        fixed = (wintypes.DWORD * 13).from_address(found.value)  # VS_FIXEDFILEINFO
        if fixed[0] != 0xFEEF04BD:
            return None
        high, low = fixed[4], fixed[5]  # dwProductVersionMS, dwProductVersionLS
        parts = [high >> 16, high & 0xFFFF, low >> 16] + ([low & 0xFFFF] if low & 0xFFFF else [])
        return ".".join(map(str, parts))

    def swap_in(self, app, update):
        """Starts the helper that runs the installer once this process has exited
        (windows_update.HELPER_SCRIPT). The installer and its signature move into the helper's
        own folder first (the download's is deleted straight after), where the installer is
        checked again as it was when downloaded, maybe hours ago, on the bytes the helper runs:
        held from then until this process exits, so nothing can change them in between. Never
        while Windows is logging off or shutting down (Updater.install doesn't ask then; this is
        in case a way of quitting didn't say)."""
        import shutil
        import tempfile

        import windows_update
        from updater import InstallError, LoggingOff, UpdateError

        installer, version = update
        if self._session_ending():
            raise LoggingOff("Windows is logging off or shutting down.")
        folder = Path(tempfile.mkdtemp(prefix=f"{APP_NAME} installing "))
        handle = None
        try:
            moved = folder / Path(installer).name
            for source, target in ((installer, moved), (windows_update.signature_of(installer),
                                                        windows_update.signature_of(moved))):
                shutil.move(str(source), str(target))
            handle = self._hold(moved)
            try:
                again = self._checked_version(moved)
            except UpdateError:
                again = None
            if again != version:
                raise InstallError(f"The installer for {version} changed after it was checked, so it wasn't run.")
            self._start_helper(app, folder, moved, version)
        except OSError as e:
            if handle is not None:
                self._let_go(handle)
            shutil.rmtree(folder, ignore_errors=True)
            if isinstance(e, InstallError):
                raise
            raise InstallError(f"Couldn't start the installer for {version} ({e}).") from e
        self._waiting, self._holding = folder, handle

    def _hold(self, path):
        """Opens `path` so that until _let_go, or this process exits, nobody can write to it,
        rename it or delete it. Reading it, and so checking it, still works."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:  # INVALID_HANDLE_VALUE
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _let_go(self, handle):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)

    def relaunch(self, app):
        """With an update waiting, its helper reopens the app once the installer is done (the
        `relaunch` file in its folder says so); otherwise a helper that only waits for this
        process to exit."""
        import tempfile

        if self._waiting is not None:
            (self._waiting / "relaunch").touch()
            return
        folder = Path(tempfile.mkdtemp(prefix=f"{APP_NAME} reopening "))
        (folder / "relaunch").touch()
        self._start_helper(app, folder, None)

    def _start_helper(self, app, folder, installer, version=None):
        """powershell.exe, with no window, out of any job that would end it with the app, in
        Windows' own folder (not one the installer replaces), with the DLL search path as Windows
        has it: PyInstaller's launcher points it at the app's own folder, and processes started
        from the app inherit that."""
        import subprocess

        import windows_update

        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        powershell = system / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        environment = windows_update.helper_environment(os.environ, wait_for=os.getpid(), folder=folder, app=app,
                                                        installer=installer, version=version,
                                                        log_file=windows_update.log_file())
        command = [str(powershell), "-NoProfile", "-NonInteractive", "-Command", windows_update.HELPER_SCRIPT]
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        with _dll_search_as_windows_has_it():
            for breakaway in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):  # refused in a job that doesn't allow it
                try:
                    subprocess.Popen(command, env=environment, cwd=str(system), creationflags=flags | breakaway,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    if not breakaway:
                        raise

    def _session_ending(self):
        import ctypes

        return bool(ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN))


# ── Windows' APIs for updates ───────────────────────────────

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # this user, this PC: not copied with a roaming profile
SM_SHUTTINGDOWN = 0x2000
GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3


def _credential_api():
    """advapi32's Credential Manager calls, and its CREDENTIALW."""
    import ctypes
    from ctypes import wintypes

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(Credential))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return advapi32, Credential


@contextlib.contextmanager
def _dll_search_as_windows_has_it():
    """For a frozen app, the DLL search path without PyInstaller's change while a process is
    started (its docs say to undo it so), then back as it was."""
    if not getattr(sys, "frozen", False):
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    kernel32.GetDllDirectoryW.restype = wintypes.DWORD
    kernel32.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel32.SetDllDirectoryW.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(32768)
    kept = buffer.value if kernel32.GetDllDirectoryW(len(buffer), buffer) else None
    kernel32.SetDllDirectoryW(None)
    try:
        yield
    finally:
        if kept:
            kernel32.SetDllDirectoryW(kept)
