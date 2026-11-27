#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Publishes a release the release run has built and checked (.github/workflows/release.yml, issue #190).

    python3 scripts/publish-release.py --version 1.5.0 --previous v1.4.3 --commit SHA --notes NOTES.md FOLDER

FOLDER holds exactly the release's three files: I-Need-A-Budget-<version>.dmg, …-Setup.exe and
…-Setup.exe.sig. Then:
  1. v<version> is published from the commit, with those files, unless an earlier try of this run got
     part of the way: a release of that tag at that commit is finished (a draft gets the files it's
     missing and is published), never made again. A tag at another commit, or a published release
     with other files, stops it: that's for a person to look at (docs/DEVELOPMENT.md → Release
     checklist → If publishing stops half-way).
  2. GitHub's latest release, the one installed copies see, must be that tag at that commit with
     exactly those files, byte for byte (each asset's sha256 digest). GitHub can take a moment, so it
     asks a few times. If it never is, the release is made a draft again: installed copies keep the
     previous one.
  3. The previous release becomes a draft, so the page lists only the current one.
Each step that fails says what's been done and what's left. Through `gh` (GH_TOKEN, GITHUB_REPOSITORY).
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "DigitalN/I-Need-A-Budget")
TRIES, WAIT = 6, 10   # GitHub's API can lag just after a release is made


class Stop(Exception):
    """Something a person has to look at: the message says what's done and what's left."""


def gh(*args):
    """`gh` with these arguments: (exit code, standard output, standard error). Tests put a stand-in here."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True)
    return done.returncode, done.stdout, done.stderr


def run(*args, check=True):
    code, out, err = gh(*args)
    if check and code != 0:
        raise Stop(f"gh {' '.join(args[:3])} failed: {err.strip()[:300]}")
    return code, out


def sleep(seconds):
    time.sleep(seconds)


def api(path):
    """GitHub's answer for `path`, or None when it says there's nothing there (404)."""
    code, out, err = gh("api", f"repos/{REPOSITORY}/{path}")
    if code != 0:
        if "HTTP 404" in err:
            return None
        raise Stop(f"GitHub didn't answer for {path}: {err.strip()[:300]}")
    return json.loads(out)


def tagged(tag):
    """The commit a tag points at, or None."""
    ref = api(f"git/ref/tags/{tag}")
    if ref is None:
        return None
    target = ref["object"]
    if target.get("type") == "tag":  # an annotated tag points at a tag object first
        target = api(f"git/tags/{target['sha']}")["object"]
    return target["sha"]


def release_of(tag):
    """The release with this tag, drafts included (the run's token can see them), or None."""
    for release in api("releases?per_page=100") or []:
        if release.get("tag_name") == tag:
            return release
    return None


def latest():
    """GitHub's latest release (not drafts or pre-releases), or None."""
    return api("releases/latest")


def digests(files):
    found = {}
    for path in files:
        with open(path, "rb") as f:
            found[path.name] = "sha256:" + hashlib.sha256(f.read()).hexdigest()
    return found


def differences(release, tag, commit, wanted):
    """[what's wrong] with a release that should be `tag` at `commit` with exactly `wanted` (name: digest)."""
    if release is None:
        return ["There's no latest release."]
    found = []
    if release.get("tag_name") != tag:
        found.append(f"The latest release is {release.get('tag_name')}, not {tag}.")
    assets = {a.get("name"): a.get("digest") for a in release.get("assets", [])}
    if sorted(assets) != sorted(wanted):
        found.append(f"It has {sorted(assets)}, not {sorted(wanted)}.")
    for name, digest in wanted.items():
        if name in assets and not assets[name]:
            found.append(f"GitHub doesn't say what {name} holds (no digest), so it can't be checked.")
        elif name in assets and assets[name] != digest:
            found.append(f"GitHub's {name} isn't the file this run checked.")
    at = tagged(tag)
    if at != commit:
        found.append(f"{tag} points at {at}, not {commit}.")
    return found


def publish(version, previous, commit, notes, folder):
    tag = f"v{version}"
    files = [folder / f"I-Need-A-Budget-{version}.dmg", folder / f"I-Need-A-Budget-{version}-Setup.exe",
             folder / f"I-Need-A-Budget-{version}-Setup.exe.sig"]
    have = sorted(p.name for p in folder.iterdir())
    if have != sorted(p.name for p in files):
        raise Stop(f"The release's files aren't the three it should have: {have}.")
    wanted = digests(files)

    # 1. Make it, or finish what an earlier try of this run made.
    at = tagged(tag)
    if at is not None and at != commit:
        raise Stop(f"There's already a tag {tag}, at {at}, not this run's commit {commit}. If it's a leftover,"
                   f" delete it (gh release delete {tag} --cleanup-tag, or gh api -X DELETE"
                   f" repos/{REPOSITORY}/git/refs/tags/{tag}); otherwise raise VERSION.")
    made = release_of(tag)
    if made is None:
        now = (latest() or {}).get("tag_name") or ""
        if now != previous:
            raise Stop(f"The latest release changed while this ran ({previous or 'none'}, now {now}): start again.")
        run("release", "create", tag, *map(str, files), "--repo", REPOSITORY, "--target", commit,
            "--title", f"I Need A Budget {version}", "--notes-file", str(notes), "--latest")
        print(f"Made {tag} from {commit[:7]}.")
    else:
        assets = {a.get("name"): a.get("digest") for a in made.get("assets", [])}
        if not made.get("draft"):
            if assets != wanted:
                raise Stop(f"{tag} is published already, with other files ({sorted(assets)}). Look at it on the"
                           f" Releases page: nothing was changed.")
            print(f"{tag} was published by an earlier try of this run.")
        else:
            if assets != wanted:  # an earlier try stopped while uploading: ours replace what's there
                run("release", "upload", tag, *map(str, files), "--repo", REPOSITORY, "--clobber")
                print(f"Gave the draft {tag} the run's files.")
            run("release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest")
            print(f"Published the draft {tag} an earlier try of this run left.")

    # 2. What installed copies see.
    found = []
    for attempt in range(TRIES):
        found = differences(latest(), tag, commit, wanted)
        if not found:
            break
        if attempt < TRIES - 1:
            sleep(WAIT)
    if found:
        run("release", "edit", tag, "--repo", REPOSITORY, "--draft=true", check=False)
        raise Stop(" ".join(found) + f" So {tag} is a draft again, and installed copies still see"
                   f" {previous or 'no release'}. Once it's fixed, re-run this job (gh run rerun --failed): it"
                   f" finishes the draft.")
    print(f"GitHub's latest release is {tag}, at {commit[:7]}, with exactly the three files this run checked.")

    # 3. The page lists only the current one.
    if previous and previous != tag:
        for attempt in range(TRIES):
            code, _ = run("release", "edit", previous, "--repo", REPOSITORY, "--draft=true", check=False)
            if code == 0:
                break
            if attempt < TRIES - 1:
                sleep(WAIT)
        else:
            raise Stop(f"{tag} is published, but {previous} couldn't be made a draft:"
                       f" gh release edit {previous} --draft=true")
        print(f"{previous} is a draft now.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--previous", default="", help="the latest release's tag when the run began")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    try:
        publish(args.version, args.previous, args.commit, args.notes, args.folder)
    except Stop as e:
        print(f"::error::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
