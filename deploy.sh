#!/usr/bin/env bash
# Sync editable source into the two INDEPENDENT live locations.
#
# Extension dir: the unpacked dir Chrome loads. It needs background.js plus a
# manifest. Choose either a generated local keyed manifest or the public unkeyed
# source manifest explicitly.
#
# Host dir: the path in the registered native-messaging manifest. Chrome executes
# bridge.py from here. Token/policy copying is explicit and never overwrites an
# existing host policy.
#
# Usage:
#   ./deploy.sh --ext <extension-dir> --with-local-key [--key-file <path>]
#   ./deploy.sh --ext <extension-dir> --public-unkeyed
#   ./deploy.sh --host <host-dir> [--copy-token] [--copy-policy]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR=""
HOST_DIR=""
PRUNE=0
WITH_LOCAL_KEY=0
PUBLIC_UNKEYED=0
KEY_FILE="$SCRIPT_DIR/extension_key.pem"
COPY_TOKEN=0
COPY_POLICY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ext) EXT_DIR="${2:-}"; shift 2 ;;
    --host) HOST_DIR="${2:-}"; shift 2 ;;
    --prune) PRUNE=1; shift ;;
    --with-local-key) WITH_LOCAL_KEY=1; shift ;;
    --public-unkeyed) PUBLIC_UNKEYED=1; shift ;;
    --key-file) KEY_FILE="${2:-}"; shift 2 ;;
    --copy-token) COPY_TOKEN=1; shift ;;
    --copy-policy) COPY_POLICY=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$EXT_DIR" && -z "$HOST_DIR" ]]; then
  echo "Usage: ./deploy.sh --ext <extension-dir> (--with-local-key|--public-unkeyed) [--host <host-dir>]" >&2
  exit 1
fi

if [[ -n "$EXT_DIR" ]]; then
  if [[ $((WITH_LOCAL_KEY + PUBLIC_UNKEYED)) -ne 1 ]]; then
    echo "ERROR: choose exactly one extension manifest mode: --with-local-key or --public-unkeyed" >&2
    exit 1
  fi
else
  if [[ "$WITH_LOCAL_KEY" == "1" || "$PUBLIC_UNKEYED" == "1" ]]; then
    echo "ERROR: extension manifest mode requires --ext" >&2
    exit 1
  fi
fi

if [[ "$KEY_FILE" != "$SCRIPT_DIR/extension_key.pem" && "$WITH_LOCAL_KEY" != "1" ]]; then
  echo "ERROR: --key-file is valid only with --with-local-key" >&2
  exit 1
fi

