#!/usr/bin/env python3
"""Offline contract test for the moat verbs (sessionStatus, waitForHandoff).

Spawns the native host with a MOCK extension wired to its stdio -- no real
Chrome -- and proves the two new verbs round-trip through the HOST verbatim.
The host's only job for these verbs is to forward them; this locks that
contract for both the Python host (bridge.py) and the Rust host (if built).

The client side reuses the repo-root test_client's send_command_data so the
read_timeout_ms transport plumbing is exercised exactly as production does it.
"""
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 9241
TOKEN = "moat-token"

# Import the repo-root test_client via importlib (mirrors how the other
# contract tests resolve SCRIPT_DIR) so we use the real send_command_data.
_spec = importlib.util.spec_from_file_location(
    "test_client", os.path.join(SCRIPT_DIR, "test_client.py"))
test_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_client)

# The handoff mock delays this long before replying; the client must use a read
# timeout well above it so the transport deadline never fires first.
HANDOFF_DELAY_MS = 300

def write_moat_policy(label):
    path = f"/tmp/chrome-bridge-moat-policy-{label}.json"
    policy = {
        "default": {
            "allowedActions": ["ping", "sessionStatus", "waitForHandoff"],
            "deniedActions": [],
            "allowedOrigins": ["*"],
            "deniedOrigins": [],
            "requireConfirmation": [],
            "redact": True,
            "audit": False,
        }
    }
    with open(path, "w") as f:
        json.dump(policy, f)
    return path



def mock_extension(proc):
    """Read 4-byte length + JSON frames the host forwards on stdout and reply on
    stdin with canned moat results keyed by the request's action."""
    while True:
        raw_len = proc.stdout.read(4)
        if len(raw_len) < 4:
            return
        length = struct.unpack("@I", raw_len)[0]
        msg = json.loads(proc.stdout.read(length).decode("utf-8"))
        action = msg.get("action")

        if action == "sessionStatus":
            result = {
                "sessions": [
                    {
                        "domain": "github.com",
                        "cookieCount": 3,
                        "cookieNames": ["user_session", "_gh_sess", "foo"],
                        "hasSessionCookie": True,
                        "loggedIn": True,
                    }
                ]
            }
        elif action == "__tabOrigin":
            result = {"url": "https://example.com/dashboard", "origin": "https://example.com"}
        elif action == "waitForHandoff":
            time.sleep(HANDOFF_DELAY_MS / 1000)
            result = {
                "handedOff": True,
                "mode": "manual",
                "elapsedMs": HANDOFF_DELAY_MS,
                "finalUrl": "https://example.com/dashboard",
            }
        else:
            result = {"echo": action}

        resp = {"id": msg.get("id"), "success": True, "result": result}
        encoded = json.dumps(resp).encode("utf-8")
        proc.stdin.write(struct.pack("@I", len(encoded)))
        proc.stdin.write(encoded)
        proc.stdin.flush()


