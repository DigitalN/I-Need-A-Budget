# SPDX-License-Identifier: GPL-3.0-only
"""Nothing private in the repository: no bank files, no databases, no personal words.

The pre-commit hook (scripts/hooks/pre-commit) checks what's being committed; this
checks everything already in the working tree that git would track.
"""

import os
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
    terms = privacy_check.read_denylist(privacy_check.repository_denylist(ROOT))  # the main checkout's, from a worktree too
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
    """core.hooksPath is this repository's scripts/hooks, written relative or absolute: a git worktree gets the
    main checkout's setting, which is the absolute path of the main checkout's folder."""
    path = subprocess.run(["git", "config", "core.hooksPath"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert path
    main = Path(privacy_check.repository_denylist(ROOT)).parent
    assert (ROOT / path).resolve() in {(ROOT / "scripts" / "hooks").resolve(), (main / "scripts" / "hooks").resolve()}


# ── the hooks from a git worktree (issue #117) ──
# .git-denylist is a local, ignored file, so only the main checkout has it. The Claude desktop app
# works in worktrees, and a commit made there must still be checked against it.

WORD = "zzmadeupword"  # stands in for a private word


def _git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], cwd=cwd,
                          capture_output=True, text=True, env=env)


def _repository_with_a_worktree(tmp_path, denylist=True):
    """A throwaway repository (hooks switched on the way a worktree inherits them: by absolute path) with a
    .git-denylist holding WORD, and a worktree of it. Returns (main checkout, worktree)."""
    main, tree = tmp_path / "main", tmp_path / "tree"
    main.mkdir()
    assert _git(main, "init", "-q").returncode == 0
    (main / "README").write_text("sample\n")
    assert _git(main, "add", "README").returncode == 0
    assert _git(main, "commit", "-q", "-m", "First").returncode == 0
    if denylist:
        (main / ".git-denylist").write_text(f"# made up\n{WORD}\n")
    assert _git(main, "config", "core.hooksPath", str(ROOT / "scripts" / "hooks")).returncode == 0
    assert _git(main, "worktree", "add", "-q", "-b", "other", str(tree)).returncode == 0
    return main, tree


def _commit_file(where, text, message="Add a file"):
    (where / "notes.txt").write_text(text)
    assert _git(where, "add", "notes.txt").returncode == 0
    return _git(where, "commit", "-q", "-m", message)


def test_the_denylist_is_the_main_checkouts_from_a_worktree_too(tmp_path):
    main, tree = _repository_with_a_worktree(tmp_path)
    for where in (main, tree):
        found = privacy_check.repository_denylist(where)
        assert Path(found).resolve() == (main / ".git-denylist").resolve()


def test_a_commit_from_a_worktree_is_checked_against_the_denylist(tmp_path):
    main, tree = _repository_with_a_worktree(tmp_path)
    refused = _commit_file(tree, f"bought at {WORD}\n")
    assert refused.returncode != 0 and "Commit refused" in refused.stderr
    assert _git(tree, "log", "--oneline").stdout.count("\n") == 1  # nothing was committed
    assert _commit_file(main, f"bought at {WORD}\n").returncode != 0  # and from the main checkout


def test_a_commit_message_from_a_worktree_is_checked_too(tmp_path):
    _, tree = _repository_with_a_worktree(tmp_path)
    refused = _commit_file(tree, "fine\n", message=f"Paid {WORD}")
    assert refused.returncode != 0 and "Commit refused" in refused.stderr


def test_a_clean_commit_from_a_worktree_goes_through(tmp_path):
    _, tree = _repository_with_a_worktree(tmp_path)
    done = _commit_file(tree, "nothing private here\n")
    assert done.returncode == 0, done.stderr


def test_a_copy_without_a_denylist_still_commits_and_says_so(tmp_path):
    _, tree = _repository_with_a_worktree(tmp_path, denylist=False)
    done = _commit_file(tree, "nothing private here\n")
    assert done.returncode == 0, done.stderr
    assert "No .git-denylist" in done.stderr
