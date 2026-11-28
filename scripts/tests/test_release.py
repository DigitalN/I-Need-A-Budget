# SPDX-License-Identifier: GPL-3.0-only
"""The release run's own pieces (issue #190, .github/workflows/release.yml): what it checks a Mac app
for, how it signs a Windows installer, how check-update.py reads a release, and the guards that keep
the release secrets in one job. Throwaway keys and made-up files only."""

import base64
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_mac_app = script("check-mac-app")
check_update = script("check-update")


# ── check-mac-app.py: what a Mac app's programs and libraries need ──

ARM64, X86_64 = 0x0100000C, 0x01000007


def thin(cpu, macos=None, old_style=False):
    """A 64-bit Mach-O header with one load command saying the oldest macOS it runs on."""
    commands = b""
    if macos:
        encoded = (macos[0] << 16) | (macos[1] << 8)
        if old_style:
            commands = struct.pack("<IIII", check_mac_app.LC_VERSION_MIN_MACOSX, 16, encoded, encoded)
        else:
            commands = struct.pack("<IIIIII", check_mac_app.LC_BUILD_VERSION, 24, 1, encoded, encoded, 0)
    header = struct.pack("<4siIIIII", b"\xcf\xfa\xed\xfe", cpu, 0, 6, 1 if macos else 0, len(commands), 0)
    return header + b"\0\0\0\0" + commands + b"\0" * 64


def fat(*slices):
    """A universal file: a big-endian header listing each slice, then the slices."""
    offset, table, body = 4096, b"", b""
    for one in slices:
        cpu = struct.unpack_from("<i", one, 4)[0]
        table += struct.pack(">iiIII", cpu, 0, offset + len(body), len(one), 12)
        body += one
    head = struct.pack(">4sI", check_mac_app.FAT, len(slices)) + table
    return head + b"\0" * (offset - len(head)) + body


def an_app(folder, minimum="12.0", main=None, **libraries):
    app = folder / "I Need A Budget.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Frameworks").mkdir()
    (app / "Contents" / "Info.plist").write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        b"<key>CFBundleExecutable</key><string>I Need A Budget</string>"
        b"<key>LSMinimumSystemVersion</key><string>" + minimum.encode() + b"</string></dict></plist>")
    (app / "Contents" / "MacOS" / "I Need A Budget").write_bytes(main or thin(ARM64, (11, 0)))
    for name, data in libraries.items():
        (app / "Contents" / "Frameworks" / f"{name}.so").write_bytes(data)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "Resources" / "index.html").write_text("<p>not a program</p>")
    return app


def test_slices_are_read_from_thin_and_universal_files_both_ways_of_saying_the_version():
    assert check_mac_app.slices(thin(ARM64, (11, 0))) == [(ARM64, (11, 0))]
    assert check_mac_app.slices(thin(X86_64, (10, 13), old_style=True)) == [(X86_64, (10, 13))]
    assert check_mac_app.slices(thin(ARM64)) == [(ARM64, None)]
    assert check_mac_app.slices(fat(thin(X86_64, (10, 13)), thin(ARM64, (11, 0)))) == [
        (X86_64, (10, 13)), (ARM64, (11, 0))]
    java_class = b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 64  # the same four bytes, then a version
    assert check_mac_app.slices(java_class) == [] and check_mac_app.slices(b"<html>") == []


def test_an_app_whose_files_all_run_on_its_minimum_passes(tmp_path):
    app = an_app(tmp_path, python=fat(thin(X86_64, (10, 13)), thin(ARM64, (11, 0))),
                 old=thin(ARM64, (10, 15), old_style=True))
    assert check_mac_app.problems(app) == []
    assert "for arm64, x86_64: macOS 11.0 at the most" in check_mac_app.summary(app)


def test_a_library_needing_a_newer_macos_than_the_app_says_is_caught(tmp_path):
    app = an_app(tmp_path, rapidfuzz=thin(ARM64, (13, 0)))
    [why] = check_mac_app.problems(app)
    assert "rapidfuzz.so (arm64) needs macOS 13.0" in why and "runs on 12.0" in why


