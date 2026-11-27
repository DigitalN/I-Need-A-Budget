# SPDX-License-Identifier: GPL-3.0-only
"""The release key's public half (docs/DEVELOPMENT.md → Release secrets). Windows copies check every
update with it, so it stays one line of base64 holding the raw 32-byte Ed25519 public key."""

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_KEY = ROOT / "Application Files" / "release-key.pub"


def test_the_release_keys_public_half_is_32_raw_bytes_in_base64():
    lines = PUBLIC_KEY.read_text(encoding="ascii").splitlines()
    assert len(lines) == 1, f"{PUBLIC_KEY.name} is one line"
    assert len(base64.b64decode(lines[0].strip(), validate=True)) == 32
