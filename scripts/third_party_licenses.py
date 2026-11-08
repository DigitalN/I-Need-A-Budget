# SPDX-License-Identifier: GPL-3.0-only
"""The licenses of everything the build puts in the app besides our own code.

The PyInstaller spec calls `for_modules()` with the top-level modules it collected, and
the text goes in the app as "Third-party licenses.txt". Each bundled package's license
files come from its installed metadata (packages shipping the same text are listed
together), then PyInstaller's (its launcher is in the app), then Python's, whose
documentation also covers what Python incorporates (OpenSSL, expat, libffi...).

A package with no license file whose metadata says MIT gets the MIT License's text. Any
other package without one stops the build (`MissingLicense`) until its text is in GIVEN:
nothing ships without its license.
"""

import importlib.metadata as metadata
import re
import sys
import sysconfig
from pathlib import Path

LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)
RULE = "=" * 72

# License texts for bundled packages that ship none and aren't MIT, by package name.
GIVEN = {}

HEADER = """\
I Need A Budget includes the software below, each under its own license.
I Need A Budget itself is free software under the GNU GPL v3: see LICENSE.txt and NOTICE.txt.
"""

MIT = """\
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class MissingLicense(Exception):
    pass


def for_modules(top_level_names):
    """The whole file, for the packages that provide these top-level modules."""
    owners = metadata.packages_distributions()
    names = {name for top in top_level_names for name in owners.get(top, [])}
    names.add("pyinstaller")  # its launcher runs the app
    return notices(metadata.distribution(name) for name in names)


def notices(distributions):
    """Each distribution's licenses, then Python's. Raises MissingLicense naming every
    distribution without one."""
    by_text = {}  # license text -> titles of the packages that ship it
    missing = []
    unique = {_key(d): d for d in distributions}
    for key in sorted(unique):
        dist = unique[key]
        title = f"{dist.metadata['Name']} {dist.version}"
        text = _license_text(dist)
        if text is None:
            missing.append(title)
        else:
            by_text.setdefault(text, []).append(title)
    if missing:
        raise MissingLicense("No license text for " + ", ".join(missing)
                             + ". Put its license text in GIVEN (scripts/third_party_licenses.py).")
    sections = [(", ".join(titles), text) for text, titles in by_text.items()]
    sections.append((f"Python {sys.version.split()[0]}", _python_license()))
    return HEADER + "".join(f"\n{RULE}\n{title}\n{RULE}\n\n{text.strip()}\n" for title, text in sections)


def _key(dist):
    return re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()


def _license_text(dist):
    files = sorted((f for f in dist.files or []
                    if LICENSE_FILE.match(f.name) and any(p.endswith(".dist-info") for p in f.parts)),
                   key=str)
    if files:
        return "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files)
    if _key(dist) in GIVEN:
        return GIVEN[_key(dist)]
    if _says_mit(dist.metadata):
        by = _author(dist.metadata)
        return (f"Ships no license file; its package metadata says the MIT License"
                f"{f', by {by}' if by else ''}. The MIT License:\n\n{MIT}")
    return None


def _says_mit(meta):
    stated = [(meta.get("License-Expression") or "").strip(), (meta.get("License") or "").strip()]
    return (any(s in ("MIT", "MIT License") for s in stated)
            or "License :: OSI Approved :: MIT License" in (meta.get_all("Classifier") or []))


def _author(meta):
    author = (meta.get("Author") or "").strip()
    if not author:  # "Name <address>": the name only
        author = re.sub(r"\s*<[^>]*>", "", meta.get("Author-email") or "").strip()
    return author


def _python_license():
    """The documentation's license page (python.org builds) covers the software Python
    incorporates too; LICENSE.txt is Python's own license only."""
    docs = Path(sys.base_prefix) / "Resources/English.lproj/Documentation/_sources/license.rst.txt"
    own = Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt"
    for path in (docs, own):
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise MissingLicense(f"No license text for Python: neither {docs} nor {own} exists.")
