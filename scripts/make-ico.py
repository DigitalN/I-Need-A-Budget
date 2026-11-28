# SPDX-License-Identifier: GPL-3.0-only
"""Makes the Windows icon, Application Files/INeedABudget.ico, from the Mac's
INeedABudget.icns (scripts/make-icon.swift draws both's artwork): its 1024-pixel picture,
without most of the margin macOS's icon grid leaves around the rounded square, at each size
Windows asks for. Run it after redrawing the icon:

    python -m pip install pillow==12.0.0   # only for this script, not the app
    python scripts/make-ico.py
"""

import io
import struct
from pathlib import Path

from PIL import Image

APP_FILES = Path(__file__).resolve().parents[1] / "Application Files"
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]  # the taskbar, Start, Explorer's views, at 100–300%
KEEP = 920  # of the 1024 pixels around the middle: the rounded square (824) and its shadow


def biggest_picture(icns):
    """The largest PNG in an .icns file."""
    data, found = icns.read_bytes(), []
    assert data[:4] == b"icns", f"{icns} isn't an .icns file"
    at = 8
    while at < len(data):
        kind, length = struct.unpack(">4sI", data[at:at + 8])
        body = data[at + 8:at + length]
        if body[:8] == b"\x89PNG\r\n\x1a\n":
            found.append(Image.open(io.BytesIO(body)))
        at += length
    return max(found, key=lambda picture: picture.width).convert("RGBA")


def main():
    picture = biggest_picture(APP_FILES / "INeedABudget.icns")
    margin = (picture.width - KEEP * picture.width // 1024) // 2
    picture = picture.crop((margin, margin, picture.width - margin, picture.height - margin))
    output = APP_FILES / "INeedABudget.ico"
    picture.resize((256, 256), Image.LANCZOS).save(output, sizes=[(s, s) for s in SIZES],
                                                   append_images=[picture.resize((s, s), Image.LANCZOS)
                                                                  for s in SIZES])
    print(output)


if __name__ == "__main__":
    main()