def test_a_library_missing_the_main_programs_processor_is_caught(tmp_path):
    app = an_app(tmp_path, main=fat(thin(X86_64, (11, 0)), thin(ARM64, (11, 0))), intel=thin(X86_64, (10, 13)))
    [why] = check_mac_app.problems(app)
    assert "intel.so isn't built for arm64" in why


def test_a_minimum_that_cant_be_read_doesnt_pass(tmp_path):
    for folder, minimum in (("a", ""), ("b", "12.x"), ("c", "twelve")):
        [why] = check_mac_app.problems(an_app(tmp_path / folder, minimum=minimum))
        assert "isn't a macOS version" in why
    [why] = check_mac_app.problems(an_app(tmp_path / "d", silent=thin(ARM64)))  # no LC_BUILD_VERSION
    assert "silent.so (arm64) doesn't say which macOS it needs" in why
    assert "isn't a Mac program" in check_mac_app.problems(an_app(tmp_path / "e", main=b"#!/bin/sh\n"))[0]


def test_an_update_must_run_on_every_mac_the_previous_release_did(tmp_path):
    both = fat(thin(X86_64, (11, 0)), thin(ARM64, (11, 0)))
    arm_only, older_arm = an_app(tmp_path / "new"), an_app(tmp_path / "old")   # 1.4.3's is arm64 only
    assert check_mac_app.problems(arm_only, previous=older_arm) == []
    universal = an_app(tmp_path / "universal", main=both)
    [why] = check_mac_app.problems(arm_only, previous=universal)
    assert "runs on x86_64 Macs, and this one isn't built for them" in why
    assert check_mac_app.problems(an_app(tmp_path / "new-universal", main=both, lib=both), previous=universal) == []
    assert check_mac_app.main([str(arm_only), "--previous", str(universal)]) == 1


# ── sign-windows.sh: the installer's signature ──

