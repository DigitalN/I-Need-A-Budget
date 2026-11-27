# SPDX-License-Identifier: GPL-3.0-only
"""Windows updates, checked with our own release key (ADR 0009, issue #188).

A release has `I-Need-A-Budget-<version>-Setup.exe` for Windows with its signature beside it,
`I-Need-A-Budget-<version>-Setup.exe.sig`: Ed25519 (RFC 8032, not pre-hashed) over the
installer's exact bytes, the 64-byte signature in base64 on one line. Only the release run
on GitHub can sign, with the release key kept in its protected environment. Its public half,
`release-key.pub` (the raw 32-byte key in base64, one line), is built into the app, and a
Windows copy runs no installer it hasn't checked with it (docs/DEVELOPMENT.md → Release
secrets has the formats).

The installer runs once the app has quit: Windows won't let it replace the app's files while
it runs. A small PowerShell script waits for the app to exit, runs the installer quietly,
then reopens the app if Restart now asked for that (`HELPER_SCRIPT`; computer_windows.py
starts it).

All of this is pure Python, so it's tested on any system: Ed25519 verification as RFC 8032
§5.1.7 has it (the same steps as its §6 reference code, checked against its test vectors).
Verifying needs no secret and has nothing to keep secret, so it needn't run in constant time.
"""

import base64
import binascii
import hashlib
import sys
from pathlib import Path

from computer import APP_NAME

# ── Ed25519 verification (RFC 8032) ─────────────────────────

P = 2 ** 255 - 19                                   # the field
L = 2 ** 252 + 27742317777372353535851937790883648493  # the group's order
D = -121665 * pow(121666, P - 2, P) % P             # the curve's constant
SQRT_M1 = pow(2, (P - 1) // 4, P)                   # a square root of -1
_IDENTITY = (0, 1, 1, 0)


def _add(a, b):
    """Two points in extended coordinates (X, Y, Z, T), x = X/Z, y = Y/Z, xy = T/Z."""
    e1 = (a[1] - a[0]) * (b[1] - b[0]) % P
    e2 = (a[1] + a[0]) * (b[1] + b[0]) % P
    e3 = 2 * a[3] * b[3] * D % P
    e4 = 2 * a[2] * b[2] % P
    e, f, g, h = e2 - e1, e4 - e3, e4 + e3, e2 + e1
    return e * f % P, g * h % P, f * g % P, e * h % P


def _multiply(scalar, point):
    result = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _x_for(y, sign):
    """The x with this y and lowest bit, or None when there's none (RFC 8032 §5.1.3)."""
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    return P - x if (x & 1) != sign else x


def _point(encoded):
    """A point from its 32 bytes, or None when they aren't one."""
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x_for(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


_BASE_Y = 4 * pow(5, P - 2, P) % P
_BASE_X = _x_for(_BASE_Y, 0)
_BASE = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % P)


def _small_order(point):
    """Whether eight times the point is the identity: one of the curve's eight points of small
    order, for which anyone can make a signature that passes, without any private key."""
    return _same(_multiply(8, point), _IDENTITY)


def usable_key(public_key):
    """Whether `public_key` is a key a signature proves anything for: 32 bytes that are a point,
    written the one way RFC 8032 writes it (`_point` refuses a y of p or more, and x = 0 with
    x's sign set), and not of small order (a placeholder of 32 zero bytes is one)."""
    point = _point(public_key) if isinstance(public_key, bytes) else None
    return point is not None and not _small_order(point)


def verify(public_key, signature, message):
    """Whether `signature` (64 bytes) is `public_key`'s (32 bytes) over `message`: bytes, or an
    iterable of bytes (a file read in pieces)."""
    if not isinstance(public_key, bytes) or not isinstance(signature, bytes):
        return False
    if len(public_key) != 32 or len(signature) != 64 or not usable_key(public_key):
        return False
    a, r = _point(public_key), _point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if r is None or s >= L:
        return False
    digest = hashlib.sha512(signature[:32] + public_key)
    for piece in [message] if isinstance(message, (bytes, bytearray)) else message:
        digest.update(piece)
    k = int.from_bytes(digest.digest(), "little") % L
    return _same(_multiply(s, _BASE), _add(r, _multiply(k, a)))


# ── the release key and an installer's signature ────────────

RELEASE_KEY_FILE = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent) / "release-key.pub"
SIGNATURE = ".sig"   # the signature's name: the installer's, with this added
QUIET = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")  # the installer, without a window or a question


