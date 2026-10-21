#!/bin/bash
# Builds I Need A Budget and packages it as a disk image for a GitHub release:
#   ~/Library/Caches/I Need A Budget-dmg/I-Need-A-Budget-<version>.dmg
# When opened, it shows the app and the Applications folder on a background with an
# arrow between them. Attach it to a release tagged v<version>; installed copies update
# themselves from it too.
#
# Finder lays out the window, so the first run asks to let Terminal control Finder.

set -euo pipefail
cd "$(dirname "$0")/.."

VOLUME_NAME="I Need A Budget"

APP=$(scripts/build.sh)
# Installed copies only take updates signed by their own team, so a release must be too.
TEAM=$(codesign -d --verbose=2 "$APP" 2>&1 | sed -n 's/^TeamIdentifier=//p')
if [[ -z "$TEAM" || "$TEAM" == "not set" ]]; then
    echo "Releases must be signed with an Apple Development certificate (see docs/DEVELOPMENT.md)." >&2
    exit 1
fi
VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
ICON=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIconFile" "$APP/Contents/Info.plist")
# Only the newest is kept, outside the repo: the others are on their GitHub releases.
DMG="$HOME/Library/Caches/I Need A Budget-dmg/I-Need-A-Budget-$VERSION.dmg"

# Work outside the repo, which may be synced by iCloud Drive (see build.sh).
WORK="$HOME/Library/Caches/I Need A Budget-build/dmg"
rm -rf "$WORK"
mkdir -p "$WORK/source/.background"
ditto "$APP" "$WORK/source/I Need A Budget.app"
ln -s /Applications "$WORK/source/Applications"
echo "Drawing the window background…" >&2
swift scripts/make-dmg-background.swift "$WORK/source/.background/background.tiff"

echo "Laying out the window…" >&2
SIZE=$(( $(du -sm "$WORK/source" | cut -f1) + 20 ))m
hdiutil create -quiet -volname "$VOLUME_NAME" -srcfolder "$WORK/source" -fs HFS+ \
    -format UDRW -size "$SIZE" -ov "$WORK/layout.dmg"
MOUNT=$(hdiutil attach -readwrite -noverify -noautoopen "$WORK/layout.dmg" | grep -o '/Volumes/.*$')
DISK=$(basename "$MOUNT")
detach() { hdiutil detach -quiet "$MOUNT" 2>/dev/null || hdiutil detach -quiet -force "$MOUNT" 2>/dev/null || true; }
trap detach EXIT

# Icon positions match the arrow in make-dmg-background.swift: a 660 × 420 pt window
# with icon centers at x = 170 and x = 490.
if ! osascript <<APPLESCRIPT
tell application "Finder"
    tell disk "$DISK"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, 860, 568}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 112
        set text size of viewOptions to 13
        set label position of viewOptions to bottom
        set shows item info of viewOptions to false
        set shows icon preview of viewOptions to false
        set background picture of viewOptions to file ".background:background.tiff"
        set position of item "I Need A Budget.app" of container window to {170, 205}
        set position of item "Applications" of container window to {490, 205}
        close
        open
        update without registering applications
        delay 2
        close
    end tell
end tell
APPLESCRIPT
then
    echo "Finder couldn't lay out the window. If macOS asked, allow Terminal to control Finder" >&2
    echo "(System Settings → Privacy & Security → Automation), then run this again." >&2
    exit 1
fi

# Wait for Finder to save the layout before detaching.
for _ in $(seq 1 20); do
    [[ -f "$MOUNT/.DS_Store" ]] && break
    sleep 0.25
done

# Show the app icon on the mounted disk. Added after the layout step because
# Finder removes .VolumeIcon.icns while it arranges the window.
cp "$APP/Contents/Resources/$ICON" "$MOUNT/.VolumeIcon.icns"
xcrun SetFile -a C "$MOUNT"
rm -rf "$MOUNT/.fseventsd"
sync
detach
trap - EXIT

rm -rf "$(dirname "$DMG")"
mkdir -p "$(dirname "$DMG")"
hdiutil convert -quiet "$WORK/layout.dmg" -format UDZO -imagekey zlib-level=9 -o "$DMG"
rm -rf "$(dirname "$WORK")"

echo "$DMG"
