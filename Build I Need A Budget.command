#!/bin/bash
# Double-click this file to rebuild I Need A Budget.app from your copy of the code.
# Pulls the latest from your git remote, runs the tests, then builds, installs to
# /Applications and opens it. (Installed copies also update themselves from GitHub
# releases; this is for running your own changes.)

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT" || exit 1

echo "═══════════════════════════════════════════"
echo "  Building I Need A Budget..."
echo "═══════════════════════════════════════════"
echo ""

echo "→ Pulling latest from git..."
git pull --ff-only 2>&1
echo ""

echo "→ Running tests..."
if ! python3 -m pytest tests -q 2>&1 | tail -3; then
    echo "✗ Tests failed"
fi
echo ""

if scripts/install.sh --open; then
    echo "✓ Build complete!"
    osascript -e 'tell application "Terminal" to close front window' &>/dev/null &
else
    echo "✗ Build failed — check output above"
    echo ""
    echo "Press any key to close..."
    read -n 1
fi