def read_key(text, size):
    """`size` bytes from one line of base64, or None when that isn't what `text` is."""
    lines = text.split()
    if len(lines) != 1:
        return None
    try:
        raw = base64.b64decode(lines[0], validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == size else None


def release_key(path=None):
    """The release key's public half built into this copy, or None when it has none: a copy
    built before the maintainer made the key, the file damaged, or a key no signature proves
    anything for (`usable_key`). Updates are then off."""
    try:
        key = read_key((path or RELEASE_KEY_FILE).read_text(encoding="ascii"), 32)
    except (OSError, UnicodeDecodeError):
        return None
    return key if key is not None and usable_key(key) else None


def _pieces(path, size=1 << 20):
    with open(path, "rb") as f:
        while piece := f.read(size):
            yield piece


def signature_of(installer):
    """Where an installer's signature is: beside it, its name with SIGNATURE added."""
    installer = Path(installer)
    return installer.with_name(installer.name + SIGNATURE)


def check(installer, public_key):
    """Refuses an installer our release key didn't sign (its signature: signature_of). Raises
    updater.UpdateError, or returns."""
    from updater import UpdateError

    installer = Path(installer)
    if public_key is None:
        raise UpdateError("This copy has no release key to check updates with.")
    try:
        text = signature_of(installer).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise UpdateError("The latest release's installer isn't signed.") from None
    signature = read_key(text, 64)
    if signature is None or not verify(public_key, signature, _pieces(installer)):
        raise UpdateError("The downloaded installer was changed or isn't signed with the release key.")


def app_folder(app):
    """The folder the app is installed in: `app` is that folder, or the .exe in it."""
    app = Path(app)
    return app.parent if app.suffix.lower() == ".exe" else app


def app_exe(app):
    """The program to reopen."""
    return app_folder(app) / f"{APP_NAME}.exe"


def where_the_installer_installs(app, programs):
    """Whether the app is in `programs`' folder for it, `I Need A Budget`, the one the installer
    always installs into (it doesn't ask where): an update installed anywhere else wouldn't
    replace this copy. Windows compares paths without case."""
    return app_folder(app).resolve() == (Path(programs) / APP_NAME).resolve()


# ── installing once the app has quit ────────────────────────

# PowerShell, given on its command line: no double quotes in it, so Windows' quoting leaves it be,
# and nothing that varies, which comes in INAB_HELPER_* (helper_environment). Run by powershell.exe
# with -NoProfile -NonInteractive -Command, no window. It waits for the app to exit (a minute at
# most: the installer can't replace a running app's files), runs the installer quietly and notes in
# the app's log how that went, reopens the app when Restart now left the `relaunch` file in its
# folder, and deletes its folder. An app still running after that minute gets neither: no installer,
# and no second copy (the helper only waits on, ten minutes at most, to delete its folder). The app it opens has the same environment: INAB_DATA_DIR stays. A helper with
# no installer (Restart now without an update) only reopens the app, and notes nothing.
HELPER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Note([string]$level, [string]$text) {
  if ($env:INAB_HELPER_LOG) {
    $line = '{0}  {1,-8}  {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss,fff'), $level, $text
    $bytes = [Text.Encoding]::UTF8.GetBytes($line + [Environment]::NewLine)
    try {  # shared, as an app that hasn't exited still has the log open
      $log = [IO.File]::Open($env:INAB_HELPER_LOG, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
      try { $log.Write($bytes, 0, $bytes.Length) } finally { $log.Dispose() }
    } catch {}
  }
}
$folder = $env:INAB_HELPER_FOLDER
$exe = $env:INAB_HELPER_APP
$installer = $env:INAB_HELPER_INSTALLER
$version = $env:INAB_HELPER_VERSION
$patience = [int]$env:INAB_HELPER_PATIENCE
$gone = $false
try {
  $app = Get-Process -Id ([int]$env:INAB_HELPER_WAIT_FOR) -ErrorAction SilentlyContinue
  $gone = (-not $app) -or $app.WaitForExit($patience * 1000)
  if (-not $gone) {
    if ($installer) { Note 'ERROR' ('Couldn''t install {0}: the app was still running {1} seconds after quitting' -f $version, $patience) }
    $null = $app.WaitForExit($patience * 10000)  # only to delete the folder: the app holds what's in it until then
  } elseif ($installer) {
    $run = Start-Process -FilePath $installer -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -PassThru
    $null = $run.Handle
    $run.WaitForExit()
    if ($run.ExitCode -eq 0) { Note 'INFO' ('Installed {0}' -f $version) }
    else { Note 'ERROR' ('Couldn''t install {0}: its installer failed (exit code {1})' -f $version, $run.ExitCode) }
  }
} catch {
  if ($installer) { Note 'ERROR' ('Couldn''t install {0}: {1}' -f $version, $_.Exception.Message) }
}
try {
  if ($gone -and $folder -and (Test-Path -LiteralPath (Join-Path $folder 'relaunch'))) {
    Get-ChildItem Env:INAB_HELPER_* | ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
    Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe)
  }
} catch {
  Note 'ERROR' ('Couldn''t reopen the app: ' + $_.Exception.Message)
} finally {
  if ($folder) { Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue }
}
"""
PATIENCE = 60  # seconds the helper waits for the app to exit


def helper_environment(environment, *, wait_for, folder, app, installer=None, version=None, log_file=None):
    """The helper's environment: this process's (which the installer and the reopened app keep),
    and what HELPER_SCRIPT needs to know. PYINSTALLER_RESET_ENVIRONMENT makes the reopened app
    start afresh rather than as a child of this one's launcher."""
    values = {key: value for key, value in environment.items() if not key.upper().startswith("INAB_HELPER_")}
    values.update(INAB_HELPER_WAIT_FOR=str(wait_for), INAB_HELPER_FOLDER=str(folder), INAB_HELPER_PATIENCE=str(PATIENCE),
                  INAB_HELPER_APP=str(app_exe(app)), PYINSTALLER_RESET_ENVIRONMENT="1")
    if installer is not None:
        values.update(INAB_HELPER_INSTALLER=str(installer), INAB_HELPER_VERSION=str(version))
    if log_file:
        values["INAB_HELPER_LOG"] = str(log_file)
    return values


def log_file():
    """Where the app's log is (main.py sets it up), for the helper's notes; None without one."""
    import logging

    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            return handler.baseFilename
    return None
