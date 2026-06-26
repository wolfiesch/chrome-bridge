#!/usr/bin/env bash
# Sync editable source into the two INDEPENDENT live locations.
#
# These are NOT the same directory, and conflating them causes silent failures:
#
#   1. Extension dir  — the unpacked dir Chrome loads (chrome://extensions).
#      Needs ONLY background.js + manifest.json. Chrome rejects any entry whose
#      name starts with "_", so a stray __pycache__/ here breaks loading. We
#      never put bridge.py (or anything Python) here.
#
#   2. Host dir       — the path in the registered native-messaging manifest
#      (com.automation.bridge.json "path"). Chrome EXECUTES bridge.py from here,
#      and bridge.py reads bridge_token.txt next to itself. A bridge.py copied
#      anywhere else is dead code. The token must live beside it.
#
# Usage:
#   ./deploy.sh --ext <extension-dir> --host <host-dir>
#   ./deploy.sh --ext <extension-dir>     # extension only
#   ./deploy.sh --host <host-dir>         # host only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR=""
HOST_DIR=""
PRUNE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ext)   EXT_DIR="${2:-}"; shift 2 ;;
    --host)  HOST_DIR="${2:-}"; shift 2 ;;
    --prune) PRUNE=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$EXT_DIR" && -z "$HOST_DIR" ]]; then
  echo "Usage: ./deploy.sh --ext <extension-dir> --host <host-dir>" >&2
  exit 1
fi

# Syntax-check without writing bytecode. ast.parse never emits a .pyc; py_compile
# would (even under PYTHONDONTWRITEBYTECODE) and poison a Chrome-loaded dir.
check_py() { python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$1"; }

# Resolve a path to its physical absolute form (no symlinks, no trailing slash).
abspath() { python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$1"; }

if [[ -n "$EXT_DIR" ]]; then
  node --check "$SCRIPT_DIR/background.js"

  # Snapshot, BEFORE we touch the dir, whether it is already OUR extension dir.
  # This marker can't be forged by our own copy, so it survives --ext /tmp etc.
  ext_resolved="$(abspath "$EXT_DIR")"
  preexisting_ext=0
  if [[ -f "$EXT_DIR/manifest.json" ]] && grep -q "Chrome Native Messaging Automation Bridge" "$EXT_DIR/manifest.json" 2>/dev/null; then
    preexisting_ext=1
  fi

  # If pruning is requested, vet the target BEFORE writing anything, so a bad
  # --ext (typo, wrong dir) is rejected with zero side effects.
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
    # Refuse if the target also looks like a NATIVE HOST dir, regardless of
    # whether --host was passed. The install/host dir mirrors the repo layout
    # (it has a matching manifest.json), so the extension-marker check alone
    # would let --ext <host-dir> --prune delete the token and host manifest.
    for host_marker in bridge_token.txt com.automation.bridge.json extension_key.pem; do
      if [[ -e "$EXT_DIR/$host_marker" ]]; then
        echo "REFUSING to prune '$ext_resolved' (contains host marker '$host_marker'; looks like the native-host dir, not an extension-only dir)." >&2
        exit 1
      fi
    done
  fi

  mkdir -p "$EXT_DIR"
  cp "$SCRIPT_DIR/background.js" "$EXT_DIR/background.js"
  cp "$SCRIPT_DIR/manifest.json" "$EXT_DIR/manifest.json"
  node --check "$EXT_DIR/background.js"

  if [[ "$PRUNE" == "1" ]]; then
    # Targeted denylist: remove only known native-host/secret artifacts, never a
    # blanket "delete everything not allowlisted". Even if every guard above were
    # bypassed, this can't empty an arbitrary directory.
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
  echo "Extension synced to $EXT_DIR (background.js, manifest.json). Reload the extension."
fi

if [[ -n "$HOST_DIR" ]]; then
  check_py "$SCRIPT_DIR/bridge.py"
  mkdir -p "$HOST_DIR"
  cp "$SCRIPT_DIR/bridge.py" "$HOST_DIR/bridge.py"
  chmod +x "$HOST_DIR/bridge.py"
  check_py "$HOST_DIR/bridge.py"
  if [[ ! -f "$HOST_DIR/bridge_token.txt" ]]; then
    echo "WARNING: no bridge_token.txt in $HOST_DIR — bridge.py will auth as None." >&2
    echo "         Copy your token there or run ./setup.sh so the host can authenticate." >&2
  fi
  echo "Host synced to $HOST_DIR (bridge.py). Chrome respawns it on next extension reload."
fi
