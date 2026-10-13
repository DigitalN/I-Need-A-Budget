#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  I Need A Budget — First Time Setup
#  Double-click this file to install what's needed, switch on the
#  privacy guard, then build and install the app. Only needs to be
#  run once on a new machine.
# ═══════════════════════════════════════════════════════════════

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  I Need A Budget — First Time Setup"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── 1. Check for Python 3 ────────────────────────────────────
echo "→ Checking for Python 3..."
if ! command -v python3 &>/dev/null; then
    echo ""
    echo "  ✗ Python 3 is not installed."
    echo "    Download it from: https://www.python.org/downloads/"
    echo ""
    echo "  After installing Python, run this setup again."
    echo ""
    echo "Press any key to close..."
    read -n 1
    exit 1
fi
echo "  ✓ Found $(python3 --version 2>&1)"
echo ""

# ── 2. Install Python packages (pinned versions) ─────────────
echo "→ Installing Python packages..."
pip3 install -r "$PROJECT_ROOT/Application Files/requirements.txt" \
    2>&1 | grep -E "^(Successfully|Requirement|Installing)" || true
echo "  ✓ All packages installed"
echo ""

# ── 3. Privacy guard: refuse commits with bank data ──────────
echo "→ Switching on the privacy guard for git commits..."
git config core.hooksPath scripts/hooks
echo "  ✓ Commits containing bank files, databases or card numbers will be refused"
echo ""

# ── 4. Build and install the app ─────────────────────────────
echo "→ Building I Need A Budget.app..."
echo ""
if ! "$PROJECT_ROOT/scripts/install.sh" --open; then
    echo "  ✗ Build failed — check output above"
    echo ""
    echo "Press any key to close..."
    read -n 1
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✓ Setup complete! I Need A Budget is in Applications."
echo ""
echo "  It keeps itself up to date (Settings → Updates)."
echo "  To rebuild your own changes: double-click 'Build I Need A Budget.command'"
echo "═══════════════════════════════════════════════════════════"
echo ""

read -p "Press any key to close..." -n 1
osascript -e 'tell application "Terminal" to close front window' &>/dev/null &
