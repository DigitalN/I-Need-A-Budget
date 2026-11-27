#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Signs a Windows installer with the release key and checks the signature the way Windows copies
# will (windows_update.py). The release run (.github/workflows/release.yml) runs it, in the one job
# that can reach the release secrets (docs/DEVELOPMENT.md → Release secrets).
#
#   RELEASE_KEY=… scripts/sign-windows.sh INSTALLER          with the release key, from the environment
#   scripts/sign-windows.sh --throwaway FOLDER INSTALLER     a dry run's: a key made here and forgotten,
#                                                            its public half saved as FOLDER/test-key.pub
#
# Writes INSTALLER.sig: Ed25519 (RFC 8032, not pre-hashed) over the installer's exact bytes, 64 bytes in
# base64 on one line. With the release key it first checks that the key's public half is
# Application Files/release-key.pub, the one built into the app: a release signed with any other key
# would be refused by every Windows copy.
#
# The key never reaches a command line or the log: it goes from the environment to a file only this
# user can read, in a folder of its own that's deleted on the way out (and by --clean, which the run's
# always() step calls in case this was stopped half-way). Needs OpenSSL 3 or later (Git for Windows has it;
# on a Mac, Homebrew's, which GitHub's Macs have: macOS's own is LibreSSL, which has no Ed25519).

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
KEYS="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/inab-release-key"
PUBLIC_HALF="Application Files/release-key.pub"

fail() { echo "✗ $*" >&2; exit 1; }
clean() { rm -rf "$KEYS"; }

if [[ "${1:-}" == "--clean" ]]; then clean; exit 0; fi
THROWAWAY=""
if [[ "${1:-}" == "--throwaway" ]]; then THROWAWAY="${2:?Say which folder gets the throwaway public half}"; shift 2; fi
INSTALLER="${1:?Say which installer: scripts/sign-windows.sh INSTALLER}"
[[ -f "$INSTALLER" ]] || fail "There's no installer at $INSTALLER."

OPENSSL=""
for candidate in openssl "$(brew --prefix openssl@3 2>/dev/null)/bin/openssl"; do
    if [[ "$("$candidate" version 2>/dev/null)" =~ ^OpenSSL\ ([3-9]|[1-9][0-9])\. ]]; then OPENSSL="$candidate"; break; fi
done
[[ -n "$OPENSSL" ]] || fail "OpenSSL 3 or later isn't here (on a Mac: brew install openssl@3)."

clean
trap clean EXIT
(umask 077 && mkdir -p "$KEYS")
KEY="$KEYS/key"

if [[ -n "$THROWAWAY" ]]; then
    echo "Signing with a throwaway key (a dry run): Windows copies wouldn't take this installer." >&2
    "$OPENSSL" genpkey -algorithm ed25519 -outform DER -out "$KEY"
    mkdir -p "$THROWAWAY"
    "$OPENSSL" pkey -inform DER -in "$KEY" -pubout -outform DER | tail -c 32 | "$OPENSSL" base64 -A > "$THROWAWAY/test-key.pub"
    echo >> "$THROWAWAY/test-key.pub"
    CHECK_WITH="$THROWAWAY/test-key.pub"
else
    [[ -n "${RELEASE_KEY:-}" ]] || fail "RELEASE_KEY isn't set: it's in the release environment (scripts/release-secrets.sh key)."
    [[ -f "$PUBLIC_HALF" ]] || fail "There's no $PUBLIC_HALF: Windows copies would have no key to check updates with."
    # The raw 32-byte seed in base64, as OpenSSL takes it: DER, with RFC 8410's prefix in front.
    { printf '\x30\x2e\x02\x01\x00\x30\x05\x06\x03\x2b\x65\x70\x04\x22\x04\x20'
      printf '%s' "$RELEASE_KEY" | "$OPENSSL" base64 -d -A; } > "$KEY"
    [[ "$(wc -c < "$KEY" | tr -d ' ')" == 48 ]] || fail "RELEASE_KEY isn't 32 bytes in base64 (docs/DEVELOPMENT.md → Release secrets)."
    if ! "$OPENSSL" pkey -inform DER -in "$KEY" -noout 2>/dev/null; then
        fail "OpenSSL can't read RELEASE_KEY as an Ed25519 key."
    fi
    public=$("$OPENSSL" pkey -inform DER -in "$KEY" -pubout -outform DER | tail -c 32 | "$OPENSSL" base64 -A)
    [[ "$public" == "$(tr -d '[:space:]' < "$PUBLIC_HALF")" ]] ||
        fail "RELEASE_KEY isn't the key whose public half is $PUBLIC_HALF: every Windows copy would refuse this release."
    echo "The release key matches $PUBLIC_HALF." >&2
    CHECK_WITH="$PUBLIC_HALF"
fi

SIGNATURE="$INSTALLER.sig"
"$OPENSSL" pkeyutl -sign -rawin -inkey "$KEY" -keyform DER -in "$INSTALLER" -out "$KEYS/signature"
"$OPENSSL" base64 -A -in "$KEYS/signature" > "$SIGNATURE"
echo >> "$SIGNATURE"
clean

# The check a Windows copy makes before running anything in a downloaded installer: our own verify,
# so OpenSSL's signature is checked by a second implementation of Ed25519.
"$PYTHON" - "$INSTALLER" "$CHECK_WITH" <<'PY' || fail "The signature doesn't pass the app's own check."
import sys
from pathlib import Path

sys.path.insert(0, "Application Files")
import windows_update
from updater import UpdateError

installer, public_half = Path(sys.argv[1]), Path(sys.argv[2])
key = windows_update.release_key(public_half)
if key is None:
    sys.exit(f"{public_half} isn't a key a signature proves anything for.")
try:
    windows_update.check(installer, key)
except UpdateError as e:
    sys.exit(str(e))
PY
echo "Signed $(basename "$INSTALLER"): $(basename "$SIGNATURE") passes the app's check with $(basename "$CHECK_WITH")." >&2
