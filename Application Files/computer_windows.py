# SPDX-License-Identifier: GPL-3.0-only
"""How a Windows PC does what computer.py's interface asks.

So far: its folders and one copy at a time, which the app and the tests need to start there.
The folders come from Windows itself (its Known Folders), never from the home folder: on a
PC whose Documents OneDrive has moved, `~/Documents` is another, unsynced folder, and the
backups would quietly go there.

What isn't here yet raises NotImplementedError from computer.Computer: the window, quitting
and opening things are the Windows app's (#187); the token, the update's steps and
relaunching are Windows updates' (#188).

Windows' APIs are reached through ctypes and msvcrt, imported in the methods that use them,
so this file imports on any system.
"""

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

    def machine_id(self):
        """Windows' MachineGuid, made when Windows was installed on this PC."""
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0]) or None
        except (OSError, ImportError):
            return None

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

    # ── updates ──

    def saved_token(self):
        """None until Windows updates (#188) has somewhere to keep one."""
        return None
