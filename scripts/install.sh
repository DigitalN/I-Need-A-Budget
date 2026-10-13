#!/bin/bash
# Builds I Need A Budget and installs it in /Applications.
#
#   scripts/install.sh            sign with your Apple Development certificate, if you have one
#   scripts/install.sh --adhoc    sign locally only (the app then can't update itself)
#   scripts/install.sh --open     open it afterwards
#
# If it's running, it's asked to quit first (it asks before quitting, so save nothing
# half-typed). Your data in ~/Library/Application Support/I Need A Budget is untouched.

set -euo pipefail
cd "$(dirname "$0")/.."

DEST="/Applications/I Need A Budget.app"
OPEN=0
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--open" ]]; then OPEN=1; else ARGS+=("$arg"); fi
done

APP=$(scripts/build.sh "${ARGS[@]+"${ARGS[@]}"}")

if pgrep -x "I Need A Budget" >/dev/null 2>&1; then
    echo "Quitting the running copy…" >&2
    osascript -e 'tell application "I Need A Budget" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do pgrep -x "I Need A Budget" >/dev/null 2>&1 || break; sleep 0.2; done
    if pgrep -x "I Need A Budget" >/dev/null 2>&1; then
        echo "It's still running. Quit it, then run this again." >&2
        exit 1
    fi
fi

echo "Installing to $DEST…" >&2
rm -rf "$DEST"
ditto "$APP" "$DEST"
rm -rf "$(dirname "$APP")"

if [[ $OPEN -eq 1 ]]; then
    open "$DEST"
    echo "Done. I Need A Budget is open." >&2
else
    echo "Done. Open I Need A Budget from your Applications folder." >&2
fi
