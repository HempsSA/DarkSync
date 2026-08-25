#!/usr/bin/env bash
#
# DarkSync Updater — Pull latest code from the shared git remote.
# Usage:
#   ./update.sh            Pull latest changes
#   ./update.sh --check    Only check for updates (no pull)
#

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$REPO_DIR"

# Make sure this is a git repo — try to recover if not
if [ ! -d ".git" ]; then
    echo "[!] Not a git repository — attempting to recover..."
    echo ""

    REMOTE_URL="https://github.com/HempsSA/DarkSync.git"

    echo "[v] Initializing git repo..."
    git init
    git remote add origin "$REMOTE_URL"
    git fetch origin || { echo "[X] Could not connect to remote."; exit 1; }
    git checkout -b main origin/main || { echo "[X] Could not set up branch."; exit 1; }
    echo "[i] Repo recovered from $REMOTE_URL"
    echo ""
fi

# Check for origin remote
REMOTE="$(git remote get-url origin 2>/dev/null || true)"

if [ -z "$REMOTE" ]; then
    echo "[X] No 'origin' remote configured."
    echo "   Set one with: git remote add origin <your-repo-url>"
    exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "[i] Repo:     $REPO_DIR"
echo "[i] Branch:   $CURRENT_BRANCH"
echo "[i] Remote:   $REMOTE"
echo ""

# Fetch latest
echo "[v] Fetching..."
git fetch origin

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "origin/$CURRENT_BRANCH" 2>/dev/null || echo "$LOCAL_SHA")"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    echo "[i] Already up to date."
    exit 0
fi

BEHIND="$(git rev-list HEAD..origin/$CURRENT_BRANCH --count)"
echo "[v] $BEHIND commit(s) behind."

if [ "${1:-}" = "--check" ]; then
    echo "   (Use './update.sh' without --check to pull.)"
    exit 0
fi

echo ""
echo "Pulling..."
git pull origin "$CURRENT_BRANCH"

echo ""
echo "[i] Updated to $(git rev-parse --short HEAD)."
echo ""
echo "[i] If DarkSync is running, restart it to load the new code."
