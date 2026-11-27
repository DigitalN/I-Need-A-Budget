# SPDX-License-Identifier: GPL-3.0-only
"""How a Windows PC does what computer.py's interface asks.

So far: its folders and one copy at a time, which the app and the tests need to start there.
The folders come from Windows itself (its Known Folders), never from the home folder: on a
PC whose Documents OneDrive has moved, `~/Documents` is another, unsynced folder, and the
backups would quietly go there.

What isn't here yet raises NotImplementedError from computer.Computer: the window, quitting
and opening things are the Windows app's (#187).

Updates (#188): each release's installer is checked with our own release key, and runs once
the app has quit; what needs no Windows API for that is windows_update.py. The token is kept
in Credential Manager, as a generic credential named "I Need A Budget" (user name "GitHub").

Windows' APIs are reached through ctypes and msvcrt, imported in the methods that use them,
so this file imports on any system.
"""

import contextlib
import os
import sys
import uuid
from pathlib import Path

from computer import APP_NAME, Computer

# Known Folder IDs (KnownFolders.h).
DOCUMENTS = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")
DOWNLOADS = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
LOCAL_APP_DATA = uuid.UUID("{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}")
DONT_VERIFY = 0x00004000  # KF_FLAG_DONT_VERIFY: where it is even if it's been deleted, rather than an error


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


class Windows(Computer):
    name = "windows"
    curl = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe")  # Windows 10 and later
    gh = (str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"),)

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
        import msvcrt

        fd = os.open(path, os.O_RDWR | os.O_CREAT)  # not truncated: another copy may hold it
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return None
        return os.fdopen(fd, "r+b")

    def already_running(self):
        """Nothing yet: the Windows app (#187) brings the running copy's window forward."""

    # ── where the app is installed ──

    def installed_app(self):
        if not getattr(sys, "frozen", False):
            return None
        raise NotImplementedError("the Windows app (#187)")

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
