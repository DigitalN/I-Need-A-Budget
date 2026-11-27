#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Signs the Mac app with the Apple Development certificate and checks it the way installed copies
# check an update (computer_mac.verify): the same team, XT4LT7MS6Y, and a certificate with 30 or
# more days left. The release run (.github/workflows/release.yml) runs it, in the one job that can
# reach the release secrets (docs/DEVELOPMENT.md → Release secrets).
#
#   MAC_CERTIFICATE=… MAC_CERTIFICATE_PASSWORD=… scripts/sign-mac.sh APP    the release's certificate
#   scripts/sign-mac.sh --throwaway FOLDER APP     a dry run's: a certificate made here and forgotten, whose
#                                                  SHA-1 goes in FOLDER/test-identity (it has no team)
#   scripts/sign-mac.sh --clean                    deletes the temporary keychain (the run's always() step)
#
# The .p12 goes into a temporary keychain with a password made up here (not a secret), then is
# deleted; the keychain is deleted on the way out. Nothing is printed that names the certificate's
# owner: only its team, its fingerprint and when it expires. Signs as scripts/build.sh does
# (codesign --force --deep), so installed copies see what they always have.

set -euo pipefail
cd "$(dirname "$0")/.."

TEAM="XT4LT7MS6Y"
BUNDLE_ID="com.digitaln.ineedabudget"
DAYS_LEFT=30
PYTHON="${PYTHON:-python3}"
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/inab-signing"
KEYCHAIN="$WORK/signing.keychain-db"
SEARCH_LIST="$WORK/search-list"   # the keychains codesign looked in before, to put back

fail() { echo "✗ $*" >&2; exit 1; }
clean() {
    if [[ -f "$SEARCH_LIST" ]]; then
        # shellcheck disable=SC2046  # one keychain per line, as security printed them
        security list-keychains -d user -s $(tr -d '"' < "$SEARCH_LIST") >/dev/null 2>&1 || true
    fi
    [[ -e "$KEYCHAIN" ]] && security delete-keychain "$KEYCHAIN" >/dev/null 2>&1
    rm -rf "$WORK"
}

if [[ "${1:-}" == "--clean" ]]; then clean; exit 0; fi
THROWAWAY=""
if [[ "${1:-}" == "--throwaway" ]]; then THROWAWAY="${2:?Say which folder gets the throwaway identity}"; shift 2; fi
APP="${1:?Say which app: scripts/sign-mac.sh APP}"
[[ -d "$APP/Contents" ]] || fail "There's no app at $APP."

clean
trap clean EXIT
(umask 077 && mkdir -p "$WORK")
P12="$WORK/identity.p12"
P12_PASSWORD=""

if [[ -n "$THROWAWAY" ]]; then
    echo "Signing with a throwaway identity (a dry run): installed copies wouldn't take this app." >&2
    # LibreSSL, macOS's own, writes a .p12 that `security import` reads (OpenSSL 3's needs -legacy).
    cat > "$WORK/openssl.cnf" <<'CNF'
[req]
distinguished_name = subject
prompt = no
x509_extensions = signing
[subject]
CN = I Need A Budget dry run (throwaway)
OU = DRYRUN0000
[signing]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
CNF
    /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 90 -config "$WORK/openssl.cnf" \
        -keyout "$WORK/key.pem" -out "$WORK/certificate.pem" 2>/dev/null || fail "Couldn't make the throwaway certificate."
    P12_PASSWORD=$(/usr/bin/openssl rand -hex 16)
    /usr/bin/openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/certificate.pem" -out "$P12" \
        -passout "pass:$P12_PASSWORD" || fail "Couldn't make the throwaway .p12."
    rm -f "$WORK/key.pem"
else
    [[ -n "${MAC_CERTIFICATE:-}" ]] || fail "MAC_CERTIFICATE isn't set: it's in the release environment (scripts/release-secrets.sh certificate)."
    [[ -n "${MAC_CERTIFICATE_PASSWORD:-}" ]] || fail "MAC_CERTIFICATE_PASSWORD isn't set (scripts/release-secrets.sh certificate)."
    printf '%s' "$MAC_CERTIFICATE" | base64 --decode > "$P12" 2>/dev/null || fail "MAC_CERTIFICATE isn't base64."
    P12_PASSWORD="$MAC_CERTIFICATE_PASSWORD"
fi

KEYCHAIN_PASSWORD=$(/usr/bin/openssl rand -hex 24)  # this keychain's own, made up here: not a secret
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings -lut 3600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
# -x: the private key can't be exported from the keychain; -T: only codesign may use it.
security import "$P12" -k "$KEYCHAIN" -f pkcs12 -P "$P12_PASSWORD" -x -T /usr/bin/codesign >/dev/null 2>&1 ||
    fail "The keychain wouldn't take the certificate: is MAC_CERTIFICATE_PASSWORD the .p12's password?"
