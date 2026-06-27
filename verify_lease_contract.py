#!/usr/bin/env python3
"""Offline contract test for per-client tokens + host-side leasing.

Runs against BOTH hosts (Python bridge.py and the Rust binary, when built) to
prove parity. A mock extension is wired to the host's stdio; two named clients
(alpha, beta) connect over separate persistent sockets using distinct tokens
from a BRIDGE_TOKENS_FILE registry. Asserts: named-token identity, lease
acquire / deny-by-other / release / TTL expiry, leaseStatus schema parity, and
that a non-owner `batch` (a potential lease bypass) is blocked. No browser.
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
PORT = 9232

failures = []


def expect(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")


def mock_extension(proc):
    """Echo each forwarded request back on stdin: result = {"echo": action}."""
    while True:
        raw_len = proc.stdout.read(4)
        if len(raw_len) < 4:
            return
        length = struct.unpack("@I", raw_len)[0]
        msg = json.loads(proc.stdout.read(length).decode("utf-8"))
        resp = {"id": msg.get("id"), "success": True, "result": {"echo": msg.get("action")}}
        enc = json.dumps(resp).encode("utf-8")
        proc.stdin.write(struct.pack("@I", len(enc)))
        proc.stdin.write(enc)
        proc.stdin.flush()


class Client:
    def __init__(self, token):
        self.token = token
        self.buf = b""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(("127.0.0.1", PORT))

    def req(self, action, payload=None):
        cmd = {"action": action, "payload": payload or {}, "token": self.token}
        self.sock.sendall((json.dumps(cmd) + "\n").encode())
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode())

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def run_against(label, cmd, env):
    print(f"\n=== host: {label} ===")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    threading.Thread(target=mock_extension, args=(proc,), daemon=True).start()
    time.sleep(1)

    alpha = Client("tok-alpha")
    beta = Client("tok-beta")
    try:
        # Unknown token rejected.
        bad = Client("tok-unknown")
        r = bad.req("ping")
        expect(r and r.get("error") == "unauthorized", f"{label}: unknown token must be unauthorized")
        bad.close()

        # Named identity works (both tokens authorized, forwarded to extension).
        r = alpha.req("ping")
        expect(r and r.get("success") and r["result"]["echo"] == "ping", f"{label}: alpha ping should round-trip")

        # leaseStatus schema: owner/expiresAt/now keys, owner null when unheld.
        r = alpha.req("leaseStatus")
        res = (r or {}).get("result", {})
        expect(set(res.keys()) == {"owner", "expiresAt", "now"}, f"{label}: leaseStatus keys = {sorted(res.keys())}")
        expect(res.get("owner") is None, f"{label}: owner should be null when unheld")

        # alpha acquires the lease.
        r = alpha.req("lease", {"ttlMs": 5000})
        expect(r and r.get("success") and r["result"]["owner"] == "alpha", f"{label}: alpha should acquire lease")

        # beta is denied a non-lease action AND a batch (lease-bypass guard).
        r = beta.req("ping")
        expect(r and r.get("error") == "leased by alpha", f"{label}: beta ping must be blocked, got {r}")
        r = beta.req("batch", {"steps": [{"action": "click", "payload": {"tabId": 1, "selector": "#x"}}]})
        expect(r and r.get("error") == "leased by alpha", f"{label}: beta batch must be blocked (no bypass), got {r}")

        # beta cannot acquire or release alpha's lease.
        r = beta.req("lease")
        expect(r and r.get("error") == "leased by alpha", f"{label}: beta lease must be denied")
        r = beta.req("release")
        expect(r and r.get("error") == "not lease owner", f"{label}: beta release must be denied, got {r}")

        # owner still works while holding the lease.
        r = alpha.req("ping")
        expect(r and r.get("success"), f"{label}: alpha should still act while holding lease")

        # leaseStatus reports alpha as owner.
        r = beta.req("leaseStatus")
        expect((r or {}).get("result", {}).get("owner") == "alpha", f"{label}: status should show alpha owner")

        # alpha releases; beta can now act and acquire.
        r = alpha.req("release")
        expect(r and r["result"]["released"] is True, f"{label}: alpha release should succeed")
        r = beta.req("ping")
        expect(r and r.get("success"), f"{label}: beta should act after release")

        # TTL expiry: alpha takes a short lease; after it expires beta proceeds.
        alpha.req("lease", {"ttlMs": 300})
        r = beta.req("ping")
        expect(r and r.get("error") == "leased by alpha", f"{label}: beta blocked during alpha TTL")
        time.sleep(0.5)
        r = beta.req("ping")
        expect(r and r.get("success"), f"{label}: beta should proceed after alpha TTL expiry")
        # release=false when no live lease.
        r = alpha.req("release")
        expect(r and r["result"]["released"] is False, f"{label}: release with no live lease should be released=false")
    finally:
        alpha.close()
        beta.close()
        proc.terminate()
        proc.wait()


def make_env():
    tokens_file = "/tmp/chrome-bridge-tokens.txt"
    with open(tokens_file, "w") as f:
        f.write("# name:token\nalpha:tok-alpha\nbeta:tok-beta\n")
    # Legacy single token must still load; point it at an unused fixture.
    legacy = "/tmp/chrome-bridge-legacy-token.txt"
    with open(legacy, "w") as f:
        f.write("legacy-token\n")
    env = os.environ.copy()
    env["BRIDGE_PORT"] = str(PORT)
    env["BRIDGE_TOKENS_FILE"] = tokens_file
    env["BRIDGE_TOKEN_FILE"] = legacy
    env["BRIDGE_LOG_FILE"] = "/tmp/chrome-bridge-lease.log"
    policy = "/tmp/chrome-bridge-lease-policy.json"
    with open(policy, "w") as f:
        json.dump({
            "default": {
                "allowedActions": ["ping", "batch", "click", "lease", "release", "leaseStatus"],
                "allowedOrigins": ["*"],
                "deniedActions": [],
                "deniedOrigins": [],
                "requireConfirmation": [],
                "redact": True,
                "audit": False,
            }
        }, f)
    env["BRIDGE_POLICY_FILE"] = policy
    return env


def main():
    env = make_env()
    run_against("python", [sys.executable, os.path.join(SCRIPT_DIR, "bridge.py")], env)

    rust_bin = os.path.join(SCRIPT_DIR, "host-rs", "target", "release", "bridge-host")
    try:
        import subprocess as sp
        meta = json.loads(sp.check_output(
            ["cargo", "metadata", "--format-version", "1", "--no-deps",
             "--manifest-path", os.path.join(SCRIPT_DIR, "host-rs", "Cargo.toml")]))
        rust_bin = os.path.join(meta["target_directory"], "release", "bridge-host")
    except Exception:
        pass
    if os.path.exists(rust_bin):
        run_against("rust", [rust_bin], env)
    else:
        print("\n(skipping rust host: binary not built)")

    if failures:
        print(f"\n{len(failures)} lease/token contract failure(s).")
        sys.exit(1)
    print("\nLease/token contract OK (both hosts)")


if __name__ == "__main__":
    main()
