#!/bin/bash
# Installs (or reinstalls) the market_check.py launchd job.
# Run manually, once, after FINNHUB_API_KEY and ANTHROPIC_API_KEY are set in .env
# and a manual `python market_check.py` run has succeeded.
set -euo pipefail

PLIST_NAME="com.aiworkflows.marketcheck.plist"
LABEL="com.aiworkflows.marketcheck"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$PROJECT_ROOT/fidelity_data/output"
"$PROJECT_ROOT/.venv/bin/python3" "$PROJECT_ROOT/generate_launchd_plist.py" \
  market-check "$DEST" --project-root "$PROJECT_ROOT"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "Installed and loaded: $DEST"
echo "Runs weekdays at 9:35am, 12:30pm, and 3:45pm local time."
echo ""
echo "Useful commands:"
echo "  launchctl list | grep marketcheck        # confirm it's loaded"
echo "  launchctl start $LABEL   # trigger a run right now, for testing"
echo "  tail -f fidelity_data/output/market_check.log        # watch stdout"
echo "  tail -f fidelity_data/output/market_check.error.log  # watch stderr"
echo "  launchctl unload ~/Library/LaunchAgents/$PLIST_NAME   # stop/remove the schedule"
