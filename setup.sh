#!/usr/bin/env bash
# Generate per-user secrets and register the native-messaging host.
# Safe to re-run: never overwrites an existing token.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_FILE="$SCRIPT_DIR/bridge_token.txt"
HOST_MANIFEST="$SCRIPT_DIR/com.automation.bridge.json"
TEMPLATE="$SCRIPT_DIR/com.automation.bridge.json.template"

# 1. Generate a fresh 0600 shared token if absent.
if [[ ! -f "$TOKEN_FILE" ]]; then
  python3 -c "import secrets; print(secrets.token_hex(32))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "Generated new bridge token at $TOKEN_FILE"
else
  echo "Existing bridge token kept at $TOKEN_FILE"
fi

# 2. Resolve the extension ID.
EXTENSION_ID="${1:-}"
if [[ -z "$EXTENSION_ID" ]]; then
  echo "Usage: ./setup.sh <unpacked-extension-id>"
  echo "Load ./extension in chrome://extensions (Developer mode) to get the ID, then re-run."
  exit 1
fi

# 3. Render the host manifest from the template.
sed -e "s#__BRIDGE_PY_PATH__#$SCRIPT_DIR/bridge.py#g" \
    -e "s#__EXTENSION_ID__#$EXTENSION_ID#g" \
    "$TEMPLATE" > "$HOST_MANIFEST"
echo "Wrote host manifest $HOST_MANIFEST"

# 4. Register the host for every installed Chrome/Chromium variant on this OS.
case "$(uname -s)" in
  Darwin)
    BASE="$HOME/Library/Application Support"
    HOST_DIRS=(
      "$BASE/Google/Chrome/NativeMessagingHosts"
      "$BASE/Google/Chrome Beta/NativeMessagingHosts"
      "$BASE/Google/Chrome Canary/NativeMessagingHosts"
      "$BASE/Chromium/NativeMessagingHosts"
    ) ;;
  Linux)
    HOST_DIRS=(
      "$HOME/.config/google-chrome/NativeMessagingHosts"
      "$HOME/.config/google-chrome-beta/NativeMessagingHosts"
      "$HOME/.config/chromium/NativeMessagingHosts"
    ) ;;
  *)
    echo "Unsupported OS for auto-registration; copy $HOST_MANIFEST into your browser's NativeMessagingHosts directory manually."
    exit 0 ;;
esac

chmod +x "$SCRIPT_DIR/bridge.py"
REGISTERED=0
for HOST_DIR in "${HOST_DIRS[@]}"; do
  # Register where the browser profile root already exists, plus the default Chrome dir.
  if [[ -d "$(dirname "$HOST_DIR")" || "$HOST_DIR" == *"/Google/Chrome/"* || "$HOST_DIR" == *"/google-chrome/"* ]]; then
    mkdir -p "$HOST_DIR"
    ln -sf "$HOST_MANIFEST" "$HOST_DIR/com.automation.bridge.json"
    echo "Registered native host at $HOST_DIR/com.automation.bridge.json"
    REGISTERED=$((REGISTERED + 1))
  fi
done
echo "Registered with $REGISTERED browser variant(s)."
echo "Done. Run: python3 test_client.py ping"
