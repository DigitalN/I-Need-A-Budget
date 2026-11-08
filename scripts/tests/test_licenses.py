# SPDX-License-Identifier: GPL-3.0-only
"""The license reaches every copy: a tag in each source file, and the licenses of what the
build bundles (scripts/third_party_licenses.py, which the spec runs)."""

import importlib.metadata as metadata
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import third_party_licenses  # noqa: E402

TAG = "SPDX-License-Identifier: GPL-3.0-only"
SOURCE = re.compile(r"\.(py|sh|swift|command|html|spec)$|^scripts/hooks/")


def test_every_source_file_says_its_license():
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.splitlines()
    sources = [p for p in listed if SOURCE.search(p) and (ROOT / p).is_file()]
    assert len(sources) > 50
    untagged = [p for p in sources
                if not any(TAG in line for line in (ROOT / p).read_text(encoding="utf-8").splitlines()[:3])]
    assert untagged == [], f"Add '{TAG}' near the top (after a #! line) of: {', '.join(untagged)}"


def fake_distribution(folder, name, metadata_lines, license_text=None):
    info = folder / f"{name}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("\n".join(["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0",
                                              *metadata_lines]) + "\n")
    record = [f"{info.name}/METADATA,,"]
    if license_text is not None:
        (info / "LICENSE").write_text(license_text)
        record.append(f"{info.name}/LICENSE,,")
    (info / "RECORD").write_text("\n".join(record) + "\n")
    return metadata.PathDistribution(info)


def test_a_package_without_a_license_stops_the_build(tmp_path):
    closed = fake_distribution(tmp_path, "sample-closed", ["License: Proprietary"])
    with pytest.raises(third_party_licenses.MissingLicense, match="sample-closed 1.0"):
        third_party_licenses.notices([closed])


def test_licenses_come_from_the_packages_files(tmp_path):
    a = fake_distribution(tmp_path, "sample-a", [], "Sample License, Copyright Sample Author")
    b = fake_distribution(tmp_path, "sample-b", [], "Sample License, Copyright Sample Author")
    c = fake_distribution(tmp_path, "sample-c", [], "Another Sample License")
    text = third_party_licenses.notices([c, b, a])
    assert text.count("Sample License, Copyright Sample Author") == 1  # the same text once, for both
    assert "sample-a 1.0, sample-b 1.0\n" in text
    assert "Another Sample License" in text
    assert "Python Software Foundation" in text  # Python's own, always


def test_mit_without_a_file_gets_the_mit_text(tmp_path):
    mit = fake_distribution(tmp_path, "sample-mit", ["Author: Sample Author", "License: MIT"])
    text = third_party_licenses.notices([mit])
    assert "the MIT License, by Sample Author" in text
    assert "Permission is hereby granted, free of charge" in text


def test_every_package_the_app_needs_has_a_license():
    lines = (ROOT / "Application Files" / "requirements.txt").read_text().splitlines()
    names = [line.split("==")[0] for line in lines if line and not line.startswith("#")]
    runtime = [n for n in names if n not in ("pyinstaller", "pytest")]
    text = third_party_licenses.notices(metadata.distribution(n) for n in runtime)
    for name in runtime:
        assert metadata.distribution(name).metadata["Name"] in text