rm -f "$P12"
P12_PASSWORD=""
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null 2>&1
security list-keychains -d user > "$SEARCH_LIST"
# shellcheck disable=SC2046
security list-keychains -d user -s "$KEYCHAIN" $(tr -d '"' < "$SEARCH_LIST")

# Its identity, by fingerprint. A real one must be the only valid Apple Development identity in it;
# a throwaway one isn't trusted by anyone, so it's never "valid".
if [[ -n "$THROWAWAY" ]]; then
    IDENTITIES=$(security find-identity -p codesigning "$KEYCHAIN" | awk '/^ *[0-9]+\) [0-9A-F]+ / && length($2) == 40 {print $2}' | sort -u)
else
    IDENTITIES=$(security find-identity -v -p codesigning "$KEYCHAIN" | awk '/^ *[0-9]+\) [0-9A-F]+ "Apple Development: / && length($2) == 40 {print $2}' | sort -u)
fi
[[ -n "$IDENTITIES" ]] || fail "The certificate isn't a valid Apple Development identity (expired, or no private key in the .p12)."
[[ "$(wc -l <<< "$IDENTITIES" | tr -d ' ')" == 1 ]] || fail "The .p12 has more than one signing identity: export just the one."
IDENTITY="$IDENTITIES"

# Its certificate, to check the team and the dates without printing who it belongs to.
CERTIFICATE=$(security find-certificate -a -Z -p "$KEYCHAIN" |
    awk -v want="$IDENTITY" '/^SHA-1 hash:/ {hash=$3} /BEGIN CERT/ {if (hash == want) keep=1} keep {print} /END CERT/ {if (keep) exit}')
[[ -n "$CERTIFICATE" ]] || fail "Couldn't read the certificate back from the keychain."
if [[ -z "$THROWAWAY" ]]; then
    /usr/bin/openssl x509 -noout -subject <<< "$CERTIFICATE" | grep -Eq "OU *= *$TEAM(/|,|$)" ||
        fail "The certificate isn't team $TEAM's: installed copies refuse updates from any other team."
fi
EXPIRES=$(/usr/bin/openssl x509 -noout -enddate <<< "$CERTIFICATE" | sed 's/^notAfter=//')
/usr/bin/openssl x509 -noout -checkend $((DAYS_LEFT * 86400)) <<< "$CERTIFICATE" >/dev/null ||
    fail "The certificate expires within $DAYS_LEFT days ($EXPIRES): renew it (docs/DEVELOPMENT.md → Release secrets)."
echo "Certificate $IDENTITY, valid until $EXPIRES." >&2

# Sign as scripts/build.sh does.
xattr -cr "$APP"
if ! codesign --force --deep --keychain "$KEYCHAIN" -s "$IDENTITY" "$APP" > "$WORK/codesign.log" 2>&1; then
    grep -v ': replacing existing signature$' "$WORK/codesign.log" >&2 || true
    fail "codesign couldn't sign the app."
fi
security delete-keychain "$KEYCHAIN"   # done with it: nothing after this can sign

# What installed copies check, with their own requirement (computer_mac.signing_requirement): the
# whole app intact, this bundle identifier, Apple's chain and this team. A throwaway identity has no
# team or Apple chain: its own certificate stands in for them.
if [[ -n "$THROWAWAY" ]]; then
    REQUIREMENT="=identifier \"$BUNDLE_ID\" and certificate leaf = H\"$IDENTITY\""
    mkdir -p "$THROWAWAY"
    echo "$IDENTITY" > "$THROWAWAY/test-identity"
else
    REQUIREMENT=$("$PYTHON" -c 'import sys; sys.path.insert(0, "Application Files"); import computer_mac; print(computer_mac.signing_requirement(sys.argv[1], sys.argv[2]))' "$BUNDLE_ID" "$TEAM")
fi
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")" == "$BUNDLE_ID" ]] ||
    fail "The app's bundle identifier isn't $BUNDLE_ID."
if ! codesign --verify --deep --strict -R "$REQUIREMENT" "$APP" > "$WORK/verify.log" 2>&1; then
    cat "$WORK/verify.log" >&2
    fail "The signed app doesn't pass the check installed copies make."
fi
if [[ -z "$THROWAWAY" ]]; then
    SIGNED_TEAM=$(codesign -d --verbose=2 "$APP" 2>&1 | sed -n 's/^TeamIdentifier=//p')
    [[ "$SIGNED_TEAM" == "$TEAM" ]] || fail "codesign says the app's team is ${SIGNED_TEAM:-none}, not $TEAM."
    echo "Signed by team $TEAM: it passes the check installed copies make." >&2
else
    echo "Signed with the throwaway identity: it passes the check installed copies make, with that identity standing in for team $TEAM." >&2
fi
