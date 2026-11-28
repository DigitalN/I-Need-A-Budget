#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Makes the release's disk image from a signed app, without Finder, and checks one.

    python3 scripts/make-dmg.py APP OUT.dmg                    make it (APP signed already: scripts/sign-mac.sh)
    python3 scripts/make-dmg.py --check OUT.dmg --version 1.5.0
                                [--test-identity SHA1]       what's in it, and its window, read back

Opened, it shows the window releases have always had (1.4.3's, which Finder laid out): the app and
Applications with the arrow between them on scripts/make-dmg-background.swift's background, no
toolbar, and the app's icon as the disk's. The check also reads the app in it: that version, and the
signature installed copies check (team XT4LT7MS6Y, or a dry run's throwaway identity in its place). dmgbuild writes the window's settings (.DS_Store) itself,
so nothing has to drive Finder, which GitHub's Macs can't (issue #190). It needs dmgbuild
(scripts/dmg-requirements.txt) and swift (Xcode's command-line tools) to draw the background.
"""

import argparse
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEAM = "XT4LT7MS6Y"
VOLUME = "I Need A Budget"
APP_NAME = "I Need A Budget.app"
# make-dmg-background.swift draws a 660 × 420 pt picture with the arrow between icon centres at x = 170
# and x = 490, y = 205. The window is that plus its title bar: Finder's bounds for 1.4.3's were 660 × 448.
WINDOW = ((200, 120), (660, 448))
POSITIONS = {APP_NAME: (170, 205), "Applications": (490, 205)}
ICON_SIZE, TEXT_SIZE = 112, 13


def settings(app, background):
    icon = app / "Contents" / "Resources" / info(app)["CFBundleIconFile"]
    if icon.suffix != ".icns":
        icon = icon.with_name(icon.name + ".icns")
    return {
        "format": "UDZO",           # what every release has been: any macOS opens it
        "filesystem": "HFS+",
        "files": [str(app)],
        "symlinks": {"Applications": "/Applications"},
        "icon": str(icon),           # the disk's own icon, .VolumeIcon.icns
        "background": str(background),
        "window_rect": WINDOW,
        "default_view": "icon-view",
        "show_status_bar": False, "show_tab_view": False, "show_toolbar": False,
        "show_pathbar": False, "show_sidebar": False,
        "arrange_by": None,
        "show_icon_preview": False, "show_item_info": False,
        "label_pos": "bottom",
        "icon_size": ICON_SIZE, "text_size": TEXT_SIZE,
        "icon_locations": POSITIONS,
    }


def info(app):
    with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
        return plistlib.load(f)


def make(app, out):
    import dmgbuild

    app, out = Path(app).resolve(), Path(out).resolve()
    if app.name != APP_NAME:
        sys.exit(f"The app must be called {APP_NAME}, as installed copies look for it.")
    with tempfile.TemporaryDirectory() as tmp:
        background = Path(tmp) / "background.tiff"  # one TIFF, 1x and 2x
        subprocess.run(["swift", str(REPO / "scripts" / "make-dmg-background.swift"), str(background)], check=True)
        out.unlink(missing_ok=True)
        # lookForHiDPI: the TIFF has both sizes in it already.
        dmgbuild.build_dmg(str(out), VOLUME, settings=settings(app, background), lookForHiDPI=False)
    print(out)


def attach(image):
    shown = subprocess.run(["hdiutil", "attach", str(image), "-nobrowse", "-readonly", "-noautoopen", "-plist"],
                           capture_output=True, check=True)
    entities = plistlib.loads(shown.stdout).get("system-entities", [])
    return Path(next(e["mount-point"] for e in entities if e.get("mount-point")))


def layout(mount):
    """The window's settings as the disk's .DS_Store has them, and each item's place."""
    from ds_store import DSStore

    found = {"places": {}}
    with DSStore.open(str(mount / ".DS_Store"), "r") as store:
        for entry in store:
            if entry.filename == "." and entry.code == b"bwsp":
                found["window"] = entry.value
            elif entry.filename == "." and entry.code == b"icvp":
                found["view"] = entry.value
            elif entry.code == b"Iloc":
                found["places"][entry.filename] = tuple(entry.value)
    return found


def requirement(bundle_id, test_identity=None):
    """What installed copies check an update's app against (computer_mac.signing_requirement), or
    with a dry run's throwaway certificate standing in for Apple's chain and the team."""
    if test_identity:
        return f'=identifier "{bundle_id}" and certificate leaf = H"{test_identity}"'
    sys.path.insert(0, str(REPO / "Application Files"))
    import computer_mac

    return computer_mac.signing_requirement(bundle_id, TEAM)


def problems(image, version, test_identity=None):
    """[what's wrong] with a disk image: the app in it, what else is in it, and the window it opens in."""
    found = []
    mount = attach(image)
    try:
        names = {p.name for p in mount.iterdir()}
        app = mount / APP_NAME
        if not (app / "Contents" / "Info.plist").is_file():
            return [f"There's no {APP_NAME} in it."]
        if info(app).get("CFBundleShortVersionString") != version:
            found.append(f"Its app is version {info(app).get('CFBundleShortVersionString')}, not {version}.")
        verified = subprocess.run(["codesign", "--verify", "--deep", "--strict", "-R",
                                   requirement(info(app).get("CFBundleIdentifier"), test_identity), str(app)],
                                  capture_output=True, text=True)
        if verified.returncode != 0:
            found.append(f"Its app doesn't pass the check installed copies make: {verified.stderr.strip()}")
        link = mount / "Applications"
        if not (link.is_symlink() and str(link.readlink()) == "/Applications"):
            found.append("Its Applications isn't a link to /Applications.")
        extra = {n for n in names if not n.startswith(".")} - {APP_NAME, "Applications"}  # Finder hides the rest
        if extra:
            found.append(f"It has more in it than the app and Applications: {', '.join(sorted(extra))}.")
        if ".VolumeIcon.icns" not in names:
            found.append("It has no icon of its own (.VolumeIcon.icns).")
        if ".background.tiff" not in names:
            found.append("Its window has no background.")
        flags = subprocess.run(["GetFileInfo", "-a", str(mount)], capture_output=True, text=True).stdout
        if "C" not in flags:  # the volume's custom-icon flag: without it Finder shows a plain disk
            found.append("Finder wouldn't show its icon (the volume's custom-icon flag isn't set).")
        shape = layout(mount)
        window, view = shape.get("window", {}), shape.get("view", {})
        size = window.get("WindowBounds", "").replace(" ", "").rsplit("{", 1)[-1].rstrip("}")
        want = {
            "the window's size": (size, f"{WINDOW[1][0]},{WINDOW[1][1]}"),
            "the toolbar": (window.get("ShowToolbar"), False),
            "the status bar": (window.get("ShowStatusBar"), False),
            "the sidebar": (window.get("ShowSidebar"), False),
            "the background": (view.get("backgroundType"), 2),   # a picture
            "the icon size": (view.get("iconSize"), float(ICON_SIZE)),
            "the text size": (view.get("textSize"), float(TEXT_SIZE)),
            "the names under the icons": (view.get("labelOnBottom"), True),
            "the arrangement": (view.get("arrangeBy"), "none"),
            "where the icons are": (shape["places"], POSITIONS),
        }
        found += [f"{what.capitalize()} is {have!r}, not {wanted!r}." for what, (have, wanted) in want.items()
                  if have != wanted]
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", metavar="IMAGE", type=Path, help="check a disk image instead of making one")
    parser.add_argument("--version", help="the version its app must be (with --check)")
    parser.add_argument("--test-identity", help="a dry run's throwaway certificate's SHA-1 (with --check)")
    parser.add_argument("app", nargs="?", type=Path)
    parser.add_argument("out", nargs="?", type=Path)
    args = parser.parse_args()
    if args.check:
        if not args.version or args.app:
            parser.error("--check IMAGE --version VERSION [--test-identity SHA1]")
        found = problems(args.check, args.version, args.test_identity)
        for why in found:
            print(f"✗ {why}", file=sys.stderr)
        if not found:
            signer = "a dry run's throwaway identity" if args.test_identity else f"team {TEAM}"
            print(f"OK: {APP_NAME} {args.version}, signed by {signer}, and Applications, in the window"
                  f" releases have always had.")
        return 1 if found else 0
    if not (args.app and args.out):
        parser.error("APP OUT.dmg, or --check IMAGE --version VERSION")
    if not shutil.which("swift"):
        sys.exit("swift isn't here: it draws the window's background (Xcode's command-line tools).")
    make(args.app, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