def run_against(label, cmd, port):
    print(f"\n=== host: {label} ===")
    token_file = "/tmp/chrome-bridge-moat-token.txt"
    with open(token_file, "w") as f:
        f.write(TOKEN + "\n")
    env = os.environ.copy()
    env["BRIDGE_PORT"] = str(port)
    env["BRIDGE_TOKEN_FILE"] = token_file
    env["BRIDGE_LOG_FILE"] = f"/tmp/chrome-bridge-moat-{label}.log"
    env["BRIDGE_POLICY_FILE"] = write_moat_policy(label)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    threading.Thread(target=mock_extension, args=(proc,), daemon=True).start()
    time.sleep(1)

    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)
            print(f"FAIL: {label}: {msg}")

    # The client (test_client.send_command_data) reads BRIDGE_PORT / token from
    # the environment, so point it at this host instance.
    os.environ["BRIDGE_PORT"] = str(port)
    os.environ["BRIDGE_TOKEN_FILE"] = token_file

    # Confirm the transport plumbing for long human-handoff waits exists: the
    # function must accept read_timeout_ms so the socket deadline can be raised.
    import inspect
    sig = inspect.signature(test_client.send_command_data)
    expect("read_timeout_ms" in sig.parameters,
           "send_command_data is missing the read_timeout_ms parameter")

    try:
        # --- sessionStatus: round-trips intact, names only, no cookie values ---
        code, resp, stderr = test_client.send_command_data("sessionStatus", {})
        expect(code == 0 and resp is not None,
               f"sessionStatus failed to round-trip: code={code} stderr={stderr}")
        if resp is not None:
            expect(resp.get("success") is True, f"sessionStatus not success: {resp}")
            sessions = (resp.get("result") or {}).get("sessions")
            expect(isinstance(sessions, list) and len(sessions) == 1,
                   f"sessionStatus sessions malformed: {resp}")
            if sessions:
                s = sessions[0]
                expect(s.get("domain") == "github.com", f"wrong domain: {s}")
                expect(s.get("cookieCount") == 3, f"wrong cookieCount: {s}")
                expect(s.get("cookieNames") == ["user_session", "_gh_sess", "foo"],
                       f"wrong cookieNames: {s}")
                expect(s.get("hasSessionCookie") is True, f"hasSessionCookie wrong: {s}")
                expect(s.get("loggedIn") is True, f"loggedIn wrong: {s}")
            # Privacy contract: cookie names only -- NO 'value' key anywhere.
            expect('"value"' not in json.dumps(resp),
                   f"privacy violation: cookie value leaked in sessionStatus: {resp}")

        # --- waitForHandoff: long read timeout so transport doesn't fire first ---
        # The mock delays HANDOFF_DELAY_MS then replies; read_timeout_ms=60000
        # proves the kwarg is threaded through to the socket deadline (success
        # under a long wait is the regression check against a 15s default fire).
        code, resp, stderr = test_client.send_command_data(
            "waitForHandoff",
            {"timeoutMs": 60000, "mode": "manual"},
            read_timeout_ms=60000,
        )
        expect(code == 0 and resp is not None,
               f"waitForHandoff failed to round-trip: code={code} stderr={stderr}")
        if resp is not None:
            expect(resp.get("success") is True, f"waitForHandoff not success: {resp}")
            result = resp.get("result") or {}
            expect(result.get("handedOff") is True, f"handedOff not True: {resp}")
            expect("elapsedMs" in result, f"elapsedMs missing: {resp}")
            expect(result.get("finalUrl") == "https://example.com/dashboard",
                   f"finalUrl wrong: {resp}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if not failures:
        print(f"OK: {label} moat contract holds")
    return failures


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


def run_idle_override(label, cmd, port):
    """Prove the per-request response timeout overrides BRIDGE_SOCKET_IDLE_TIMEOUT.

    Spawns the host with a tiny 0.1s idle timeout and a mock extension that
    delays 0.5s (5x idle) before replying. A request carrying a large payload
    ``timeoutMs`` must still succeed (the host extends its response wait), while
    a request WITHOUT ``timeoutMs`` must hit the idle timeout. This locks the
    host-side plumbing that lets long human handoffs outlive the idle bound.
    """
    print(f"\n=== host idle-override: {label} ===")
    token_file = "/tmp/chrome-bridge-moat-token.txt"
    with open(token_file, "w") as f:
        f.write(TOKEN + "\n")
    env = os.environ.copy()
    env["BRIDGE_PORT"] = str(port)
    env["BRIDGE_TOKEN_FILE"] = token_file
    env["BRIDGE_LOG_FILE"] = f"/tmp/chrome-bridge-moat-idle-{label}.log"
    env["BRIDGE_SOCKET_IDLE_TIMEOUT"] = "0.1"
    env["BRIDGE_POLICY_FILE"] = write_moat_policy(f"idle-{label}")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )

    def slow_mock(p, delay_s):
        while True:
            raw_len = p.stdout.read(4)
            if len(raw_len) < 4:
                return
            length = struct.unpack("@I", raw_len)[0]
            msg = json.loads(p.stdout.read(length).decode("utf-8"))
            time.sleep(delay_s)
            resp = {"id": msg.get("id"), "success": True, "result": {"handedOff": True, "elapsedMs": int(delay_s * 1000)}}
            enc = json.dumps(resp).encode("utf-8")
            try:
                p.stdin.write(struct.pack("@I", len(enc)))
                p.stdin.write(enc)
                p.stdin.flush()
            except Exception:
                return

    threading.Thread(target=slow_mock, args=(proc, 0.5), daemon=True).start()
    time.sleep(1)

    failures = []

    def expect(cond, msg):
        if not cond:
            failures.append(msg)
            print(f"FAIL: {label}: {msg}")

    def raw_request(payload, read_to_s):
        s = socket.socket()
        s.settimeout(read_to_s)
        s.connect(("127.0.0.1", port))
        cmd_obj = {"action": "waitForHandoff", "payload": payload, "token": TOKEN}
        s.sendall((json.dumps(cmd_obj) + "\n").encode("utf-8"))
        buf = b""
        try:
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        if not buf.strip():
            return None
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))

    try:
        ok = raw_request({"timeoutMs": 5000, "mode": "manual"}, read_to_s=10)
        expect(ok is not None and ok.get("success") is True,
               f"timeoutMs request should override idle but got: {ok}")
        ctrl = raw_request({}, read_to_s=10)
        expect(ctrl is not None and ctrl.get("success") is False
               and "timeout" in str(ctrl.get("error", "")).lower(),
               f"no-timeoutMs request should hit idle timeout but got: {ctrl}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if not failures:
        print(f"OK: {label} idle-override holds")
    return failures


def main():
    py_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "bridge.py")]
    failures = run_against("python", py_cmd, PORT)
    failures += run_idle_override("python", py_cmd, PORT + 10)

    rust_bin = resolve_rust_bin()
    if os.path.exists(rust_bin):
        failures += run_against("rust", [rust_bin], PORT + 1)
        failures += run_idle_override("rust", [rust_bin], PORT + 11)
    else:
        print("\n(skipping rust host: binary not built)")

    if failures:
        print(f"\n{len(failures)} moat failure(s).")
        sys.exit(1)
    print("\nMoat contract OK (both hosts)")


if __name__ == "__main__":
    main()
