#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Would the installed copy take this release file as an update?

Runs the installed version's own updater code (taken from its git tag, v<version>) on the file,
exactly as it would after downloading it, so a release can't go out that the copies people have
would refuse. The release run (issue #190) runs it on both systems, against the previous release
installed on GitHub's machine.

    python3 scripts/check-update.py I-Need-A-Budget-1.5.0.dmg [installed app]
    python scripts/check-update.py I-Need-A-Budget-1.5.0-Setup.exe [installed folder]

  - The release's files are the ones in the file's folder: the installed version's parse_release
    must pick this one out of them (and on Windows its signature beside it), as it would from
    GitHub's answer.
  - A Mac's disk image: the app inside must have the installed copy's bundle identifier, pass
    `codesign --verify --deep --strict` signed by the same team, and be a newer version (from 1.5.0
    computer_mac's `why_updates_off` and `take_update`, so a copy that wouldn't update at all, outside
    Applications, says so too). The installed app defaults to /Applications/I Need A Budget.app.
  - A Windows installer: its signature (`….sig` beside it) must be the release key's, checked with
    the key built into the installed copy (`_internal/release-key.pub`), and the version in it newer
    (computer_windows' `why_updates_off` and `take_update`). The installed folder defaults to
    %LOCALAPPDATA%\\Programs\\I Need A Budget; its version is its program's.

A dry run signs with throwaway identities, which installed copies rightly refuse. For it:
    --test-identity SHA1   (Mac) that certificate stands in for Apple's chain and the team
    --test-key FILE        (Windows) that public half stands in for the installed copy's key
Everything else is checked as it is.

Read-only: the installed copy is only read, and everything this copies is deleted. Prints why
not, if not.
"""

import argparse
import inspect
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERE = REPO / "Application Files"
MAC_INSTALLED = Path("/Applications/I Need A Budget.app")


def windows_installed():
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "I Need A Budget"


def load_code(version, folder, needs):
    """The app's code as `version` has it, from the tag v<version> if there is one, else this
    checkout's: imported from `folder`, so nothing of this checkout's gets mixed in. `needs` is a
    module the code must have (the version's updater can't be checked without it)."""
    tag = f"v{version}"
    listed = subprocess.run(["git", "-C", str(REPO), "ls-tree", "--name-only", f"{tag}:Application Files"],
                            capture_output=True, text=True)
    if listed.returncode == 0:
        names = [n for n in listed.stdout.splitlines() if n.endswith(".py")]
        for name in names:
            shown = subprocess.run(["git", "-C", str(REPO), "show", f"{tag}:Application Files/{name}"],
                                   capture_output=True, check=True)
            (folder / name).write_bytes(shown.stdout)
        where = f"{version}'s own code, from {tag}"
    else:
        names = [p.name for p in HERE.glob("*.py")]
        for name in names:
            shutil.copy2(HERE / name, folder / name)
        where = f"this checkout's code (there's no tag {tag})"
    if f"{needs}.py" not in names:
        raise SystemExit(f"{version} has no {needs}.py: it can't take this update.")
    sys.path.insert(0, str(folder))
    print(f"Checking with {where}.")
    return where


def release_answer(file):
    """GitHub's answer for a release of the files in `file`'s folder, each "downloadable" from where
    it is, as the updater reads it."""
    version = re.search(r"-(\d+(?:\.\d+)*)(?:-setup)?\.(?:dmg|exe)$", file.name, re.IGNORECASE)
    tag = f"v{version.group(1)}" if version else "v0"
    assets = [{"name": p.name, "url": p.resolve().as_uri(), "browser_download_url": p.resolve().as_uri()}
              for p in sorted(file.parent.iterdir()) if p.is_file()]
    return {"tag_name": tag, "assets": assets}


def picked(updater, data, computer):
    """(url, signature url) the version's parse_release picks: 1.4.x's took only the answer (and
    gave the first .dmg); 1.5.0's takes this computer's file and signature."""
    if len(inspect.signature(updater.parse_release).parameters) == 1:
        version, url = updater.parse_release(data)
        return url, None
    _, url, signature = updater.parse_release(data, computer.update_file, computer.update_signature)
    return url, signature


def check_mac(file, installed, test_identity):
    if not installed.exists():
        print(f"There's no {installed} on this Mac to check against.")
        return 2
    with open(installed / "Contents" / "Info.plist", "rb") as f:
        current = plistlib.load(f)["CFBundleShortVersionString"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "code").mkdir()
        load_code(current, tmp / "code", "updater")
        import updater
        if hasattr(updater, "own_team"):  # before 1.5.0 the Mac's steps were updater.py's own
            mac = updater
        else:
            import computer_mac as mac
        if test_identity:  # the dry run's throwaway certificate in place of Apple's chain and the team
            mac.signing_requirement = lambda bundle_id, team: (
                f'=identifier "{bundle_id}" and certificate leaf = H"{test_identity}"')
        team = mac.own_team(installed)
        bundle_id = mac.info_value(installed, "CFBundleIdentifier")
        if team is None:
            print(f"The installed copy ({current}) isn't signed by a developer team: it can't update itself.")
            return 1
        try:
            url, _ = picked(updater, release_answer(file), getattr(mac, "Mac", object))
            if url != file.resolve().as_uri():
                print(f"The installed copy ({current}) would download {url.rsplit('/', 1)[-1]}, not {file.name}.")
                return 1
            downloaded = tmp / "update.dmg"
            shutil.copy2(file, downloaded)  # where the updater would have downloaded it
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
    signer = f"the dry run's throwaway identity standing in for team {team}" if test_identity else f"team {team}"
    print(f"OK: the installed {current} ({bundle_id}) would install {version}, signed by {signer}.")
    return 0


def program_version(program):
    """The version a Windows program says it is, read by this checkout's computer_windows in a
    process of its own, so none of this checkout's code mixes with the installed version's."""
    read = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import computer_windows;"
         " print(computer_windows.Windows()._installer_version(sys.argv[2]) or '')", str(HERE), str(program)],
        capture_output=True, text=True)
    return read.stdout.strip() or None


def check_windows(file, installed, test_key):
    program = installed / "I Need A Budget.exe"
    if not program.is_file():
        print(f"There's no {program} on this PC to check against.")
        return 2
    current = program_version(program)
    if not current:
        print(f"The installed {program.name} doesn't say which version it is.")
        return 1
    key = Path(test_key) if test_key else installed / "_internal" / "release-key.pub"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "code").mkdir()
        load_code(current, tmp / "code", "windows_update")
        # The key the installed copy checks with: the one built into it (read from beside its code,
        # as a copy run from source reads it), or the dry run's throwaway one in its place.
        if not key.is_file():
            print(f"The installed copy ({current}) has no release key ({key}): it can't update itself.")
            return 1
        shutil.copy2(key, tmp / "code" / "release-key.pub")
        import computer_windows
        import updater
        this = computer_windows.Windows()
        try:
            off = this.why_updates_off(installed)
            if off:
                print(f"The installed copy ({current}) doesn't update itself: {off}.")
                return 1
            url, signature = picked(updater, release_answer(file), this)
            if url != file.resolve().as_uri() or signature != Path(f"{file}.sig").resolve().as_uri():
                print(f"The installed copy ({current}) would download {url.rsplit('/', 1)[-1]}"
                      f" and {str(signature).rsplit('/', 1)[-1]}, not {file.name} and its signature.")
                return 1
            folder = tmp / "download"
            folder.mkdir()
            downloaded = folder / f"update{this.update_file[0]}"  # where the updater downloads them
            shutil.copy2(file, downloaded)
            shutil.copy2(f"{file}.sig", f"{downloaded}{this.update_signature}")
            version, _ = this.take_update(downloaded, installed, folder)
        except updater.UpdateError as e:
            print(f"The installed copy ({current}) would refuse it: {e}")
            return 1
        if not updater.is_newer(version, current):
            print(f"The installer holds {version}, which isn't newer than the installed {current}.")
            return 1
    signer = "the dry run's throwaway key, standing in for the release key" if test_key else "the release key"
    print(f"OK: the installed {current} would install {version}, signed with {signer}.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="the disk image or the installer (its .sig beside it)")
    parser.add_argument("installed", type=Path, nargs="?", help="the installed app (Mac) or folder (Windows)")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--test-identity", help="a dry run's: the throwaway certificate's SHA-1 (Mac)")
    which.add_argument("--test-key", help="a dry run's: the throwaway key's public half (Windows)")
    args = parser.parse_args()
    file = args.file.resolve()
    if not file.is_file():
        sys.exit(f"There's no {file}.")
    if file.name.lower().endswith(".dmg"):
        if args.test_key:
            sys.exit("--test-key is for a Windows installer.")
        return check_mac(file, args.installed or MAC_INSTALLED, args.test_identity)
    if file.name.lower().endswith("-setup.exe"):
        if args.test_identity:
            sys.exit("--test-identity is for a Mac's disk image.")
        return check_windows(file, args.installed or windows_installed(), args.test_key)
    sys.exit(f"{file.name} isn't a disk image (.dmg) or an installer (-Setup.exe).")


if __name__ == "__main__":
    sys.exit(main())
