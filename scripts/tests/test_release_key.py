# SPDX-License-Identifier: GPL-3.0-only
"""The release key's public half (docs/DEVELOPMENT.md → Release secrets). Windows copies check every
update with it, so it stays one line of base64 holding the raw 32-byte Ed25519 public key, and one
windows_update.py can use: a placeholder (32 zero bytes) or another point of small order would turn
every Windows copy's updates off."""

import base64
from pathlib import Path

import windows_update

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_KEY = ROOT / "Application Files" / "release-key.pub"


def test_the_release_keys_public_half_is_32_raw_bytes_in_base64():
    lines = PUBLIC_KEY.read_text(encoding="ascii").splitlines()
    assert len(lines) == 1, f"{PUBLIC_KEY.name} is one line"
    assert len(base64.b64decode(lines[0].strip(), validate=True)) == 32


def test_windows_copies_can_check_updates_with_it():
    assert windows_update.RELEASE_KEY_FILE == PUBLIC_KEY
    key = windows_update.release_key()
    assert key is not None, f"{PUBLIC_KEY.name} isn't a key windows_update.py can check updates with"
    assert key == base64.b64decode(PUBLIC_KEY.read_text(encoding="ascii").strip())