def git_bash():
    """Git for Windows' bash on a PC (not WSL's, which may come first on the PATH); bash elsewhere."""
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    for folder in Path(git).resolve().parents if git else []:
        for candidate in (folder / "bin" / "bash.exe", folder / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


BASH = git_bash()


def openssl_3(bash):
    """Whether sign-windows.sh would find OpenSSL 3 or later from this bash."""
    found = subprocess.run([bash, "-c", 'for o in openssl "$(brew --prefix openssl@3 2>/dev/null)/bin/openssl"; do '
                                        '"$o" version 2>/dev/null; done'], capture_output=True, text=True)
    return bool(re.search(r"^OpenSSL ([3-9]|[1-9][0-9])\.", found.stdout, re.MULTILINE))


SIGNS = pytest.mark.skipif(not (BASH and openssl_3(BASH)), reason="needs bash and OpenSSL 3 or later")


@pytest.fixture
def checkout(tmp_path):
    """sign-windows.sh in a copy of what it uses, with a release key of its own (a throwaway one)."""
    sys.path.insert(0, str(ROOT / "scripts" / "tests"))
    from test_windows_update import public_key_of, throwaway_key

    copy = tmp_path / "checkout"
    (copy / "scripts").mkdir(parents=True)
    (copy / "Application Files").mkdir()
    shutil.copy2(ROOT / "scripts" / "sign-windows.sh", copy / "scripts")
    for name in ("windows_update.py", "updater.py", "computer.py"):
        shutil.copy2(ROOT / "Application Files" / name, copy / "Application Files")
    seed = throwaway_key()
    (copy / "Application Files" / "release-key.pub").write_text(base64.b64encode(public_key_of(seed)).decode() + "\n")
    installer = tmp_path / "I-Need-A-Budget-9.9.9-Setup.exe"
    installer.write_bytes(b"MZ made-up installer " * 500)
    return copy, seed, installer


def sign(copy, installer, *options, key=None, temp):
    environment = dict(os.environ, PYTHON=sys.executable, RUNNER_TEMP=str(temp))
    environment.pop("RELEASE_KEY", None)
    if key is not None:
        environment["RELEASE_KEY"] = key
    return subprocess.run([BASH, str(copy / "scripts" / "sign-windows.sh"), *options, str(installer)],
                          capture_output=True, text=True, env=environment)


def passes(installer, public_half):
    import windows_update
    from updater import UpdateError

    try:
        windows_update.check(installer, windows_update.release_key(public_half))
        return True
    except UpdateError:
        return False


@SIGNS
def test_the_release_key_signs_so_the_app_accepts_it_and_leaves_no_key_behind(checkout, tmp_path):
    copy, seed, installer = checkout
    signed = sign(copy, installer, key=base64.b64encode(seed).decode(), temp=tmp_path / "temp")
    assert signed.returncode == 0, signed.stderr
    signature = installer.with_name(installer.name + ".sig").read_text()
    assert len(signature.split()) == 1 and len(base64.b64decode(signature)) == 64
    assert passes(installer, copy / "Application Files" / "release-key.pub")
    assert not (tmp_path / "temp" / "inab-release-key").exists()
    assert base64.b64encode(seed).decode() not in signed.stdout + signed.stderr


@SIGNS
@pytest.mark.parametrize("key, says", [
    ("", "RELEASE_KEY isn't set"),
    ("bm90IGEga2V5", "isn't 32 bytes"),
    (None, "isn't the key whose public half is"),  # another key: every Windows copy would refuse it
])
def test_a_release_key_that_isnt_the_apps_signs_nothing(checkout, tmp_path, key, says):
    sys.path.insert(0, str(ROOT / "scripts" / "tests"))
    from test_windows_update import throwaway_key

    copy, _, installer = checkout
    if key is None:
        key = base64.b64encode(throwaway_key()).decode()
    signed = sign(copy, installer, key=key, temp=tmp_path / "temp")
    assert signed.returncode == 1 and says in signed.stderr
    assert not installer.with_name(installer.name + ".sig").exists()
    assert not (tmp_path / "temp" / "inab-release-key").exists()


@SIGNS
def test_a_dry_run_signs_with_a_throwaway_key_the_app_would_refuse(checkout, tmp_path):
    copy, _, installer = checkout
    signed = sign(copy, installer, "--throwaway", str(tmp_path / "ids"), temp=tmp_path / "temp")
    assert signed.returncode == 0, signed.stderr
    assert passes(installer, tmp_path / "ids" / "test-key.pub")
    assert not passes(installer, copy / "Application Files" / "release-key.pub")
    with open(installer, "ab") as f:
        f.write(b"changed")
    assert not passes(installer, tmp_path / "ids" / "test-key.pub")


# ── check-update.py: the release as the installed version's updater reads it ──

def test_the_release_is_the_files_beside_the_one_checked(tmp_path):
    for name in ("I-Need-A-Budget-1.5.0.dmg", "I-Need-A-Budget-1.5.0-Setup.exe", "I-Need-A-Budget-1.5.0-Setup.exe.sig"):
        (tmp_path / name).write_bytes(b"made up")
    answer = check_update.release_answer(tmp_path / "I-Need-A-Budget-1.5.0-Setup.exe")
    assert answer["tag_name"] == "v1.5.0"
    assert [a["name"] for a in answer["assets"]] == sorted(p.name for p in tmp_path.iterdir())

    import computer_mac
    import computer_windows
    import updater

    dmg, setup = tmp_path / "I-Need-A-Budget-1.5.0.dmg", tmp_path / "I-Need-A-Budget-1.5.0-Setup.exe"
    assert check_update.picked(updater, answer, computer_mac.Mac) == (dmg.as_uri(), None)
    assert check_update.picked(updater, answer, computer_windows.Windows) == (
        setup.as_uri(), Path(f"{setup}.sig").as_uri())

    class Old:  # 1.4.x: parse_release(data), the first .dmg
        UpdateError = updater.UpdateError

        @staticmethod
        def parse_release(data):
            return "1.5.0", next(a["url"] for a in data["assets"] if a["name"].endswith(".dmg"))

    assert check_update.picked(Old, answer, computer_mac.Mac) == (dmg.as_uri(), None)


# ── publish-release.py: publishing, and finishing a publish that stopped half-way ──

publish_release = script("publish-release")
OURS, OTHER = "a" * 40, "b" * 40


class FakeGitHub:
    """The part of GitHub publish-release.py talks to through gh: releases (drafts too), tags,
    which release is the latest, and each asset's digest."""

    def __init__(self, previous="v1.4.3"):
        self.releases, self.tags, self.calls = [], {}, []
        self.lag = 0              # how many reads of the latest release still see the old one
        self.digests = True       # whether GitHub says what each asset holds
        self.fail = set()         # gh commands that fail ("upload", "draft-previous")
        if previous:
            self.add(previous, OTHER, {"I-Need-A-Budget-1.4.3.dmg": "sha256:old"})
            self.latest_tag = previous
        else:
            self.latest_tag = None

    def add(self, tag, commit, assets, draft=False):
        self.tags[tag] = commit
        self.releases.append({"tag_name": tag, "draft": draft, "assets": [
            {"name": name, "digest": digest} for name, digest in assets.items()]})
        return self.releases[-1]

    def find(self, tag):
        return next((r for r in self.releases if r["tag_name"] == tag), None)

    def files(self, args):
        values = {args[i + 1] for i, a in enumerate(args[:-1]) if a.startswith("--")}  # --notes-file's file
        paths = [Path(a) for a in args if not a.startswith("--") and a not in values and Path(a).is_file()]
        return {p.name: ("sha256:" + __import__("hashlib").sha256(p.read_bytes()).hexdigest()
                         if self.digests else None) for p in paths}

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "api":
            path = args[1].split("/", 3)[3]
            if path == "releases/latest":
                release = self.find(self.latest_tag) if self.latest_tag else None
                if self.lag:
                    self.lag -= 1
                    release = self.find("v1.4.3")
                return (0, json_of(release), "") if release else (1, "", "gh: Not Found (HTTP 404)")
            if path.startswith("releases?"):
                return 0, json_of(self.releases), ""
            if path.startswith("git/ref/tags/"):
                tag = path.rsplit("/", 1)[1]
                if tag in self.tags:
                    return 0, json_of({"object": {"type": "commit", "sha": self.tags[tag]}}), ""
                return 1, "", "gh: Not Found (HTTP 404)"
        if args[:2] == ("release", "create"):
            tag, commit = args[2], args[args.index("--target") + 1]
            self.add(tag, commit, self.files(args))
            self.latest_tag = tag
            return 0, "", ""
        if args[:2] == ("release", "upload"):
            if "upload" in self.fail:
                return 1, "", "upload failed"
            release = self.find(args[2])
            release["assets"] = [{"name": n, "digest": d} for n, d in self.files(args).items()]
            return 0, "", ""
        if args[:2] == ("release", "edit"):
            release = self.find(args[2])
            if "--draft=true" in args:
                if args[2] == "v1.4.3" and "draft-previous" in self.fail:
                    return 1, "", "HTTP 502"
                release["draft"] = True
                if self.latest_tag == args[2]:
                    self.latest_tag = None
            else:
                release["draft"] = False
                self.latest_tag = args[2]
            return 0, "", ""
        raise AssertionError(f"gh {args} isn't something publish-release.py should ask")


def json_of(value):
    import json
    return json.dumps(value)


@pytest.fixture
def github(monkeypatch, tmp_path):
    fake = FakeGitHub()
    monkeypatch.setattr(publish_release, "gh", fake)
    monkeypatch.setattr(publish_release, "sleep", lambda seconds: None)
    folder = tmp_path / "release"
    folder.mkdir()
    for name in ("I-Need-A-Budget-1.5.0.dmg", "I-Need-A-Budget-1.5.0-Setup.exe", "I-Need-A-Budget-1.5.0-Setup.exe.sig"):
        (folder / name).write_bytes(f"made-up {name}".encode())
    notes = tmp_path / "notes.md"
    notes.write_text("Made-up notes.\n")
    fake.publish = lambda: publish_release.main(
        ["--version", "1.5.0", "--previous", "v1.4.3", "--commit", OURS, "--notes", str(notes), str(folder)])
    fake.folder = folder
    return fake


def published(github):
    release = github.find("v1.5.0")
    return (release is not None and not release["draft"] and github.latest_tag == "v1.5.0"
            and github.tags["v1.5.0"] == OURS and len(release["assets"]) == 3)


def test_a_release_is_published_checked_and_the_previous_one_drafted(github):
    assert github.publish() == 0
    assert published(github) and github.find("v1.4.3")["draft"]


def test_github_lagging_behind_is_waited_for(github):
    github.lag = 2
    assert github.publish() == 0 and published(github)


def test_a_release_github_shows_without_digests_goes_back_to_a_draft(github, capsys):
    github.digests = False
    assert github.publish() == 1
    assert github.find("v1.5.0")["draft"] and not github.find("v1.4.3")["draft"]  # installed copies keep 1.4.3
    assert "no digest" in capsys.readouterr().out


def test_a_rerun_finishes_a_draft_an_earlier_try_left_half_uploaded(github):
    github.add("v1.5.0", OURS, {"I-Need-A-Budget-1.5.0.dmg": "sha256:partial"}, draft=True)
    assert github.publish() == 0
    assert published(github) and github.find("v1.4.3")["draft"]
    assert not any(call[:2] == ("release", "create") for call in github.calls)


def test_a_rerun_after_the_previous_one_wasnt_drafted_only_drafts_it(github):
    github.fail = {"draft-previous"}
    assert github.publish() == 1 and published(github) and not github.find("v1.4.3")["draft"]
    github.fail = set()
    assert github.publish() == 0 and github.find("v1.4.3")["draft"]
    assert sum(call[:2] == ("release", "create") for call in github.calls) == 1


def test_a_tag_at_another_commit_or_a_moved_latest_release_stops_it_before_anything(github, capsys):
    github.tags["v1.5.0"] = OTHER
    assert github.publish() == 1 and "already a tag v1.5.0" in capsys.readouterr().out
    del github.tags["v1.5.0"]
    github.add("v1.4.4", OTHER, {"x.dmg": "sha256:x"})
    github.latest_tag = "v1.4.4"
    assert github.publish() == 1 and "latest release changed" in capsys.readouterr().out
    assert github.find("v1.5.0") is None


def test_a_published_release_with_other_files_is_left_alone(github, capsys):
    github.add("v1.5.0", OURS, {"I-Need-A-Budget-1.5.0.dmg": "sha256:someone-elses"})
    assert github.publish() == 1 and "published already, with other files" in capsys.readouterr().out
    assert not any(call[:2] in (("release", "edit"), ("release", "upload")) for call in github.calls)


# ── the workflow: the secrets stay in one job, and nothing reaches the log ──

def jobs(text):
    """{job name: its lines} for the workflow's jobs (two-space indented keys under `jobs:`)."""
    found, name = {}, None
    for line in text.split("jobs:\n", 1)[1].splitlines():
        if re.match(r"^  [a-z][a-z-]*:$", line):
            name = line.strip()[:-1]
            found[name] = []
        elif name:
            found[name].append(line)
    return found


def test_only_the_sign_job_reaches_the_release_environment_and_only_from_main():
    text = WORKFLOW.read_text(encoding="utf-8")
    environments = {name for name, lines in jobs(text).items() if any("environment:" in line for line in lines)}
    assert environments == {"sign"}
    sign = "\n".join(jobs(text)["sign"])
    assert "environment: release" in sign and "if: needs.check.outputs.real == 'true'" in sign
    # real is true only for a run started (workflow_dispatch) on main
    assert 'if [[ "$EVENT" == workflow_dispatch && "$GITHUB_REF" == refs/heads/main ]]; then\n            real=true' in text
    assert "if: needs.check.outputs.real != 'true'" in "\n".join(jobs(text)["sign-dry"])
    publish = "\n".join(jobs(text)["publish"])
    for guard in ("github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/main'",
                  "needs.check.outputs.publish == 'true'", "needs.sign.result == 'success'", "contents: write"):
        assert guard in publish
    assert text.count("contents: write") == 1


def test_a_failure_anywhere_stops_everything_after_it_publishing_included():
    """Each job needs the ones before it; the three that also run after a skipped one (sign-dry is
    skipped in a release, sign in a dry run) go on only when nothing before them failed."""
    found = jobs(WORKFLOW.read_text(encoding="utf-8"))

    def needs(name):
        line = next(line for line in found[name] if line.strip().startswith("needs:"))
        return set(re.findall(r"[a-z][a-z-]*", line.split(":", 1)[1]))

    assert needs("build-mac") == needs("build-windows") == {"check"}
    assert needs("sign") == needs("sign-dry") == {"check", "tests", "build-mac", "build-windows"}
    assert needs("package") == {"check", "sign", "sign-dry"}
    assert needs("check-windows") == {"check", "package"}
    assert needs("publish") == {"check", "sign", "package", "check-windows"}
    for name in ("package", "check-windows", "publish"):
        condition = "\n".join(found[name])
        assert "!cancelled() && !failure()" in condition, name
    assert "needs.package.result == 'success'" in "\n".join(found["check-windows"])
    assert "needs.check-windows.result == 'success'" in "\n".join(found["publish"])


def test_secrets_are_only_given_to_the_steps_that_sign():
    text = WORKFLOW.read_text(encoding="utf-8")
    uses = [line.strip() for line in text.splitlines() if "secrets." in line]
    assert uses == ["MAC_CERTIFICATE: ${{ secrets.MAC_CERTIFICATE }}",
                    "MAC_CERTIFICATE_PASSWORD: ${{ secrets.MAC_CERTIFICATE_PASSWORD }}",
                    "RELEASE_KEY: ${{ secrets.RELEASE_KEY }}"]
    assert all(line.split("${{", 1)[0].strip().endswith(":") for line in uses)  # env values, never in a run script
    sign, dry = "\n".join(jobs(text)["sign"]), "\n".join(jobs(text)["sign-dry"])
    assert all(use in sign for use in uses)
    assert "secrets" not in dry and "environment" not in dry  # not even through an anchor
    # each clean-up a step of its own, so one failing can't skip the other, in both jobs
    for name, alias in (("Delete the keychain", "*sign-clean-keychain"), ("Delete the key", "*sign-clean-key")):
        assert f"name: {name}\n        if: always()" in sign and f"- {alias}" in dry


def test_nothing_echoes_commands_or_puts_notes_into_a_script():
    for path in (WORKFLOW, ROOT / "scripts" / "sign-mac.sh", ROOT / "scripts" / "sign-windows.sh"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"set -[a-wyz]*x|set -o xtrace|bash -x", text), f"{path.name} echoes its commands"
    text = WORKFLOW.read_text(encoding="utf-8")
    # The notes are read from the run's event file: in a step's env the log would print them, and
    # in a script as ${{ }} they'd be run as code.
    notes = [line.strip() for line in text.splitlines() if "inputs.notes" in line]
    assert notes == ['jq -r \'.inputs.notes // ""\' "$GITHUB_EVENT_PATH" > "$notes"'] * 2
    assert "${{ inputs.notes" not in text


def test_a_release_from_github_checks_its_notes_for_private_details_without_logging_them():
    text = WORKFLOW.read_text(encoding="utf-8")
    checks = [line for line in text.splitlines() if 'privacy_check.py' in line and '--what "In the release notes"' in line]
    assert len(checks) == 2  # when the run starts, and just before publishing
    # what it finds is the private line itself, and the log is public
    assert all(line.rstrip().endswith(">/dev/null 2>&1 ||") for line in checks)
