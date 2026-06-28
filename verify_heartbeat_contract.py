#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CLIENT = os.path.join(SCRIPT_DIR, "test_client.py")
MANIFESTS = [
    os.path.join(SCRIPT_DIR, "manifest.json"),
    os.path.join(SCRIPT_DIR, "extension", "manifest.json"),
]
BACKGROUNDS = [
    os.path.join(SCRIPT_DIR, "background.js"),
    os.path.join(SCRIPT_DIR, "extension", "background.js"),
]

failed = False

def fail(message):
    global failed
    failed = True
    print("FAIL:", message)

# Manifest must grant alarms for the reconnect heartbeat and storage for the
# durable (suspension-surviving) reconnect backoff state.
for path in MANIFESTS:
    data = json.load(open(path))
    if "alarms" not in data.get("permissions", []):
        fail(f"{path} missing alarms permission")
    if "storage" not in data.get("permissions", []):
        fail(f"{path} missing storage permission")

# Background must install an alarm listener, send native keepalive traffic, and
# keep persistent debugger monitors compatible with one-shot debugger commands.
for path in BACKGROUNDS:
    text = open(path).read()
    for needle in [
        "chrome.alarms.create",
        "chrome.alarms.onAlarm.addListener",
        "heartbeat",
        "monitors",
        "chrome.debugger.onEvent.addListener",
        "Network.requestWillBeSent",
        "Runtime.consoleAPICalled",
        "RECONNECT_ALARM",
        "scheduleReconnect",
        "chrome.storage",
    ]:
        if needle not in text:
            fail(f"{path} missing {needle}")

# Wake support must give the CLI an external nudge path when the native host is
# down. The CLI cannot wake a suspended service worker through TCP, so it opens
# the extension page; the page messages the service worker, which reconnects the
# native host and closes the temporary tab.
for path in BACKGROUNDS:
    text = open(path).read()
    for needle in [
        "chrome.runtime.onMessage.addListener",
        "wakeNativeHost",
        "connectToHost",
    ]:
        if needle not in text:
            fail(f"{path} missing wake listener needle {needle}")

for path in [os.path.join(SCRIPT_DIR, "wake.html"), os.path.join(SCRIPT_DIR, "extension", "wake.html")]:
    if not os.path.exists(path):
        fail(f"{path} missing wake page")
    else:
        text = open(path).read()
        if '<script src="wake.js"></script>' not in text:
            fail(f"{path} must load external wake.js")
        if "chrome.runtime.sendMessage" in text:
            fail(f"{path} must not use inline script blocked by MV3 CSP")

for path in [os.path.join(SCRIPT_DIR, "wake.js"), os.path.join(SCRIPT_DIR, "extension", "wake.js")]:
    if not os.path.exists(path):
        fail(f"{path} missing wake script")
    else:
        text = open(path).read()
        for needle in [
            "chrome.runtime.sendMessage",
            "wakeNativeHost",
            "chrome.tabs.remove",
        ]:
            if needle not in text:
                fail(f"{path} missing wake script needle {needle}")

client_text = open(CLIENT).read()
for needle in [
    "wake_bridge_extension",
    "BRIDGE_WAKE_COMMAND",
    "wake.html",
    "extension_id.txt",
]:
    if needle not in client_text:
        fail(f"test_client.py missing wake CLI needle {needle}")

setup_text = open(os.path.join(SCRIPT_DIR, "setup.sh")).read()
if "extension_id.txt" not in setup_text:
    fail("setup.sh must persist extension_id.txt for state-dir wake installs")


def fake_server_after_wake(port, marker):
    deadline = time.monotonic() + 5
    while not os.path.exists(marker):
        if time.monotonic() > deadline:
            return
        time.sleep(0.05)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    conn, _ = server.accept()
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    conn.sendall(json.dumps({"success": True, "result": "pong"}).encode() + b"\n")
    conn.close()
    server.close()


def unused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# CLI must retry connection-refused long enough for a just-waking host.
# Start a tiny fake bridge after a delay. Without retry, chrome-bridge exits 111.
def delayed_fake_server(port):
    time.sleep(1.0)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    conn, _ = server.accept()
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    conn.sendall(json.dumps({"success": True, "result": "pong"}).encode() + b"\n")
    conn.close()
    server.close()

port = unused_port()
threading.Thread(target=delayed_fake_server, args=(port,), daemon=True).start()
env = os.environ.copy()
env["BRIDGE_PORT"] = str(port)
env["BRIDGE_CONNECT_TIMEOUT_SECONDS"] = "5"
token_fixture = "/tmp/chrome-bridge-heartbeat-token.txt"
with open(token_fixture, "w", encoding="utf-8") as f:
    f.write("heartbeat-token\n")
env["BRIDGE_TOKEN_FILE"] = token_fixture
env["BRIDGE_WAKE_DISABLED"] = "1"
proc = subprocess.run([CLIENT, "ping"], env=env, text=True, capture_output=True, timeout=10)
if proc.returncode != 0:
    fail(f"CLI did not retry refused connection until fake host started; exit={proc.returncode}, stdout={proc.stdout.strip()}, stderr={proc.stderr.strip()}")

with tempfile.TemporaryDirectory() as tmp:
    wake_marker = os.path.join(tmp, "wake-called")
    wake_cmd = os.path.join(tmp, "wake-command.py")
    with open(wake_cmd, "w", encoding="utf-8") as f:
        f.write(
            "import pathlib, sys\n"
            f"pathlib.Path({wake_marker!r}).write_text(sys.argv[1], encoding='utf-8')\n"
        )
    port = unused_port()
    threading.Thread(target=fake_server_after_wake, args=(port, wake_marker), daemon=True).start()
    env = os.environ.copy()
    env["BRIDGE_PORT"] = str(port)
    env["BRIDGE_CONNECT_TIMEOUT_SECONDS"] = "5"
    env["BRIDGE_TOKEN_FILE"] = token_fixture
    env["BRIDGE_EXTENSION_ID"] = "abcdefghijklmnopabcdefghijklmnop"
    env["BRIDGE_WAKE_COMMAND"] = f"{sys.executable} {wake_cmd}"
    proc = subprocess.run([CLIENT, "ping"], env=env, text=True, capture_output=True, timeout=10)
    if proc.returncode != 0:
        fail(
            "CLI did not invoke wake command before retrying refused connection; "
            f"exit={proc.returncode}, stdout={proc.stdout.strip()}, stderr={proc.stderr.strip()}"
        )
    elif not os.path.exists(wake_marker):
        fail("CLI succeeded without invoking wake command")
    else:
        wake_url = open(wake_marker, encoding="utf-8").read()
        if wake_url != "chrome-extension://abcdefghijklmnopabcdefghijklmnop/wake.html":
            fail(f"wake command received wrong URL: {wake_url}")

if failed:
    sys.exit(1)
print("heartbeat contract OK")
