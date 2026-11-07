"""The privacy hooks refuse what they promise to refuse, tried in a throwaway git repository.
Made-up data only; card numbers are built in code, as helpers.card_number() does."""

import subprocess
import sys
from pathlib import Path

import pytest

from helpers import card_number

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "scripts" / "hooks"
sys.path.insert(0, str(HOOKS))
import privacy_check  # noqa: E402

DENIED = "samplepersonword"


class Repo:
    def __init__(self, path):
        self.path = path

    def __truediv__(self, name):
        return self.path / name

    def git(self, *args, check=True):
        return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                              cwd=self.path, capture_output=True, text=True, check=check)


@pytest.fixture
def repo(tmp_path):
    """A throwaway repository with the hooks on and one private word in its .git-denylist."""
    repo = Repo(tmp_path)
    repo.git("init", "-q")
    repo.git("config", "core.hooksPath", str(HOOKS))
    (tmp_path / ".git-denylist").write_text(f"# private words\n{DENIED}  # a made-up name\n")
    (tmp_path / "README.md").write_text("A harmless file.\n")
    repo.git("add", "README.md")
    assert repo.git("commit", "-q", "-m", "A harmless start", check=False).returncode == 0
    return repo


def _commit(repo, name, text, message="Add a file", force=False):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    repo.git("add", "-f", name) if force else repo.git("add", name)
    result = repo.git("commit", "-q", "-m", message, check=False)
    return result


def _refused(result):
    return result.returncode != 0 and "refused" in (result.stderr + result.stdout).lower()


def test_an_ordinary_commit_goes_through(repo):
    assert _commit(repo, "notes.md", "Made-up notes about SAMPLE CAFE CALGARY AB, $12.50.\n").returncode == 0


def test_a_full_card_number_is_refused(repo):
    number = card_number("4321")
    assert len(number) == 16
    spaced = " ".join(number[i:i + 4] for i in range(0, 16, 4))
    dashed = "-".join(number[i:i + 4] for i in range(0, 16, 4))
    for text in (f"<CREDITLINE>{number}\n", f"card {spaced}\n", f"card {dashed}.\n", f"x = '{number}'\n"):
        assert _refused(_commit(repo, "snippet.txt", text)), text
        repo.git("reset", "-q")
    # Sixteen digits that aren't a card (they fail the Luhn check) are just a number.
    not_a_card = number[:-1] + str((int(number[-1]) + 1) % 10)
    assert _commit(repo, "snippet.txt", f"order {not_a_card}\n").returncode == 0


def test_database_side_files_and_logs_are_refused(repo):
    for name in ("budget.db-wal", "budget.db-shm", "budget.db-journal", "Backups/budget-2026-10-12.tmp", "budget.tmp",
                 "app.log", "Logs/app.log", "budget.db"):
        assert _refused(_commit(repo, name, "x", force=True)), name
        repo.git("reset", "-q")


def test_a_private_word_is_refused_even_on_a_line_that_starts_with_plus(repo):
    assert _refused(_commit(repo, "a.txt", f"a normal line about {DENIED}\n"))
    repo.git("reset", "-q")
    assert _refused(_commit(repo, "b.diff", f"+{DENIED} added\n"))  # git shows it as "++…"
    repo.git("reset", "-q")
    assert _refused(_commit(repo, "c.diff", f"++{DENIED}\n"))


def test_a_commit_message_is_checked_too(repo):
    (repo / "fine.txt").write_text("Nothing private.\n")
    repo.git("add", "fine.txt")
    assert _refused(repo.git("commit", "-q", "-m", f"Fix the {DENIED} case", check=False))
    assert _refused(repo.git("commit", "-q", "-m", f"Test with {card_number('1111')}", check=False))
    assert repo.git("commit", "-q", "-m", "Fix the rounding", check=False).returncode == 0


def test_the_release_notes_are_checked_with_the_same_rules(repo):
    notes = repo.path.parent / f"{repo.path.name}-notes.md"
    check = [sys.executable, str(HOOKS / "privacy_check.py"), "--denylist", str(repo / ".git-denylist"), str(notes)]
    notes.write_text("Fixes the way amounts are read.\n")
    assert subprocess.run(check, capture_output=True, text=True).returncode == 0
    notes.write_text(f"Thanks to {DENIED.upper()} for the report.\n")
    assert subprocess.run(check, capture_output=True, text=True).returncode == 1
    notes.write_text(f"Card {card_number('9999')} was charged twice.\n")
    assert subprocess.run(check, capture_output=True, text=True).returncode == 1
    assert "privacy_check.py" in (ROOT / "scripts" / "release.sh").read_text()


def test_luhn_and_the_shapes_it_looks_at():
    number = card_number("2468")
    assert privacy_check.card_numbers(f"a {number} b") == [number]
    assert privacy_check.card_numbers("2026-10-12 10:30:00 id 1234567890123") == []  # fails Luhn / is a date
    assert privacy_check.card_numbers("4" * 12) == [] and privacy_check.card_numbers("1" * 20) == []
    masked = "0000 " + "x" * 4 + "  " + "x" * 4 + " 1234"
    assert privacy_check.masked_card_numbers(f"file {masked}.CSV")
