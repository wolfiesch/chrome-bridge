from __future__ import annotations

import os
import shlex
import subprocess
import sys


def token_file_path(script_dir: str) -> str:
    return os.environ.get("BRIDGE_TOKEN_FILE", os.path.join(script_dir, "bridge_token.txt"))


def read_first_existing(paths: list[str]) -> str | None:
    for path in paths:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except Exception:
            pass
    return None


def bridge_extension_id(script_dir: str) -> str | None:
    configured = os.environ.get("BRIDGE_EXTENSION_ID")
    if configured:
        return configured

    token_dir = os.path.dirname(os.path.abspath(os.path.expanduser(token_file_path(script_dir))))
    from_file = read_first_existing([
        os.environ.get("BRIDGE_EXTENSION_ID_FILE"),
        os.path.join(token_dir, "extension_id.txt"),
        os.path.join(script_dir, "extension_id.txt"),
    ])
    if from_file:
        return from_file

    key_files = [
        os.environ.get("BRIDGE_EXTENSION_KEY_FILE"),
        os.path.join(token_dir, "extension_key.pem"),
        os.path.join(script_dir, "extension_key.pem"),
    ]
    for key_file in key_files:
        if not key_file or not os.path.exists(key_file):
            continue
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(script_dir, "extension_identity.py"),
                    "id",
                    "--key",
                    key_file,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            continue
        extension_id = proc.stdout.strip()
        if proc.returncode == 0 and extension_id:
            return extension_id
    return None


def wake_bridge_extension(script_dir: str) -> bool:
    if os.environ.get("BRIDGE_WAKE_DISABLED") == "1":
        return False

    extension_id = bridge_extension_id(script_dir)
    if not extension_id:
        return False
    url = f"chrome-extension://{extension_id}/wake.html"

    command = os.environ.get("BRIDGE_WAKE_COMMAND")
    if command:
        argv = shlex.split(command) + [url]
    elif sys.platform == "darwin":
        bundle = os.environ.get("BRIDGE_CHROME_BUNDLE_ID", "com.google.Chrome")
        argv = ["open", "-g", "-b", bundle, url]
    else:
        argv = ["xdg-open", url]

    try:
        return subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        ).returncode == 0
    except Exception:
        return False
