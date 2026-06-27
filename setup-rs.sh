#!/usr/bin/env bash
# Generate per-user secrets, deploy a deterministic local extension, and
# register the Rust native-messaging host. Safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_FILE="$SCRIPT_DIR/bridge_token.txt"
POLICY_FILE="$SCRIPT_DIR/bridge_policy.json"
HOST_MANIFEST="$SCRIPT_DIR/com.automation.bridge.rust.json"
TEMPLATE="$SCRIPT_DIR/com.automation.bridge.json.template"
RUST_BIN="$SCRIPT_DIR/host-rs/target/release/bridge-host"
KEY_FILE="$SCRIPT_DIR/extension_key.pem"
EXTENSION_ID=""

if command -v cargo >/dev/null 2>&1; then
  TARGET_DIR="$(cargo metadata --format-version 1 --no-deps \
    --manifest-path "$SCRIPT_DIR/host-rs/Cargo.toml" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["target_directory"])' 2>/dev/null || true)"
  if [[ -n "${TARGET_DIR:-}" ]]; then
    RUST_BIN="$TARGET_DIR/release/bridge-host"
  fi
fi

case "$(uname -s)" in
  Darwin) EXT_DIR="$HOME/Library/Application Support/chrome-native-bridge/extension" ;;
  Linux) EXT_DIR="$HOME/.local/share/chrome-native-bridge/extension" ;;
  *) EXT_DIR="$SCRIPT_DIR/extension" ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ext) EXT_DIR="${2:-}"; shift 2 ;;
    --extension-id) EXTENSION_ID="${2:-}"; shift 2 ;;
    --key-file) KEY_FILE="${2:-}"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; echo "Usage: ./setup-rs.sh [--ext <extension-dir>] [--extension-id <id>] [--key-file <path>]" >&2; exit 1 ;;
  esac
done

if [[ ! -f "$TOKEN_FILE" ]]; then
  python3 -c "import secrets; print(secrets.token_hex(32))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "Generated new bridge token at $TOKEN_FILE"
else
  echo "Existing bridge token kept at $TOKEN_FILE"
fi

if [[ ! -f "$POLICY_FILE" ]]; then
  cp "$SCRIPT_DIR/bridge_policy.example.json" "$POLICY_FILE"
  chmod 600 "$POLICY_FILE"
  echo "Installed default bridge policy at $POLICY_FILE"
else
  echo "Existing bridge_policy.json kept"
fi

if [[ -z "$EXTENSION_ID" ]]; then
  "$SCRIPT_DIR/deploy.sh" --ext "$EXT_DIR" --with-local-key --key-file "$KEY_FILE"
  EXTENSION_ID="$(python3 "$SCRIPT_DIR/extension_identity.py" id --key "$KEY_FILE")"
else
  echo "Using provided extension ID: $EXTENSION_ID"
fi

if [[ ! -x "$RUST_BIN" ]]; then
  echo "Build the Rust host first: cargo build --release --manifest-path host-rs/Cargo.toml"
  exit 1
fi

LAUNCHER="$SCRIPT_DIR/bridge-host-launch.sh"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
export BRIDGE_TOKEN_FILE="\${BRIDGE_TOKEN_FILE:-$SCRIPT_DIR/bridge_token.txt}"
export BRIDGE_TOKENS_FILE="\${BRIDGE_TOKENS_FILE:-$SCRIPT_DIR/bridge_tokens.txt}"
export BRIDGE_LOG_FILE="\${BRIDGE_LOG_FILE:-$SCRIPT_DIR/bridge_debug.log}"
export BRIDGE_POLICY_FILE="\${BRIDGE_POLICY_FILE:-$SCRIPT_DIR/bridge_policy.json}"
export BRIDGE_AUDIT_LOG_FILE="\${BRIDGE_AUDIT_LOG_FILE:-$SCRIPT_DIR/bridge_audit.jsonl}"
exec "$RUST_BIN" "\$@"
EOF
chmod +x "$LAUNCHER"
echo "Wrote launcher $LAUNCHER"

python3 - "$TEMPLATE" "$HOST_MANIFEST" "$LAUNCHER" "$EXTENSION_ID" <<'PY'
import sys
from pathlib import Path
template, out, bridge, ext_id = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
text = text.replace("__BRIDGE_PY_PATH__", bridge).replace("__EXTENSION_ID__", ext_id)
Path(out).write_text(text, encoding="utf-8")
PY
echo "Wrote host manifest $HOST_MANIFEST"

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
    echo "Load unpacked: $EXT_DIR"
    echo "Then run: python3 test_client.py ping"
    exit 0 ;;
esac

REGISTERED=0
for HOST_DIR in "${HOST_DIRS[@]}"; do
  if [[ -d "$(dirname "$HOST_DIR")" || "$HOST_DIR" == *"/Google/Chrome/"* || "$HOST_DIR" == *"/google-chrome/"* ]]; then
    mkdir -p "$HOST_DIR"
    ln -sf "$HOST_MANIFEST" "$HOST_DIR/com.automation.bridge.json"
    echo "Registered native host at $HOST_DIR/com.automation.bridge.json"
    REGISTERED=$((REGISTERED + 1))
  fi
done

echo "Registered with $REGISTERED browser variant(s)."
echo "Load unpacked: $EXT_DIR"
echo "Then run: python3 test_client.py ping"
