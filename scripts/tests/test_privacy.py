"""Nothing private in the repository: no bank files, no databases, no personal words.

The pre-commit hook (scripts/hooks/pre-commit) checks what's being committed; this
checks everything already in the working tree that git would track.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "hooks"))
import privacy_check  # noqa: E402  (the checks the hooks and release.sh run)


def tracked_files():
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.split("\n")
    return [ROOT / p for p in listed if p and (ROOT / p).is_file()]


def test_gitignore_keeps_out_bank_files_and_databases():
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "x.CSV", "x.csv", "x.qfx", "x.ofx", "budget.db",
                              "budget.db-wal", "budget.db-shm", "budget.db-journal", "budget-2026-10-12.tmp", "app.log",
                              "Imported Files/a.CSV", "Backups/budget-2026-10-12.db", ".git-denylist"],
                             cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert len(ignored) == 14  # 13 paths, one of them with a space


def test_no_bank_files_or_databases_would_be_committed():
    bad = [p for p in tracked_files() if p.suffix.lower() in (".csv", ".ofx", ".qfx", ".qbo", ".db", ".sqlite")]
    assert bad == []


def test_no_private_words_or_card_numbers_in_the_repository():
    denylist = ROOT / ".git-denylist"
    terms = []
    if denylist.exists():
        for line in denylist.read_text(encoding="utf-8").splitlines():
            term = line.split("#", 1)[0].strip()
            if term:
                terms.append(term.lower())
    found = []
    for path in tracked_files():
        if path.name == ".git-denylist" or path.suffix in (".icns", ".png", ".tiff"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lower = text.lower()
        found += [f"{path.relative_to(ROOT)}: {t}" for t in terms if t in lower]
        if privacy_check.masked_card_numbers(text) or privacy_check.card_numbers(text):
            found.append(f"{path.relative_to(ROOT)}: a card number")
    assert found == []


def test_the_commit_hook_is_switched_on():
    path = subprocess.run(["git", "config", "core.hooksPath"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert path == "scripts/hooks"
