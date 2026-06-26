#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import sys
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

port = 9231
threading.Thread(target=delayed_fake_server, args=(port,), daemon=True).start()
env = os.environ.copy()
env["BRIDGE_PORT"] = str(port)
env["BRIDGE_CONNECT_TIMEOUT_SECONDS"] = "5"
token_fixture = "/tmp/chrome-bridge-heartbeat-token.txt"
with open(token_fixture, "w", encoding="utf-8") as f:
    f.write("heartbeat-token\n")
env["BRIDGE_TOKEN_FILE"] = token_fixture
proc = subprocess.run([CLIENT, "ping"], env=env, text=True, capture_output=True, timeout=10)
if proc.returncode != 0:
    fail(f"CLI did not retry refused connection until fake host started; exit={proc.returncode}, stderr={proc.stderr.strip()}")

if failed:
    sys.exit(1)
print("heartbeat contract OK")
