#!/bin/bash
#
# Safe deployment script for polymarket-bot
#
# This script runs regression tests and checks for active trades before
# deploying to prevent bugs and interrupting trades.
#
# Usage:
#   ./agents/skills/deploy-bot/deploy.sh              # Normal deploy
#   ./agents/skills/deploy-bot/deploy.sh --force      # Skip all safety checks
#   ./agents/skills/deploy-bot/deploy.sh --skip-tests # Skip tests only
#
# Exit codes:
#   0 - Deployment successful
#   1 - Blocked by active trades (use --force to override)
#   2 - Deployment failed
#   3 - Regression tests failed

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
FORCE=false
SKIP_TESTS=false

# Server connection details (customize for your setup)
SERVER_USER="${POLYMARKET_SERVER_USER:-unarmedpuppy}"
SERVER_HOST="${POLYMARKET_SERVER_HOST:-192.168.86.47}"
SERVER_PORT="${POLYMARKET_SERVER_PORT:-4242}"
APP_DIR="${POLYMARKET_APP_DIR:-~/server/apps/polymarket-bot}"

ssh_cmd() {
    ssh -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" "$@"
}

# Parse arguments
for arg in "$@"; do
    case $arg in
        --force|-f)
            FORCE=true
            shift
            ;;
        --skip-tests)
            SKIP_TESTS=true
            shift
            ;;
    esac
done

echo "═══════════════════════════════════════════════════════════════"
echo "  POLYMARKET BOT DEPLOYMENT"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Step 0: Run regression tests (unless --force or --skip-tests)
if [ "$FORCE" = false ] && [ "$SKIP_TESTS" = false ]; then
    echo "🧪 Running regression tests..."
    echo ""

    # Build and run tests in a fresh container
    if ! ssh_cmd "cd $APP_DIR && docker compose run --rm --build polymarket-bot python3 -m pytest tests/ -v --tb=short 2>&1 | tail -60"; then
        echo ""
        echo "═══════════════════════════════════════════════════════════════"
        echo "  ❌ DEPLOYMENT BLOCKED - REGRESSION TESTS FAILED"
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        echo "Fix the failing tests before deploying."
        echo "To skip tests (not recommended): $0 --skip-tests"
        echo "To force deploy (dangerous): $0 --force"
        echo ""
        exit 3
    fi

    echo ""
    echo "✅ Regression tests passed"
    echo ""
else
    if [ "$SKIP_TESTS" = true ]; then
        echo "⚠️  SKIP TESTS - Regression tests skipped!"
        echo ""
    fi
fi

# Step 1: Check for active trades (unless --force)
if [ "$FORCE" = false ]; then
    echo "🔍 Checking for active trades..."
    echo ""

    # Run the check via SSH
    if ! ssh_cmd "docker exec polymarket-bot python3 /app/scripts/check_active_trades.py 2>/dev/null"; then
        EXIT_CODE=$?
        if [ $EXIT_CODE -eq 1 ]; then
            echo ""
            echo "═══════════════════════════════════════════════════════════════"
            echo "  ⛔ DEPLOYMENT BLOCKED - ACTIVE TRADES DETECTED"
            echo "═══════════════════════════════════════════════════════════════"
            echo ""
            echo "Wait for trades to resolve or use --force to override:"
            echo "  $0 --force"
            echo ""
            exit 1
        elif [ $EXIT_CODE -eq 2 ]; then
            echo "⚠️  Could not check for active trades (container may not be running)"
            echo "   Proceeding with deployment..."
        fi
    fi
    echo ""
else
    echo "⚠️  FORCE MODE - Skipping active trade check!"
    echo ""
fi

# Step 2: Push local changes
echo "📤 Pushing local changes..."
cd "$REPO_ROOT"
git push origin main 2>/dev/null || true

# Step 3: Pull changes on server
echo "📥 Pulling changes on server..."
ssh_cmd "cd $APP_DIR && git pull origin main"
echo ""

# Step 4: Rebuild and restart
echo "🔄 Rebuilding and restarting container..."
ssh_cmd "cd $APP_DIR && docker compose up -d --build"
echo ""

# Step 5: Wait for startup and verify
echo "⏳ Waiting for bot to start..."
sleep 3

echo "📋 Checking bot status..."
ssh_cmd "docker logs polymarket-bot --tail 15 2>&1" || true
echo ""

echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ DEPLOYMENT COMPLETE"
echo "═══════════════════════════════════════════════════════════════"
echo ""
