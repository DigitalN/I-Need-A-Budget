#!/bin/bash
# Publishes a release of what's on main: every step of DEVELOPMENT.md's release checklist
# that a script can do, stopping at the first problem.
#
#   scripts/release.sh NOTES.md            check, build, check the build, publish
#   scripts/release.sh --dry-run [NOTES]   all of it except publishing (any branch)
#
# NOTES.md is the release notes, written OUTSIDE the repository, with no private details.
# The disk image's window is laid out by Finder, so a Finder window opens for a moment.

set -euo pipefail
cd "$(dirname "$0")/.."

REPO="DigitalN/I-Need-A-Budget"
TEAM="XT4LT7MS6Y"
DRY=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY=1; shift; fi
NOTES="${1:-}"

step() { echo "▸ $*" >&2; }
fail() { echo "✗ $*" >&2; exit 1; }
newer() { python3 -c "import sys; sys.path.insert(0, 'Application Files'); from updater import is_newer; sys.exit(0 if is_newer('$1', '$2') else 1)"; }

VERSION=$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "Application Files/main.py")
TAG="v$VERSION"
DMG="dist/I-Need-A-Budget-$VERSION.dmg"
[[ -n "$VERSION" ]] || fail "Couldn't read VERSION from Application Files/main.py."

step "Releasing $VERSION$([[ $DRY == 1 ]] && echo ' (dry run: nothing is published)')"
if [[ -n "$NOTES" ]]; then
    [[ -f "$NOTES" ]] || fail "No notes file at $NOTES."
    case "$(cd "$(dirname "$NOTES")" && pwd -P)/" in
        "$(pwd -P)/"*) fail "Write the release notes outside the repository." ;;
    esac
elif [[ $DRY == 0 ]]; then
    fail "Give the release notes file: scripts/release.sh NOTES.md"
fi

step "What's going out: main, the same as GitHub, nothing uncommitted"
git fetch -q origin
if [[ $DRY == 0 ]]; then
    [[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || fail "Not on main."
    [[ -z "$(git status --porcelain)" ]] || fail "There are uncommitted changes."
    [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "Local main isn't GitHub's main: git pull."
fi
COMMIT=$(git rev-parse HEAD)

step "Newer than the latest release"
LATEST=$(gh api "repos/$REPO/releases/latest" --jq .tag_name 2>/dev/null || true)
if [[ -n "$LATEST" ]]; then
    newer "$VERSION" "$LATEST" || fail "$VERSION isn't newer than the latest release ($LATEST): raise VERSION."
fi
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then fail "There's already a release $TAG."; fi

step "Tests"
python3 -m pytest -q >&2 || fail "Tests failed."

step "Signing certificate (team $TEAM) valid for at least 30 more days"
IDENTITY=$(security find-identity -v -p codesigning | awk '/"Apple Development/ {print $2; exit}')
[[ -n "$IDENTITY" ]] || fail "No valid Apple Development certificate in the keychain (see DEVELOPMENT.md)."
CERT=$(security find-certificate -a -c "Apple Development" -Z -p 2>/dev/null |
    awk -v want="$IDENTITY" '/^SHA-1 hash:/ {hash=$3} /BEGIN CERT/ {if (hash == want) keep=1} keep {print} /END CERT/ {if (keep) exit}')
echo "$CERT" | openssl x509 -noout -subject | grep -q "OU *= *$TEAM" || fail "The certificate isn't team $TEAM's."
echo "$CERT" | openssl x509 -noout -checkend $((30 * 86400)) >/dev/null ||
    fail "The certificate expires within 30 days ($(echo "$CERT" | openssl x509 -noout -enddate)). Renew it first."

step "Building the disk image"
rm -f "$DMG"
scripts/package.sh >&2
[[ -f "$DMG" ]] || fail "No $DMG after building."
hdiutil verify -quiet "$DMG" || fail "The disk image doesn't verify."

step "Would installed copies take it?"
if [[ -d "/Applications/I Need A Budget.app" ]]; then
    python3 scripts/check-update.py "$DMG" || fail "The installed copy would refuse this update."
else
    echo "  (no installed copy on this Mac to check against)" >&2
fi

if [[ $DRY == 1 ]]; then
    step "Dry run: everything checks out. Nothing was published."
    exit 0
fi

step "Publishing $TAG from $COMMIT"
gh release create "$TAG" "$DMG" --repo "$REPO" --target "$COMMIT" --title "I Need A Budget $VERSION" --notes-file "$NOTES"

step "What installed copies see"
SEEN=$(gh api "repos/$REPO/releases/latest" --jq '.tag_name + " " + ([.assets[].name] | join(","))')
[[ "$SEEN" == "$TAG $(basename "$DMG")" ]] || fail "GitHub's latest release reads \"$SEEN\", not \"$TAG $(basename "$DMG")\"."
if [[ -n "$LATEST" && "$LATEST" != "$TAG" ]]; then
    gh release edit "$LATEST" --repo "$REPO" --draft=true >/dev/null  # the page lists only the current one
fi

step "Done. Installed copies update within a day, or now with Settings → Check now (then quit)."
