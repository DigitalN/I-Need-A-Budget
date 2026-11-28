#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Builds the Windows installer, I-Need-A-Budget-<version>-Setup.exe, on a Windows PC (in Git for
# Windows' bash), and prints its path. Needs the pinned packages (Application Files/requirements.txt)
# and Inno Setup 6.3 or later. GitHub's Windows machines run it for every pull request (.github/workflows/tests.yml).
#
#   scripts/build-windows.sh [folder for the installer]
#
# Unsigned (docs/adr/0009). The build happens outside the repository, which OneDrive may sync:
# in %LOCALAPPDATA%\I Need A Budget-build, or $INAB_BUILD_DIR.

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"
WORK="${INAB_BUILD_DIR:-${LOCALAPPDATA:?}/I Need A Budget-build}"
OUT="${1:-$WORK/installer}"
LOG="$WORK/build.log"
# PyInstaller's binary cache, this build's own (as scripts/build.sh does on a Mac).
export PYINSTALLER_CONFIG_DIR="$WORK/pyinstaller"

ISCC=$(command -v iscc || command -v ISCC || true)
for candidate in "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" "${LOCALAPPDATA:-}/Programs/Inno Setup 6/ISCC.exe"; do
    [[ -z "$ISCC" && -x "$candidate" ]] && ISCC="$candidate"
done
if [[ -z "$ISCC" ]]; then
    echo "Inno Setup 6.3 or later isn't installed (https://jrsoftware.org/isinfo.php): it makes the installer." >&2
    exit 1
fi

rm -rf "$WORK/build" "$WORK/dist"
mkdir -p "$WORK" "$OUT"
VERSION=$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "Application Files/main.py")
echo "Building $VERSION…" >&2
if ! "$PYTHON" -m PyInstaller "Application Files/INeedABudget.spec" --noconfirm --clean \
    --distpath "$WORK/dist" --workpath "$WORK/build" >"$LOG" 2>&1; then
    tail -20 "$LOG" >&2
    echo "Build failed; full log: $LOG" >&2
    exit 1
fi

# Inno Setup's switches start with "/", which Git's bash would otherwise take for paths.
if ! MSYS2_ARG_CONV_EXCL='*' "$ISCC" /Q "/DVersion=$VERSION" "/DDist=$(cygpath -w "$WORK/dist/I Need A Budget")" \
    "/DOutput=$(cygpath -w "$OUT")" "$(cygpath -w scripts/installer.iss)" >>"$LOG" 2>&1; then
    tail -20 "$LOG" >&2
    echo "Making the installer failed; full log: $LOG" >&2
    exit 1
fi

echo "$OUT/I-Need-A-Budget-$VERSION-Setup.exe"
