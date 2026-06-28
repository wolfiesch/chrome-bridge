#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="gg.wolfie.chrome-native-bridge.broker"
HOST_IMPL="python"
PUBLIC_PORT=9223
BACKEND_PORT=19223
NO_LOAD=0
PRINT_JSON=0

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: launchd broker setup is supported only on macOS." >&2
  exit 2
fi

STATE_DIR="$HOME/Library/Application Support/chrome-native-bridge"
EXT_DIR=""
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON_BIN="$(command -v python3)"

usage() {
  echo "Usage: ./setup-broker.sh [--state-dir <path>] [--ext <extension-dir>] [--host python|rust] [--public-port <port>] [--backend-port <port>] [--no-load] [--print-json]" >&2
}

abs_path() {
  python3 - "$1" <<'PY'
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-dir)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then echo "ERROR: --state-dir requires a path" >&2; exit 2; fi
      STATE_DIR="$2"; shift 2 ;;
    --ext)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then echo "ERROR: --ext requires a path" >&2; exit 2; fi
      EXT_DIR="$2"; shift 2 ;;
    --host)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then echo "ERROR: --host requires python or rust" >&2; exit 2; fi
      HOST_IMPL="$2"; shift 2 ;;
    --public-port)
      if [[ $# -lt 2 ]]; then echo "ERROR: --public-port requires a port" >&2; exit 2; fi
      PUBLIC_PORT="$2"; shift 2 ;;
    --backend-port)
      if [[ $# -lt 2 ]]; then echo "ERROR: --backend-port requires a port" >&2; exit 2; fi
      BACKEND_PORT="$2"; shift 2 ;;
    --no-load)
      NO_LOAD=1; shift ;;
    --print-json)
      PRINT_JSON=1; shift ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "$HOST_IMPL" != "python" && "$HOST_IMPL" != "rust" ]]; then
  echo "ERROR: --host must be python or rust" >&2
  exit 2
fi

STATE_DIR="$(abs_path "$STATE_DIR")"
if [[ -z "$EXT_DIR" ]]; then
  EXT_DIR="$STATE_DIR/extension"
fi
EXT_DIR="$(abs_path "$EXT_DIR")"
PLIST="$(abs_path "$PLIST")"
PYTHON_BIN="$(abs_path "$PYTHON_BIN")"
mkdir -p "$STATE_DIR" "$(dirname "$PLIST")"
STATE_TOKEN="$STATE_DIR/bridge_token.txt"
REPO_TOKEN="$SCRIPT_DIR/bridge_token.txt"
if [[ ! -f "$STATE_TOKEN" && -f "$REPO_TOKEN" ]]; then
  cp "$REPO_TOKEN" "$STATE_TOKEN"
  chmod 600 "$STATE_TOKEN"
elif [[ -f "$STATE_TOKEN" && -f "$REPO_TOKEN" ]] && ! cmp -s "$STATE_TOKEN" "$REPO_TOKEN"; then
  echo "WARNING: $STATE_TOKEN differs from $REPO_TOKEN; clients may need BRIDGE_TOKEN_FILE=$STATE_TOKEN" >&2
fi

if [[ "$HOST_IMPL" == "python" ]]; then
  if ! SETUP_OUTPUT="$("$SCRIPT_DIR/setup.sh" --state-dir "$STATE_DIR" --ext "$EXT_DIR" --host-port "$BACKEND_PORT" --print-json 2>&1)"; then
    printf '%s\n' "$SETUP_OUTPUT" >&2
    exit 1
  fi
else
  if ! SETUP_OUTPUT="$("$SCRIPT_DIR/setup-rs.sh" --state-dir "$STATE_DIR" --ext "$EXT_DIR" --host-port "$BACKEND_PORT" --print-json 2>&1)"; then
    printf '%s\n' "$SETUP_OUTPUT" >&2
    exit 1
  fi
fi
if [[ ! -f "$REPO_TOKEN" && -f "$STATE_TOKEN" ]]; then
  cp "$STATE_TOKEN" "$REPO_TOKEN"
  chmod 600 "$REPO_TOKEN"
fi

python3 - "$PLIST" "$LABEL" "$PYTHON_BIN" "$SCRIPT_DIR/broker.py" "$STATE_DIR" "$PUBLIC_PORT" "$BACKEND_PORT" <<'PY'
import plistlib
import sys
from pathlib import Path

plist, label, python_bin, broker, state_dir, public_port, backend_port = sys.argv[1:]
data = {
    "Label": label,
    "ProgramArguments": [python_bin, broker],
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(Path(state_dir) / "broker_stdout.log"),
    "StandardErrorPath": str(Path(state_dir) / "broker_stderr.log"),
    "EnvironmentVariables": {
        "BRIDGE_BROKER_PORT": public_port,
        "BRIDGE_BACKEND_PORT": backend_port,
        "BRIDGE_TOKEN_FILE": str(Path(state_dir) / "bridge_token.txt"),
        "BRIDGE_EXTENSION_ID_FILE": str(Path(state_dir) / "extension_id.txt"),
        "BRIDGE_BROKER_LOG_FILE": str(Path(state_dir) / "broker_debug.log"),
        "BRIDGE_BROKER_BACKEND_TIMEOUT_SECONDS": "45",
        "PYTHONUNBUFFERED": "1",
    },
}
with open(plist, "wb") as f:
    plistlib.dump(data, f)
PY

echo "Wrote launchd plist $PLIST"
LOADED=false
if [[ "$NO_LOAD" -eq 0 ]]; then
  launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID" "$PLIST"
  launchctl kickstart -k "gui/$UID/$LABEL"
  LOADED=true
fi

if [[ "$PRINT_JSON" -eq 1 ]]; then
  python3 - "$LABEL" "$PLIST" "$STATE_DIR" "$EXT_DIR" "$PUBLIC_PORT" "$BACKEND_PORT" "$HOST_IMPL" "$LOADED" <<'PY'
import json, sys
keys = ("label", "plist", "stateDir", "extensionDir", "publicPort", "backendPort", "host", "loaded")
values = dict(zip(keys, sys.argv[1:]))
values["loaded"] = values["loaded"] == "true"
print(json.dumps(values, separators=(",", ":")))
PY
fi
