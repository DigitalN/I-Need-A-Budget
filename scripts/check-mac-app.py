#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Will this Mac app start on every Mac its Info.plist says it does?

Every program and library in the app (the main program, Python, each extension module) says the
oldest macOS it runs on, and which processors it's built for. The release run (issue #190) builds on
whatever macOS, Python and PyInstaller GitHub's Macs have, so it checks that:
  - none needs a macOS newer than the app's LSMinimumSystemVersion (12.0: macOS 12 still starts it);
  - each is built for every processor the main program is (one built for Intel only would stop the
    app on an Apple Silicon Mac the moment Python loaded it);
  - with --previous, the main program is built for every processor the previous release's was: a
    Mac that runs the copy it has must be able to start the update (1.4.3's is arm64 only).
A minimum it can't read, the app's or a file's, is a problem too: what can't be checked doesn't pass.

    python3 scripts/check-mac-app.py "I Need A Budget.app" [--previous "the previous release's app"]

Reads the files only, in Python, so it runs and is tested on any computer. Prints why not, if not.
"""

import argparse
import plistlib
import re
import struct
import sys
from pathlib import Path

# Mach-O, as <mach-o/loader.h> and <mach-o/fat.h> have it.
THIN = {b"\xcf\xfa\xed\xfe": ("<", 32), b"\xce\xfa\xed\xfe": ("<", 28),   # 64- and 32-bit, little-endian
        b"\xfe\xed\xfa\xcf": (">", 32), b"\xfe\xed\xfa\xce": (">", 28)}
FAT, FAT_64 = b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"
LC_VERSION_MIN_MACOSX, LC_BUILD_VERSION = 0x24, 0x32
PLATFORM_MACOS = 1
CPU_NAMES = {7: "i386", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64", 0x0200000C: "arm64_32"}


def macos_version(encoded):
    """(12, 0) for 0x000C0000: X.Y.Z as xxxx.yy.zz in nibbles, the patch dropped."""
    return encoded >> 16, (encoded >> 8) & 0xFF


def shown(version):
    return f"{version[0]}.{version[1]}"


def _slice(data, offset):
    """(processor, oldest macOS or None) for the Mach-O at `offset`, or None if it isn't one."""
    kind = THIN.get(data[offset:offset + 4])
    if kind is None:
        return None
    order, header = kind
    if len(data) < offset + header:
        return None
    cpu, _, _, commands, _, _ = struct.unpack_from(order + "iIIIII", data, offset + 4)
    oldest, at = None, offset + header
    for _ in range(commands):
        if len(data) < at + 8:
            break
        command, size = struct.unpack_from(order + "II", data, at)
        if command == LC_BUILD_VERSION and len(data) >= at + 16:
            platform, minos = struct.unpack_from(order + "II", data, at + 8)
            if platform == PLATFORM_MACOS:
                oldest = macos_version(minos)
        elif command == LC_VERSION_MIN_MACOSX and len(data) >= at + 12:
            oldest = macos_version(struct.unpack_from(order + "I", data, at + 8)[0])
        if size < 8:
            break
        at += size
    return cpu, oldest


def slices(data):
    """[(processor, oldest macOS or None)] for each slice of a Mach-O file (a universal one has
    several), or [] when `data` isn't one."""
    magic = data[:4]
    if magic in (FAT, FAT_64):
        count = struct.unpack_from(">I", data, 4)[0]
        if not 0 < count <= 16:  # Java class files start with the same four bytes
            return []
        found, entry = [], 8
        for _ in range(count):
            if magic == FAT:
                offset = struct.unpack_from(">iiIII", data, entry)[2]
                entry += 20
            else:
                offset = struct.unpack_from(">iiQQII", data, entry)[2]
                entry += 32
            one = _slice(data, offset)
            if one:
                found.append(one)
        return found
    one = _slice(data, 0)
    return [one] if one else []


def read_head(path, size=1 << 16):
    """The start of a file (its Mach-O headers), or the whole of a universal one, whose slices'
    headers are further in."""
    with open(path, "rb") as f:
        head = f.read(size)
        if head[:4] in (FAT, FAT_64):
            head += f.read()
    return head


def main_program(app):
    """(Info.plist, the main program's slices), or (None, why not)."""
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None, f"{app} has no Info.plist."
    main = Path(app) / "Contents" / "MacOS" / str(info.get("CFBundleExecutable", ""))
    parts = slices(read_head(main)) if main.is_file() else []
    if not parts:
        return None, f"The main program, {main.name}, isn't a Mac program."
    return info, parts


def problems(app, previous=None):
    """[why] for every file in `app` that wouldn't run everywhere the app says it does, or where
    `previous` (the app it updates) runs."""
    app = Path(app)
    info, parts = main_program(app)
    if info is None:
        return [parts]
    said = str(info.get("LSMinimumSystemVersion", ""))
    if not re.fullmatch(r"\d+(\.\d+){0,2}", said):
        return [f"Info.plist's LSMinimumSystemVersion ({said or 'none'}) isn't a macOS version."]
    wanted = (tuple(int(n) for n in said.split(".")[:2]) + (0,))[:2]
    processors = {cpu for cpu, _ in parts}
    found = []
    if previous is not None:
        old_info, old_parts = main_program(previous)
        if old_info is None:
            return [f"The previous release's app: {old_parts}"]
        lost = {cpu for cpu, _ in old_parts} - processors
        if lost:
            names = ", ".join(sorted(CPU_NAMES.get(c, str(c)) for c in lost))
            found.append(f"The previous release runs on {names} Macs, and this one isn't built for them:"
                         f" they couldn't start the update.")
    for path in sorted(p for p in app.rglob("*") if p.is_file() and not p.is_symlink()):
        parts = slices(read_head(path))
        if not parts:
            continue
        name = path.relative_to(app)
        for cpu, oldest in parts:
            if oldest is None:
                found.append(f"{name} ({CPU_NAMES.get(cpu, cpu)}) doesn't say which macOS it needs.")
            elif oldest > wanted:
                found.append(f"{name} ({CPU_NAMES.get(cpu, cpu)}) needs macOS {shown(oldest)}, "
                             f"but the app says it runs on {shown(wanted)}.")
        missing = processors - {cpu for cpu, _ in parts}
        if missing:
            found.append(f"{name} isn't built for {', '.join(sorted(CPU_NAMES.get(c, str(c)) for c in missing))}, "
                         f"which the main program is.")
    return found


def summary(app):
    """What the app is built for, for the log: processors, and the newest macOS any file needs."""
    app = Path(app)
    newest, processors, count = None, set(), 0
    for path in (p for p in app.rglob("*") if p.is_file() and not p.is_symlink()):
        parts = slices(read_head(path))
        count += bool(parts)
        for cpu, oldest in parts:
            processors.add(CPU_NAMES.get(cpu, str(cpu)))
            if oldest and (newest is None or oldest > newest):
                newest = oldest
    needs = f"macOS {shown(newest)} at the most" if newest else "no macOS version said"
    return f"{count} programs and libraries, for {', '.join(sorted(processors))}: {needs}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app")
    parser.add_argument("--previous", help="the previous release's app, which this one updates")
    args = parser.parse_args(argv)
    found = problems(args.app, args.previous)
    for why in found:
        print(f"✗ {why}", file=sys.stderr)
    if not found:
        print(f"OK: {summary(args.app)}.")
        if args.previous:
            print(f"The previous release's app: {summary(args.previous)}.")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
