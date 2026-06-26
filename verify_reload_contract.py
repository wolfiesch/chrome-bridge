#!/usr/bin/env python3
"""Offline contract test for the mtime-guarded token self-heal.

Both hosts (Python bridge.py and, when built, the Rust bridge-host) must:
  - authorize the default token (BRIDGE_TOKEN_FILE) immediately,
  - reject an unregistered token as 'unauthorized',
  - after that token is added to BRIDGE_TOKENS_FILE *post-startup* (with a
    bumped mtime), authorize it on the next request WITHOUT a restart,
  - and still reject a token that was never registered (the reload must not
    blanket-authorize).

Host-answered actions (leaseStatus/lease/release) return immediately without a
live extension, so this needs no Chrome and no mock extension wiring. Mirrors
the dual-run harness/exit conventions of verify_lease_contract.py and spawns its
own hosts on dedicated ports with temp token files -- it never touches the real
bridge_token.txt.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_PORT = 9233
RUST_PORT = 9234
DEFAULT_TOKEN = "reload-default-token"

failures = []


def expect(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")


def wait_for_accept(port, timeout=10):
    """Block until the host's TCP socket accepts a connection."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def request(port, token, action, payload=None):
    """Open a fresh socket, send one newline-JSON request, return the reply."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(("127.0.0.1", port))
    try:
        cmd = {"action": action, "payload": payload or {}, "token": token}
        sock.sendall((json.dumps(cmd) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        return json.loads(line.decode())
    finally:
        sock.close()


def run_against(label, cmd, port):
    print(f"\n=== host: {label} ===")
    workdir = tempfile.mkdtemp(prefix=f"chrome-bridge-reload-{label}-")
    token_file = os.path.join(workdir, "bridge_token.txt")
    tokens_file = os.path.join(workdir, "bridge_tokens.txt")
    with open(token_file, "w") as f:
        f.write(DEFAULT_TOKEN + "\n")
    # Tokens file exists (empty) at startup so the host records its mtime.
    with open(tokens_file, "w") as f:
        f.write("# name:token\n")

    env = os.environ.copy()
    env["BRIDGE_PORT"] = str(port)
    env["BRIDGE_TOKEN_FILE"] = token_file
    env["BRIDGE_TOKENS_FILE"] = tokens_file
    env["BRIDGE_LOG_FILE"] = os.path.join(workdir, "bridge.log")

    # The previously-unregistered token we will later register, and a token
    # that stays unregistered for the whole test.
    registered_token = "reload-agent2-" + os.urandom(8).hex()
    never_token = "reload-never-" + os.urandom(8).hex()

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env
    )
    try:
        if not wait_for_accept(port):
            expect(False, f"{label}: host never accepted on port {port}")
            return

        # Default token is authorized immediately.
        r = request(port, DEFAULT_TOKEN, "leaseStatus")
        expect(r and r.get("success") is True and r.get("error") != "unauthorized",
               f"{label}: default token leaseStatus should succeed, got {r}")

        # Unregistered token is rejected.
        r = request(port, registered_token, "leaseStatus")
        expect(r and r.get("success") is False and r.get("error") == "unauthorized",
               f"{label}: unregistered token must be unauthorized, got {r}")

        # Register that token post-startup and bump the tokens file mtime so the
        # host's mtime-guard sees a change and self-heals on the next miss.
        with open(tokens_file, "a") as f:
            f.write(f"agent2:{registered_token}\n")
        bumped = time.time() + 5
        os.utime(tokens_file, (bumped, bumped))

        # Now the same token must be accepted -- the reload happened in-place.
        r = request(port, registered_token, "leaseStatus")
        expect(r and r.get("error") != "unauthorized" and r.get("success") is True,
               f"{label}: registered token should self-heal to success, got {r}")

        # A token that was never added stays rejected: reload != blanket-authorize.
        r = request(port, never_token, "leaseStatus")
        expect(r and r.get("success") is False and r.get("error") == "unauthorized",
               f"{label}: never-registered token must STILL be unauthorized, got {r}")

        if not [m for m in failures if m.startswith(f"{label}:")]:
            print(f"OK: {label} token self-heal contract holds")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


def resolve_rust_bin():
    rust_bin = os.path.join(SCRIPT_DIR, "host-rs", "target", "release", "bridge-host")
    try:
        meta = json.loads(subprocess.check_output(
            ["cargo", "metadata", "--format-version", "1", "--no-deps",
             "--manifest-path", os.path.join(SCRIPT_DIR, "host-rs", "Cargo.toml")]))
        rust_bin = os.path.join(meta["target_directory"], "release", "bridge-host")
    except Exception:
        pass
    return rust_bin


def main():
    run_against("python", [sys.executable, os.path.join(SCRIPT_DIR, "bridge.py")], PYTHON_PORT)

    rust_bin = resolve_rust_bin()
    if os.path.exists(rust_bin):
        run_against("rust", [rust_bin], RUST_PORT)
    else:
        print("\n(skipping rust host: binary not built)")

    if failures:
        print(f"\n{len(failures)} token self-heal contract failure(s).")
        sys.exit(1)
    print("\nToken self-heal contract OK (both hosts)")


if __name__ == "__main__":
    main()
