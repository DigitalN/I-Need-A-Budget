#!/usr/bin/env python3
"""Would the installed copy take this disk image as an update?

Runs the installed version's own updater code (taken from its git tag, v<version>)
against the image, exactly as it would after downloading it: the app inside must have
the installed copy's bundle identifier, pass `codesign --verify --deep --strict` signed by
the same team, and be a newer version. Prints why not, if not.

    python3 scripts/check-update.py ~/Library/Caches/"I Need A Budget-dmg"/I-Need-A-Budget-1.0.2.dmg [installed app]

Read-only: the installed app is only read, and everything it copies is deleted.
"""

import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLED = Path("/Applications/I Need A Budget.app")


def load_updater(version, folder):
    """The updater as `version` has it: from the tag if there is one, else this checkout's."""
    tag = f"v{version}"
    for name in ("updater.py", "launch.py"):
        shown = subprocess.run(["git", "-C", str(REPO), "show", f"{tag}:Application Files/{name}"],
                               capture_output=True, text=True)
        if shown.returncode != 0:
            print(f"No tag {tag}: checking with this checkout's updater instead.")
            folder = REPO / "Application Files"
            break
        (folder / name).write_text(shown.stdout, encoding="utf-8")
    sys.path.insert(0, str(folder))
    import updater
    return updater


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    image = Path(sys.argv[1]).resolve()
    installed = Path(sys.argv[2]) if len(sys.argv) > 2 else INSTALLED
    if not installed.exists():
        print(f"There's no {installed} on this Mac to check against.")
        return 2
    with open(installed / "Contents" / "Info.plist", "rb") as f:
        current = plistlib.load(f)["CFBundleShortVersionString"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "code").mkdir()
        updater = load_updater(current, tmp / "code")
        team = updater.own_team(installed)
        bundle_id = updater.info_value(installed, "CFBundleIdentifier")
        if team is None:
            print(f"The installed copy ({current}) isn't signed by a developer team: it can't update itself.")
            return 1
        downloaded = tmp / "update.dmg"
        shutil.copy2(image, downloaded)  # where the updater would have downloaded it
        app = tmp / installed.name
        try:
            updater.copy_app_from_image(downloaded, app, bundle_id)
            version = updater.verify(app, team, bundle_id)
        except updater.UpdateError as e:
            print(f"The installed copy ({current}) would refuse it: {e}")
            return 1
        if not updater.is_newer(version, current):
            print(f"The image holds {version}, which isn't newer than the installed {current}.")
            return 1
    print(f"OK: the installed {current} (team {team}, {bundle_id}) would install {version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
