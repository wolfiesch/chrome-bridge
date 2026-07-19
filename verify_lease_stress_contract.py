#!/usr/bin/env python3
"""Stress/race contract test for host-side lease enforcement.

Runs against the Python host and, when already built, the Rust host. A mock
extension is connected to native-messaging stdio while ten named TCP clients
race for leases and issue concurrent requests. This focuses on timing behavior
that the basic lease contract intentionally keeps simple.
"""
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CLIENT_COUNT = 10
CLIENT_NAMES = [f"client{i}" for i in range(CLIENT_COUNT)]
CLIENT_TOKENS = {name: f"tok-{name}" for name in CLIENT_NAMES}
# The main contention waves open dozens of fresh TCP connections. Two seconds
# is too tight on a cold or loaded CI runner and tests normal lease expiry rather
# than enforcement. Dedicated short-TTL cases below still cover expiry behavior.
STRESS_LEASE_TTL_MS = 10000
failures = []


def expect(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ExtensionRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self.messages = []

    def run(self, proc):
        while True:
            raw_len = proc.stdout.read(4)
            if len(raw_len) < 4:
                return
            length = struct.unpack("@I", raw_len)[0]
            msg = json.loads(proc.stdout.read(length).decode("utf-8"))
            with self._lock:
                self.messages.append(msg)
            resp = {
                "id": msg.get("id"),
                "success": True,
                "result": {"echo": msg.get("action"), "token": msg.get("token")},
            }
            enc = json.dumps(resp).encode("utf-8")
            proc.stdin.write(struct.pack("@I", len(enc)))
            proc.stdin.write(enc)
            proc.stdin.flush()

    def count(self, action=None, token=None, since=0):
        with self._lock:
            messages = list(self.messages[since:])
        return sum(
            1
            for msg in messages
            if (action is None or msg.get("action") == action)
            and (token is None or msg.get("token") == token)
        )

    def mark(self):
        with self._lock:
            return len(self.messages)


class Client:
    def __init__(self, token, port):
        self.token = token
        self.port = port
        self.buf = b""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(("127.0.0.1", port))

    def req(self, action, payload=None):
        cmd = {"action": action, "payload": payload or {}, "token": self.token}
        self.sock.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def client_for(name, port):
    return Client(CLIENT_TOKENS[name], port)


def request_once(name, port, action, payload=None):
    client = client_for(name, port)
    try:
        return client.req(action, payload)
    finally:
        client.close()


def wait_for_host(port, deadline):
    last_error = None
    while time.time() < deadline:
        try:
            client = client_for("client0", port)
            try:
                resp = client.req("leaseStatus")
                if resp and resp.get("success"):
                    return
            finally:
                client.close()
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"host did not accept TCP clients: {last_error}")