check_py() { python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$1"; }
abspath() { python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1"; }

if [[ -n "$EXT_DIR" ]]; then
  node --check "$SCRIPT_DIR/background.js"

  ext_resolved="$(abspath "$EXT_DIR")"
  preexisting_ext=0
  if [[ -f "$EXT_DIR/manifest.json" ]] && grep -q "Chrome Native Messaging Automation Bridge" "$EXT_DIR/manifest.json" 2>/dev/null; then
    preexisting_ext=1
  fi

  if [[ "$PRUNE" == "1" ]]; then
    home_r="$(abspath "$HOME")"; repo_r="$(abspath "$SCRIPT_DIR")"
    host_r=""; [[ -n "$HOST_DIR" ]] && host_r="$(abspath "$HOST_DIR")"
    for forbidden in "/" "$home_r" "$repo_r" "$host_r"; do
      if [[ -n "$forbidden" && "$ext_resolved" == "$forbidden" ]]; then
        echo "REFUSING to prune '$ext_resolved' (matches $forbidden)." >&2
        exit 1
      fi
    done
    if [[ "$preexisting_ext" != "1" ]]; then
      echo "REFUSING to prune '$ext_resolved' (not pre-existing Chrome Native Bridge extension dir)." >&2
      echo "Deploy once without --prune, confirm it is the right dir, then re-run with --prune." >&2
      exit 1
    fi
    for host_marker in bridge_token.txt com.automation.bridge.json extension_key.pem; do
      if [[ -e "$EXT_DIR/$host_marker" ]]; then
        echo "REFUSING to prune '$ext_resolved' (contains host marker '$host_marker'; looks like the native-host dir, not an extension-only dir)." >&2
        exit 1
      fi
    done
  fi

  mkdir -p "$EXT_DIR"
  cp "$SCRIPT_DIR/background.js" "$EXT_DIR/background.js"
  cp "$SCRIPT_DIR/wake.html" "$EXT_DIR/wake.html"
  cp "$SCRIPT_DIR/wake.js" "$EXT_DIR/wake.js"
  if [[ "$PUBLIC_UNKEYED" == "1" ]]; then
    cp "$SCRIPT_DIR/manifest.json" "$EXT_DIR/manifest.json"
  else
    python3 "$SCRIPT_DIR/extension_identity.py" ensure --key "$KEY_FILE"
    extension_id="$(python3 "$SCRIPT_DIR/extension_identity.py" write-manifest --source "$SCRIPT_DIR/manifest.json" --output "$EXT_DIR/manifest.json" --key "$KEY_FILE")"
    echo "Extension ID: $extension_id"
  fi
  node --check "$EXT_DIR/background.js"

  if [[ "$PRUNE" == "1" ]]; then
    for junk in bridge.py bridge_token.txt test_client.py com.automation.bridge.json \
                README.md verify_bridge.py verify_cli_contract.py verify_heartbeat_contract.py \
                verify_agent_actions_live.py verify_capability_matrix.py verify_benchmark_harness.py \
                benchmark_harness.py setup.sh deploy.sh __pycache__; do
      if [[ -e "$EXT_DIR/$junk" ]]; then
        rm -rf "$EXT_DIR/$junk"
        echo "  pruned non-extension entry: $junk"
      fi
    done
    find "$EXT_DIR" -maxdepth 1 -name '*.pyc' -type f -delete 2>/dev/null || true
  fi

  if find "$EXT_DIR" -maxdepth 1 -name '_*' | grep -q .; then
    echo "ERROR: extension dir has an entry starting with '_' (Chrome will reject it):" >&2
    find "$EXT_DIR" -maxdepth 1 -name '_*' >&2
    exit 1
  fi
  echo "Extension synced to $EXT_DIR (background.js, wake.html, wake.js, manifest.json). Reload the extension."
fi

if [[ -n "$HOST_DIR" ]]; then
  check_py "$SCRIPT_DIR/bridge.py"
  mkdir -p "$HOST_DIR"
  cp "$SCRIPT_DIR/bridge.py" "$HOST_DIR/bridge.py"
  chmod +x "$HOST_DIR/bridge.py"
  check_py "$HOST_DIR/bridge.py"

  if [[ "$COPY_TOKEN" == "1" ]]; then
    if [[ -f "$SCRIPT_DIR/bridge_token.txt" ]]; then
      cp "$SCRIPT_DIR/bridge_token.txt" "$HOST_DIR/bridge_token.txt"
      chmod 600 "$HOST_DIR/bridge_token.txt"
    fi
  elif [[ ! -f "$HOST_DIR/bridge_token.txt" ]]; then
    echo "WARNING: no bridge_token.txt in $HOST_DIR — bridge.py will auth as None." >&2
    echo "         Copy your token there, pass --copy-token, or run ./setup.sh so the host can authenticate." >&2
  fi

  if [[ "$COPY_POLICY" == "1" ]]; then
    if [[ ! -f "$HOST_DIR/bridge_policy.json" ]]; then
      if [[ -f "$SCRIPT_DIR/bridge_policy.json" ]]; then
        cp "$SCRIPT_DIR/bridge_policy.json" "$HOST_DIR/bridge_policy.json"
      else
        cp "$SCRIPT_DIR/bridge_policy.example.json" "$HOST_DIR/bridge_policy.json"
      fi
    fi
    chmod 600 "$HOST_DIR/bridge_policy.json"
  elif [[ ! -f "$HOST_DIR/bridge_policy.json" ]]; then
    echo "WARNING: no bridge_policy.json in $HOST_DIR; fail-closed defaults allow only ping/policyCheck/lease operations" >&2
  fi

  echo "Host synced to $HOST_DIR (bridge.py). Chrome respawns it on next extension reload."
fi
