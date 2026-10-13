#!/bin/bash
# Builds "I Need A Budget.app" and prints its path. Used by install.sh and package.sh.
#
#   scripts/build.sh            sign with your Apple Development certificate, if you have one
#   scripts/build.sh --adhoc    sign locally only (the app then can't update itself)
#
# Installed copies only accept updates signed by the same developer team, so releases
# must be signed with a certificate from the same Apple account every time.

set -euo pipefail
cd "$(dirname "$0")/.."

# Build outside the repo: folders synced by iCloud Drive (like ~/Documents) add
# Finder metadata to app bundles, which codesign rejects.
WORK="$HOME/Library/Caches/I Need A Budget-build"
APP="$WORK/dist/I Need A Budget.app"
LOG="$WORK/build.log"

if [[ "${1:-}" == "--adhoc" ]]; then
    IDENTITY="-"
    echo "Signing locally (ad hoc); this copy won't update itself." >&2
else
    # The first *valid* Apple Development identity (expired certificates stay in the keychain).
    IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | awk '/"Apple Development/ {print $2; exit}')
    if [[ -z "$IDENTITY" ]]; then
        cat >&2 <<'MSG'
No Apple Development certificate found, so signing locally (ad hoc): this copy works
but won't update itself. A free Apple ID is enough to get one: Xcode → Settings →
Apple Accounts → your "(Personal Team)" → Manage Certificates… → + → Apple Development.
MSG
        IDENTITY="-"
    fi
fi

rm -rf "$WORK"
mkdir -p "$WORK"
echo "Building…" >&2
if ! python3 -m PyInstaller "Application Files/INeedABudget.spec" --noconfirm --clean \
    --distpath "$WORK/dist" --workpath "$WORK/build" >"$LOG" 2>&1; then
    tail -20 "$LOG" >&2
    echo "Build failed; full log: $LOG" >&2
    exit 1
fi

xattr -cr "$APP"
if ! codesign --force --deep -s "$IDENTITY" "$APP" >>"$LOG" 2>&1 \
    || ! codesign --verify --deep --strict "$APP" >>"$LOG" 2>&1; then
    tail -5 "$LOG" >&2
    echo "Signing failed; full log: $LOG" >&2
    exit 1
fi
TEAM=$(codesign -d --verbose=2 "$APP" 2>&1 | sed -n 's/^TeamIdentifier=//p')
echo "Signed (team: $TEAM)." >&2

echo "$APP"