def concurrent_requests(names, port, action, payload=None, per_name=1):
    start = threading.Barrier(len(names) * per_name)
    results = []
    lock = threading.Lock()

    def worker(name):
        start.wait()
        resp = request_once(name, port, action, payload)
        with lock:
            results.append((name, resp))

    threads = [
        threading.Thread(target=worker, args=(name,))
        for name in names
        for _ in range(per_name)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    expect(all(not thread.is_alive() for thread in threads), f"{action}: all worker threads completed")
    return results


def race_for_lease(label, port, ttl_ms):
    results = concurrent_requests(CLIENT_NAMES, port, "lease", {"ttlMs": ttl_ms})
    successes = [(name, resp) for name, resp in results if resp and resp.get("success")]
    expect(len(successes) == 1, f"{label}: exactly one lease winner, got {successes}")
    if len(successes) != 1:
        return None, results
    winner = successes[0][0]
    expected_error = f"leased by {winner}"
    denials = [(name, resp) for name, resp in results if name != winner]
    expect(
        len(denials) == CLIENT_COUNT - 1
        and all(resp and resp.get("error") == expected_error for _, resp in denials),
        f"{label}: losing lease responses should be {expected_error}, got {denials}",
    )
    status = request_once(CLIENT_NAMES[(CLIENT_NAMES.index(winner) + 1) % CLIENT_COUNT], port, "leaseStatus")
    owner = (status or {}).get("result", {}).get("owner")
    expect(owner == winner, f"{label}: leaseStatus.owner should be {winner}, got {status}")
    return winner, results


def assert_release(label, port, owner):
    resp = request_once(owner, port, "release")
    expect(resp and resp.get("success") and resp.get("result", {}).get("released") is True,
           f"{label}: {owner} should release lease, got {resp}")


def drain_stderr(proc, sink):
    for line in proc.stderr:
        sink.put(line.decode("utf-8", "replace").rstrip())


def run_against(label, cmd, base_env):
    port = free_port()
    env = base_env.copy()
    env["BRIDGE_PORT"] = str(port)
    env["BRIDGE_LOG_FILE"] = str(Path(env["BRIDGE_TMPDIR"]) / f"{label}-lease-stress.log")
    env["BRIDGE_AUDIT_LOG_FILE"] = str(Path(env["BRIDGE_TMPDIR"]) / f"{label}-lease-stress-audit.jsonl")
    print(f"\n=== host: {label} ===")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    recorder = ExtensionRecorder()
    stderr_lines = queue.Queue()
    threading.Thread(target=recorder.run, args=(proc,), daemon=True).start()
    threading.Thread(target=drain_stderr, args=(proc, stderr_lines), daemon=True).start()
    try:
        wait_for_host(port, time.time() + 5)

        # 1. Ten clients race at once; exactly one wins and status reports it.
        winner, _ = race_for_lease(f"{label} race 1", port, STRESS_LEASE_TTL_MS)
        if winner is None:
            return

        # 2. Non-owners are rejected locally and never forwarded to the extension.
        before = recorder.mark()
        non_owners = [name for name in CLIENT_NAMES if name != winner]
        non_owner_wave = [non_owners[i % len(non_owners)] for i in range(50)]
        non_owner_results = concurrent_requests(non_owner_wave, port, "ping")
        expected_error = f"leased by {winner}"
        expect(
            len(non_owner_results) == 50
            and all(resp and resp.get("error") == expected_error for _, resp in non_owner_results),
            f"{label}: all non-owner pings should be rejected as {expected_error}, got {non_owner_results}",
        )
        expect(recorder.count("ping", since=before) == 0,
               f"{label}: non-owner pings must not be forwarded")

        # 3. The lease owner can send concurrent pings and all are forwarded.
        before = recorder.mark()
        owner_results = concurrent_requests([winner], port, "ping", per_name=20)
        expect(
            len(owner_results) == 20 and all(resp and resp.get("success") for _, resp in owner_results),
            f"{label}: all owner pings should succeed, got {owner_results}",
        )
        expect(recorder.count("ping", since=before) == 20,
               f"{label}: extension should see 20 forwarded owner pings")

        # 4. After release, a new ten-client race still has exactly one winner.
        assert_release(label, port, winner)
        winner2, _ = race_for_lease(f"{label} race 2 after release", port, STRESS_LEASE_TTL_MS)
        if winner2 is None:
            return
        assert_release(label, port, winner2)

        # 5. Expired leases clear before the next race.
        short_owner = CLIENT_NAMES[0]
        resp = request_once(short_owner, port, "lease", {"ttlMs": 150})
        expect(resp and resp.get("success"), f"{label}: short lease acquire should succeed, got {resp}")
        time.sleep(0.25)
        winner3, _ = race_for_lease(f"{label} race 3 after TTL expiry", port, STRESS_LEASE_TTL_MS)
        if winner3 is None:
            return
        assert_release(label, port, winner3)

        # TCP resilience: a disconnected owner keeps the lease until TTL expiry.
        holder = client_for(CLIENT_NAMES[0], port)
        resp = holder.req("lease", {"ttlMs": 250})
        expect(resp and resp.get("success"), f"{label}: TCP resilience lease acquire should succeed, got {resp}")
        holder.close()
        resp = request_once(CLIENT_NAMES[1], port, "lease", {"ttlMs": 2000})
        expect(resp and resp.get("error") == "leased by client0",
               f"{label}: closed socket must not release lease before TTL, got {resp}")
        time.sleep(0.35)
        resp = request_once(CLIENT_NAMES[1], port, "lease", {"ttlMs": 2000})
        expect(resp and resp.get("success") and resp.get("result", {}).get("owner") == "client1",
               f"{label}: another client should acquire after disconnected owner's TTL, got {resp}")
        assert_release(label, port, "client1")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.returncode not in (0, -15, -9):
            lines = []
            while not stderr_lines.empty():
                lines.append(stderr_lines.get())
            expect(False, f"{label}: host exited with {proc.returncode}; stderr tail: {lines[-10:]}")


def make_env(tmpdir):
    tmp = Path(tmpdir)
    tokens_file = tmp / "bridge_tokens.txt"
    tokens_file.write_text(
        "# name:token\n" + "".join(f"{name}:{token}\n" for name, token in CLIENT_TOKENS.items()),
        encoding="utf-8",
    )
    legacy = tmp / "bridge_token.txt"
    legacy.write_text("legacy-token\n", encoding="utf-8")
    env = os.environ.copy()
    env["BRIDGE_TOKENS_FILE"] = str(tokens_file)
    env["BRIDGE_TOKEN_FILE"] = str(legacy)
    env["BRIDGE_TMPDIR"] = str(tmp)
    return env


def rust_host_binary():
    rust_bin = SCRIPT_DIR / "host-rs" / "target" / "release" / "bridge-host"
    try:
        meta = json.loads(subprocess.check_output([
            "cargo", "metadata", "--format-version", "1", "--no-deps",
            "--manifest-path", str(SCRIPT_DIR / "host-rs" / "Cargo.toml"),
        ]))
        rust_bin = Path(meta["target_directory"]) / "release" / "bridge-host"
    except Exception:
        pass
    return rust_bin


def main():
    with tempfile.TemporaryDirectory(prefix="chrome-bridge-lease-stress-") as tmpdir:
        env = make_env(tmpdir)
        run_against("python", [sys.executable, str(SCRIPT_DIR / "bridge.py")], env)

        rust_bin = rust_host_binary()
        if rust_bin.exists():
            run_against("rust", [str(rust_bin)], env)
        else:
            print("\n(skipping rust host: binary not built)")

    if failures:
        print(f"\n{len(failures)} lease stress contract failure(s).")
        sys.exit(1)
    print("\nLease stress contract OK")


if __name__ == "__main__":
    main()
