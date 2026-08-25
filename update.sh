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

# Make sure this is a git repo
if [ ! -d ".git" ]; then
    echo "❌ Not a git repository. Run 'git init' and add your remote first."
    exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
REMOTE="$(git remote get-url origin 2>/dev/null || true)"

if [ -z "$REMOTE" ]; then
    echo "❌ No 'origin' remote configured."
    echo "   Set one with: git remote add origin <your-repo-url>"
    exit 1
fi

echo "📂 Repo:     $REPO_DIR"
echo "🔀 Branch:   $CURRENT_BRANCH"
echo "🌐 Remote:   $REMOTE"
echo ""

# Fetch latest
echo "⬇️  Fetching..."
git fetch origin

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "origin/$CURRENT_BRANCH" 2>/dev/null || echo "$LOCAL_SHA")"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    echo "✅ Already up to date."
    exit 0
fi

BEHIND="$(git rev-list HEAD..origin/$CURRENT_BRANCH --count)"
echo "📥 $BEHIND commit(s) behind."

if [ "${1:-}" = "--check" ]; then
    echo "   (Use './update.sh' without --check to pull.)"
    exit 0
fi

echo ""
echo "Pulling..."
git pull origin "$CURRENT_BRANCH"

echo ""
echo "✅ Updated to $(git rev-parse --short HEAD)."
echo ""
echo "💡 If Darksync is running, restart it to load the new code."
