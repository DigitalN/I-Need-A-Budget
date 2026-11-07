#!/usr/bin/env python3
"""The privacy checks shared by the pre-commit hook, the commit-msg hook, the release script
and scripts/tests/test_privacy.py: text that must never reach GitHub.

    privacy_check.py [--denylist FILE] [TEXT_FILE]      (reads standard input without one)

Prints every offending line to standard error and exits 1 if there is one:
  - a masked card number in the shape Tangerine uses in file names (four digits, x's, then
    the last four digits);
  - a full card number: 13 to 19 digits (spaces or dashes between groups) that pass the Luhn
    check, as in a Quicken file's CREDITLINE;
  - a word or name listed in .git-denylist (a local, ignored file, one per line, `#` starts a
    comment; matching ignores case).
Nothing here may ever be weakened (.claude/CLAUDE.md).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

MASKED_CARD = re.compile(r"[0-9]{4}[ *x]+x{4}[ *x]+x{4}[ *x]+[0-9]{4}", re.IGNORECASE)
DIGIT_RUN = re.compile(r"(?<![0-9])[0-9](?:[ -]?[0-9]){12,18}(?![0-9])")


def luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def card_numbers(text):
    """The digits of every full card number in `text`: 13 to 19 digits that pass the Luhn
    check, with at most a space or dash between digits."""
    found = []
    for m in DIGIT_RUN.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn(digits):
            found.append(digits)
    return found


def masked_card_numbers(text):
    return MASKED_CARD.findall(text)


def read_denylist(path):
    terms = []
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            term = line.split("#", 1)[0].strip()
            if term:
                terms.append(term.lower())
    return terms


def problems(text, terms=()):
    """[(line, why)] for each line of `text` that has a card number or a denylisted word."""
    out = []
    for line in text.splitlines():
        lower = line.lower()
        if masked_card_numbers(line):
            out.append((line, "a masked card number"))
        if card_numbers(line):
            out.append((line, "a card number"))
        for term in terms:
            if term in lower:
                out.append((line, f'a private term from .git-denylist: "{term}"'))
    return out


def repository_denylist():
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    return str(Path(top) / ".git-denylist") if top else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--denylist", help="the denylist (default: .git-denylist at the top of the repository)")
    parser.add_argument("--what", default="text", help="what is being checked, for the message")
    parser.add_argument("file", nargs="?", help="the text to check (default: standard input)")
    args = parser.parse_args(argv)
    text = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file else sys.stdin.read()
    found = problems(text, read_denylist(args.denylist or repository_denylist()))
    for line, why in found:
        print(f"✗ {args.what}: {why}\n    {line.strip()[:200]}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
