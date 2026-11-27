#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Would the installed copy take this disk image as an update?

Runs the installed version's own updater code (taken from its git tag, v<version>)
against the image, exactly as it would after downloading it: the app inside must have
the installed copy's bundle identifier, pass `codesign --verify --deep --strict` signed by
the same team, and be a newer version. From 1.5.0 that's computer_mac's `why_updates_off` and
`take_update`, so a copy that wouldn't update at all (outside Applications) says so too.
Prints why not, if not.

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
    """The updater as `version` has it, from the tag if there is one, else this checkout's, and the
    module with its Mac steps: updater.py itself before 1.5.0, computer_mac.py since."""
    tag = f"v{version}"
    listed = subprocess.run(["git", "-C", str(REPO), "ls-tree", "--name-only", f"{tag}:Application Files"],
                            capture_output=True, text=True)
    if listed.returncode != 0:
        print(f"No tag {tag}: checking with this checkout's updater instead.")
        folder = REPO / "Application Files"
    for name in listed.stdout.splitlines() if listed.returncode == 0 else []:
        if name.endswith(".py"):
            shown = subprocess.run(["git", "-C", str(REPO), "show", f"{tag}:Application Files/{name}"],
                                   capture_output=True, encoding="utf-8", check=True)
            (folder / name).write_text(shown.stdout, encoding="utf-8")
    sys.path.insert(0, str(folder))
    import updater
    if hasattr(updater, "own_team"):
        return updater, updater
    import computer_mac
    return updater, computer_mac


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
        updater, mac = load_updater(current, tmp / "code")
        team = mac.own_team(installed)
        bundle_id = mac.info_value(installed, "CFBundleIdentifier")
        if team is None:
            print(f"The installed copy ({current}) isn't signed by a developer team: it can't update itself.")
            return 1
        downloaded = tmp / "update.dmg"
        shutil.copy2(image, downloaded)  # where the updater would have downloaded it
        try:
            if hasattr(mac, "Mac"):  # 1.5.0 and later: the steps the app takes, but the download's folder
                this = mac.Mac()     # (folder_for_update needs PyObjC: the temporary folder stands in for it)
                off = this.why_updates_off(installed)
                if off:
                    print(f"The installed copy ({current}) doesn't update itself: {off}.")
                    return 1
                version, _ = this.take_update(downloaded, installed, tmp)
            else:
                app = tmp / installed.name
                mac.copy_app_from_image(downloaded, app, bundle_id)
                version = mac.verify(app, team, bundle_id)
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
