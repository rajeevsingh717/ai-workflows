#!/bin/bash
# Installs (or reinstalls) the telegram_listener.py launchd daemon.
# This is a persistent background listener (not a scheduled job) — it starts
# when loaded (and at every login) and launchd restarts it automatically if
# it ever exits or crashes.
set -euo pipefail

PLIST_NAME="com.rajeevsingh.telegramlistener.plist"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$PLIST_NAME"
DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$(dirname "$0")/fidelity_data/output"

cp "$SRC" "$DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "Installed and loaded: $DEST"
echo "Runs continuously in the background, listening for Telegram messages."
echo ""
echo "Useful commands:"
echo "  launchctl list | grep telegramlistener        # confirm it's running"
echo "  tail -f fidelity_data/output/telegram_listener.log        # watch activity"
echo "  tail -f fidelity_data/output/telegram_listener.error.log  # watch errors"
echo "  launchctl unload ~/Library/LaunchAgents/$PLIST_NAME   # stop it"
