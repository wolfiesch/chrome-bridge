#!/usr/bin/env python3
"""Offline contract test for the bridge's keep-alive (persistent) connection.

Spawns bridge.py with a mock extension wired to its stdio, opens ONE TCP
connection, and asserts that multiple newline-delimited requests are served in
order on that single socket -- including a coalesced double-frame written in a
single send(). The existing verify_bridge.py opens a fresh socket per request,
so it never exercised this path.
"""
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 9231
TOKEN = "keepalive-token"


def mock_extension(proc):
    """Echo every framed request the host forwards on stdout back on stdin,
    setting result to the request's action so the client can assert ordering."""
    while True:
        raw_len = proc.stdout.read(4)
        if len(raw_len) < 4:
            return
        length = struct.unpack("@I", raw_len)[0]
        msg = json.loads(proc.stdout.read(length).decode("utf-8"))
        resp = {"id": msg.get("id"), "success": True, "result": {"echo": msg.get("action")}}
        encoded = json.dumps(resp).encode("utf-8")
        proc.stdin.write(struct.pack("@I", len(encoded)))
        proc.stdin.write(encoded)
        proc.stdin.flush()


def recv_line(sock, buf):
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return None, buf
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return line, buf


def main():
    token_file = "/tmp/chrome-bridge-keepalive-token.txt"
    with open(token_file, "w") as f:
        f.write(TOKEN + "\n")
    env = os.environ.copy()
    env["BRIDGE_PORT"] = str(PORT)
    env["BRIDGE_TOKEN_FILE"] = token_file
    env["BRIDGE_LOG_FILE"] = "/tmp/chrome-bridge-keepalive.log"

    proc = subprocess.Popen(
        [sys.executable, os.path.join(SCRIPT_DIR, "bridge.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    threading.Thread(target=mock_extension, args=(proc,), daemon=True).start()
    time.sleep(1)

    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)
            print(f"FAIL: {msg}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(("127.0.0.1", PORT))
    buf = b""

    # Case 1: two sequential requests on the SAME socket.
    for action in ("ping", "getTabs"):
        sock.sendall((json.dumps({"action": action, "payload": {}, "token": TOKEN}) + "\n").encode())
        line, buf = recv_line(sock, buf)
        expect(line is not None, f"no response for {action} on persistent socket")
        if line:
            resp = json.loads(line.decode())
            expect(resp.get("success") is True and resp["result"]["echo"] == action,
                   f"wrong/again response for {action}: {resp}")

    # Case 2: two requests COALESCED into one send() (newline-delimited).
    coalesced = (
        json.dumps({"action": "navigate", "payload": {}, "token": TOKEN}) + "\n" +
        json.dumps({"action": "click", "payload": {}, "token": TOKEN}) + "\n"
    ).encode()
    sock.sendall(coalesced)
    for action in ("navigate", "click"):
        line, buf = recv_line(sock, buf)
        expect(line is not None, f"no response for coalesced {action}")
        if line:
            resp = json.loads(line.decode())
            expect(resp["result"]["echo"] == action, f"coalesced ordering wrong for {action}: {resp}")

    # Case 3: bad token on the persistent socket is rejected.
    sock.sendall((json.dumps({"action": "ping", "payload": {}, "token": "WRONG"}) + "\n").encode())
    line, buf = recv_line(sock, buf)
    expect(line is not None, "bad token: no response on persistent socket (expected unauthorized error)")
    if line:
        resp = json.loads(line.decode())
        expect(resp.get("success") is False and resp.get("error") == "unauthorized",
               "bad token not rejected on persistent socket")
    sock.close()

    proc.terminate()
    proc.wait()

    if failures:
        print(f"\n{len(failures)} keep-alive failure(s).")
        sys.exit(1)
    print("Keep-alive contract OK")


if __name__ == "__main__":
    main()
