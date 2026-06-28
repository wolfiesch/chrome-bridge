#!/usr/bin/env bash
set -euo pipefail

LABEL="gg.wolfie.chrome-native-bridge.broker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
KEEP_PLIST=0

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: launchd broker setup is supported only on macOS." >&2
  exit 2
fi

usage() {
  echo "Usage: ./uninstall-broker.sh [--keep-plist]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-plist)
      KEEP_PLIST=1; shift ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true

if [[ "$KEEP_PLIST" -eq 0 ]]; then
  rm -f "$PLIST"
  echo "Removed launchd plist $PLIST"
else
  echo "Kept launchd plist $PLIST"
fi
