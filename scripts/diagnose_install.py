#!/usr/bin/env python3
"""Report bridge installation drift and live connection state without waking Chrome."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get(
    "BRIDGE_STATE_DIR",
    "~/Library/Application Support/chrome-native-bridge",
)).expanduser()


def digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def listening(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def manifest_version(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def effective_native_host() -> Path:
    manifest = Path(
        os.environ.get(
            "BRIDGE_NATIVE_MANIFEST",
            "~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.automation.bridge.json",
        )
    ).expanduser()
    try:
        launcher = Path(json.loads(manifest.read_text(encoding="utf-8"))["path"])
        launcher_text = launcher.read_text(encoding="utf-8")
    except (OSError, KeyError, ValueError):
        return STATE / "bridge.py"
    repo_host = REPO / "bridge.py"
    if str(repo_host) in launcher_text:
        return repo_host
    state_host = STATE / "bridge.py"
    if str(state_host) in launcher_text:
        return state_host
    return launcher


def main() -> int:
    repo_background = digest(REPO / "extension" / "background.js")
    deployed_background = digest(STATE / "extension" / "background.js")
    repo_host = digest(REPO / "bridge.py")
    native_host = effective_native_host()
    deployed_host = digest(native_host)
    report = {
        "repository": str(REPO),
        "stateDir": str(STATE),
        "effectiveNativeHost": str(native_host),
        "versions": {
            "repository": manifest_version(REPO / "manifest.json"),
            "deployed": manifest_version(STATE / "extension" / "manifest.json"),
        },
        "filesCurrent": {
            "extension": bool(repo_background and repo_background == deployed_background),
            "nativeHost": bool(repo_host and repo_host == deployed_host),
        },
        "connections": {
            "broker9223": listening(int(os.environ.get("BRIDGE_BROKER_PORT", "9223"))),
            "nativeBackend19223": listening(int(os.environ.get("BRIDGE_BACKEND_PORT", "19223"))),
        },
    }
    problems = []
    if not report["filesCurrent"]["extension"]:
        problems.append("deployed extension differs from repository")
    if not report["filesCurrent"]["nativeHost"]:
        problems.append("deployed native host differs from repository")
    if report["connections"]["broker9223"] and not report["connections"]["nativeBackend19223"]:
        problems.append("broker is running but Chrome native backend is disconnected")
    report["problems"] = problems
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
