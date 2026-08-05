#!/bin/bash
# Installs (or reinstalls) the market_check.py launchd job.
# Run manually, once, after FINNHUB_API_KEY and ANTHROPIC_API_KEY are set in .env
# and a manual `python market_check.py` run has succeeded.
set -euo pipefail

PLIST_NAME="com.rajeevsingh.marketcheck.plist"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$PLIST_NAME"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$(dirname "$0")/fidelity_data/output"

cp "$SRC" "$DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "Installed and loaded: $DEST"
echo "Runs weekdays at 9:35am, 12:30pm, and 3:45pm local time."
echo ""
echo "Useful commands:"
echo "  launchctl list | grep marketcheck        # confirm it's loaded"
echo "  launchctl start com.rajeevsingh.marketcheck   # trigger a run right now, for testing"
echo "  tail -f fidelity_data/output/market_check.log        # watch stdout"
echo "  tail -f fidelity_data/output/market_check.error.log  # watch stderr"
echo "  launchctl unload ~/Library/LaunchAgents/$PLIST_NAME   # stop/remove the schedule"
