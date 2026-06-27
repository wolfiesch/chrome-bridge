#!/usr/bin/env python3
"""Offline contract test for host-enforced guardrails (policy, audit, redaction).

Runs the same scenarios against the Python host (bridge.py) and, when built,
the Rust host (host-rs/target/release/bridge-host). Each scenario starts a fresh
host with its own policy file and audit log so behavior is deterministic and
isolated. A mock extension echoes forwarded requests so we can assert exactly
which actions reach the extension and what responses are redacted.

Usage:
    PYTHONDONTWRITEBYTECODE=1 ./verify_guardrails_contract.py
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
PORT = 9233

failures = []


def expect(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")


# Forwarded actions seen by the mock extension, shared per running host.
forwarded = []
forwarded_lock = threading.Lock()

# Configurable origins the mock returns for the reserved __tabOrigin lookup.
# Keyed by the request's payload tabId (int) or None for the active tab.
tab_origins = {None: "https://github.com"}


def set_tab_origins(mapping):
    global tab_origins
    tab_origins = mapping


def mock_extension(proc, result_fn):
    """Echo each forwarded request. result_fn(action, payload) -> result dict.
    The reserved __tabOrigin action is answered from ``tab_origins`` so the host
    can resolve a tab's live origin for tab-scoped policy."""
    while True:
        raw_len = proc.stdout.read(4)
        if len(raw_len) < 4:
            return
        length = struct.unpack("@I", raw_len)[0]
        msg = json.loads(proc.stdout.read(length).decode("utf-8"))
        action = msg.get("action")
        payload = msg.get("payload") or {}
        with forwarded_lock:
            forwarded.append((action, payload))
        if action == "__tabOrigin":
            origin = tab_origins.get(payload.get("tabId"))
            url = None if origin is None else origin + "/some/path"
            result = {"tabId": payload.get("tabId"), "url": url, "origin": origin}
        else:
            result = result_fn(action, payload)
        resp = {"id": msg.get("id"), "success": True, "result": result}
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


TOKENS_FILE = "/tmp/chrome-bridge-guard-tokens.txt"
LEGACY_FILE = "/tmp/chrome-bridge-guard-legacy.txt"
POLICY_FILE = "/tmp/chrome-bridge-guard-policy.json"
AUDIT_FILE = "/tmp/chrome-bridge-guard-audit.jsonl"


def write_policy(policy):
    with open(POLICY_FILE, "w") as f:
        json.dump(policy, f)


def make_env():
    with open(TOKENS_FILE, "w") as f:
        f.write("# name:token\nalpha:tok-alpha\n")
    with open(LEGACY_FILE, "w") as f:
        f.write("legacy-token\n")
    env = os.environ.copy()
    env["BRIDGE_PORT"] = str(PORT)
    env["BRIDGE_TOKENS_FILE"] = TOKENS_FILE
    env["BRIDGE_TOKEN_FILE"] = LEGACY_FILE
    env["BRIDGE_POLICY_FILE"] = POLICY_FILE
    env["BRIDGE_AUDIT_LOG_FILE"] = AUDIT_FILE
    env["BRIDGE_LOG_FILE"] = "/tmp/chrome-bridge-guard.log"
    return env


class Host:
    """A running host with a fresh policy + audit log."""

    def __init__(self, label, cmd, env, result_fn=None):
        self.label = label
        self.result_fn = result_fn or (lambda a, p: {"echo": a})
        with forwarded_lock:
            forwarded.clear()
        # Truncate the audit log for this scenario.
        open(AUDIT_FILE, "w").close()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
        threading.Thread(
            target=mock_extension, args=(self.proc, self.result_fn), daemon=True
        ).start()
        time.sleep(1)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()


def audit_events():
    try:
        with open(AUDIT_FILE) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def forwarded_actions():
    with forwarded_lock:
        return [a for a, _ in forwarded]


PERMISSIVE = {"default": {"allowedActions": ["*"], "deniedActions": [],
                          "allowedOrigins": ["*"], "deniedOrigins": [],
                          "requireConfirmation": [], "redact": True, "audit": True}}

AUDIT_KEYS = {"ts", "client", "action", "targets", "decision", "reason", "requestId"}


def run_against(label, cmd, env):
    print(f"\n=== host: {label} ===")

    # --- Denied action ---
    write_policy({"default": {"deniedActions": ["getCookies"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("getCookies", {"domain": "x.test"})
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: denied action should return policy denied, got {r}")
        expect("getCookies" not in forwarded_actions(),
               f"{label}: denied action must not forward to extension")
        c.close()

    # --- Target deny for cookies ---
    write_policy({"default": {"deniedOrigins": ["*://mail.google.com"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("getCookies", {"domain": "mail.google.com"})
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: cookie target deny should be policy denied, got {r}")
        expect("getCookies" not in forwarded_actions(),
               f"{label}: cookie target deny must not forward")
        c.close()

    # --- Target deny for downloads ---
    write_policy({"default": {"deniedOrigins": ["*://mail.google.com"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("downloadUrl", {"url": "https://mail.google.com/a/file"})
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: download target deny should be policy denied, got {r}")
        expect("downloadUrl" not in forwarded_actions(),
               f"{label}: download target deny must not forward")
        c.close()

    # --- Explicit default port preserved in targets (Python/Rust parity) ---
    write_policy({"default": {"deniedOrigins": ["*://example.com:443"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("downloadUrl", {"url": "https://example.com:443/file"})
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: explicit default port should be denied, got {r}")
        expect("downloadUrl" not in forwarded_actions(),
               f"{label}: explicit default port deny must not forward")
        c.close()

    # --- Malformed URL port must not crash the handler (fail-closed targets) ---
    write_policy(PERMISSIVE)
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("navigate", {"url": "https://example.com:99999/x"})
        expect(r and r.get("success") is True,
               f"{label}: malformed-port URL should still get a clean response, got {r}")
        expect("navigate" in forwarded_actions(),
               f"{label}: malformed-port navigate (no valid target) should forward under permissive policy")
        c.close()

    # --- Policy hot-reload ---
    write_policy(PERMISSIVE)
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("ping")
        expect(r and r.get("success"), f"{label}: ping should forward under permissive policy")
        # Rewrite to deny ping; ensure mtime advances.
        time.sleep(1.1)
        write_policy({"default": {"deniedActions": ["ping"]}})
        time.sleep(0.2)
        r = c.req("ping")
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: ping should be denied after hot-reload, got {r}")
        c.close()

    # --- Confirmation ---
    write_policy({"default": {"requireConfirmation": ["executeScript"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("executeScript", {"tabId": 1, "code": "1"})
        expect(r and r.get("confirmationRequired") is True and r.get("success") is False,
               f"{label}: executeScript should require confirmation, got {r}")
        expect("executeScript" not in forwarded_actions(),
               f"{label}: confirmation-required action must not forward")
        c.close()

    # --- Batch action denial (batch itself denied, steps not inspected) ---
    write_policy({"default": {"deniedActions": ["batch"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("batch", {"steps": [{"action": "ping", "payload": {}}]})
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: denied batch should be policy denied, got {r}")
        expect(forwarded_actions() == [],
               f"{label}: denied batch must not forward any step")
        c.close()

    # --- Batch step denial ---
    write_policy({"default": {"deniedActions": ["executeScript"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("batch", {"steps": [{"action": "executeScript", "payload": {"tabId": 1, "code": "1"}}]})
        expect(r and r.get("success") is False and "batch step 0:" in str(r.get("error", "")),
               f"{label}: batch step denial should name step 0, got {r}")
        expect(forwarded_actions() == [],
               f"{label}: batch step denial must not forward")
        c.close()

    # --- Audit ---
    write_policy({"default": {"deniedActions": ["getCookies"], "requireConfirmation": ["executeScript"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        c.req("ping")
        c.req("getCookies", {"domain": "mail.google.com"})
        c.req("executeScript", {"tabId": 1, "code": "1"})
        c.close()
        time.sleep(0.3)
        events = audit_events()
        for e in events:
            expect(set(e.keys()) == AUDIT_KEYS,
                   f"{label}: audit event keys = {sorted(e.keys())}")
            expect("payload" not in e and "response" not in e,
                   f"{label}: audit must omit payload/response")
        ping_events = [e for e in events if e["action"] == "ping"]
        expect(len(ping_events) == 2, f"{label}: ping should write 2 audit events, got {len(ping_events)}")
        if len(ping_events) == 2:
            decisions = [e["decision"] for e in ping_events]
            expect(decisions == ["allow", "extension_success"],
                   f"{label}: ping audit decisions = {decisions}")
            rids = {e["requestId"] for e in ping_events}
            expect(len(rids) == 1 and None not in rids,
                   f"{label}: ping audit requestIds should match and be non-null, got {rids}")
        cookie_events = [e for e in events if e["action"] == "getCookies"]
        expect(len(cookie_events) == 1 and cookie_events[0]["decision"] == "deny"
               and cookie_events[0]["requestId"] is None,
               f"{label}: getCookies should write 1 deny event with null requestId, got {cookie_events}")
        expect(cookie_events and cookie_events[0]["targets"] == ["*://mail.google.com"],
               f"{label}: getCookies audit targets = {cookie_events[0]['targets'] if cookie_events else None}")
        exec_events = [e for e in events if e["action"] == "executeScript"]
        expect(len(exec_events) == 1 and exec_events[0]["decision"] == "confirmation_required"
               and exec_events[0]["requestId"] is None,
               f"{label}: executeScript should write 1 confirmation_required event, got {exec_events}")

    # --- Cookie redaction ---
    write_policy(PERMISSIVE)
    cookie_result = lambda a, p: {"cookies": [{"name": "sid", "value": "secret-cookie",
                                               "domain": "x.test", "secure": True}]}
    with Host(label, cmd, env, result_fn=cookie_result):
        c = Client("tok-alpha")
        r = c.req("getCookies", {"domain": "x.test"})
        cookies = (r or {}).get("result", {}).get("cookies", [])
        expect(cookies and cookies[0].get("value") == "<redacted>",
               f"{label}: cookie value should be redacted, got {cookies}")
        expect(cookies and cookies[0].get("name") == "sid" and cookies[0].get("secure") is True,
               f"{label}: cookie metadata should be preserved, got {cookies}")
        c.close()

    # --- policyCheck ---
    write_policy({"default": {"requireConfirmation": ["executeScript"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("policyCheck", {"action": "getCookies", "payload": {"domain": "mail.google.com"}})
        res = (r or {}).get("result", {})
        expect(set(res.keys()) == {"allowed", "reason", "confirmationRequired", "redact", "audit", "originDependent"},
               f"{label}: policyCheck result keys = {sorted(res.keys())}")
        expect(res.get("allowed") is True, f"{label}: policyCheck getCookies should be allowed, got {res}")
        expect(res.get("originDependent") is False,
               f"{label}: policyCheck getCookies should not be originDependent, got {res}")
        expect("getCookies" not in forwarded_actions(),
               f"{label}: policyCheck must not forward")
        c.close()
        time.sleep(0.3)
        pc_events = [e for e in audit_events() if e["action"] == "policyCheck"]
        expect(len(pc_events) == 1 and pc_events[0]["decision"] == "allow"
               and pc_events[0]["requestId"] is None,
               f"{label}: policyCheck should write 1 allow event with null requestId, got {pc_events}")
        expect(pc_events and pc_events[0]["targets"] == ["*://mail.google.com"],
               f"{label}: policyCheck targets = {pc_events[0]['targets'] if pc_events else None}")

    # --- Reserved action rejected from socket clients ---
    write_policy(PERMISSIVE)
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("__tabOrigin", {"tabId": 1})
        expect(r and r.get("success") is False and "unknown action" in str(r.get("error", "")),
               f"{label}: reserved __tabOrigin must be rejected as unknown, got {r}")
        expect("__tabOrigin" not in forwarded_actions(),
               f"{label}: reserved action must not forward")
        time.sleep(0.3)
        re_events = [e for e in audit_events() if e["action"] == "__tabOrigin"]
        expect(len(re_events) == 1 and re_events[0]["decision"] == "deny"
               and re_events[0]["reason"] == "unknown action"
               and re_events[0]["requestId"] is None,
               f"{label}: reserved action must write 1 deny event (reason 'unknown action', null requestId), got {re_events}")
        expect(re_events and set(re_events[0].keys()) == AUDIT_KEYS,
               f"{label}: reserved deny event keys = {sorted(re_events[0].keys()) if re_events else None}")
        c.close()

    # --- Reserved action rejected as a batch step (no runBatch dispatch) ---
    write_policy(PERMISSIVE)
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("batch", {"steps": [{"action": "__tabOrigin", "payload": {"tabId": 1}}]})
        expect(r and r.get("success") is False and "batch step 0:" in str(r.get("error", "")),
               f"{label}: batch reserved step must be denied, got {r}")
        expect("__tabOrigin" not in forwarded_actions(),
               f"{label}: batch reserved step must not forward")
        c.close()

    # --- Tab-origin enforcement: deny tab-scoped action on a denied origin ---
    write_policy({"default": {"deniedOrigins": ["*://mail.google.com"]}})
    set_tab_origins({7: "https://mail.google.com"})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("click", {"tabId": 7, "selector": "#x"})
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: click on denied-origin tab should be denied, got {r}")
        expect("click" not in forwarded_actions(),
               f"{label}: denied-origin click must not forward")
        # The host did a host-internal origin lookup to make the decision.
        expect("__tabOrigin" in forwarded_actions(),
               f"{label}: host should have looked up the tab origin")
        c.close()

    # --- Tab-origin enforcement: allow tab-scoped action on an allowed origin ---
    write_policy({"default": {"deniedOrigins": ["*://mail.google.com"]}})
    set_tab_origins({7: "https://github.com"})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("click", {"tabId": 7, "selector": "#x"})
        expect(r and r.get("success") is True,
               f"{label}: click on allowed-origin tab should succeed, got {r}")
        expect("click" in forwarded_actions(),
               f"{label}: allowed-origin click should forward")
        c.close()

    # --- Tab-origin allow-list with explicit default port (parity) ---
    write_policy({"default": {"allowedOrigins": ["https://example.com:443"]}})
    set_tab_origins({7: "https://example.com:443"})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("click", {"tabId": 7, "selector": "#x"})
        expect(r and r.get("success") is True,
               f"{label}: explicit default-port origin should match allow-list, got {r}")
        c.close()

    # --- Fail closed when the tab origin cannot be resolved ---
    write_policy({"default": {"deniedOrigins": ["*://mail.google.com"]}})
    set_tab_origins({7: None})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("click", {"tabId": 7, "selector": "#x"})
        expect(r and r.get("success") is False and "tab origin unresolved" in str(r.get("error", "")),
               f"{label}: unresolved tab origin should be denied, got {r}")
        expect("click" not in forwarded_actions(),
               f"{label}: unresolved-origin click must not forward")
        c.close()
    set_tab_origins({None: "https://github.com"})

    # --- Origin-permissive policy skips the lookup round-trip ---
    write_policy(PERMISSIVE)
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("click", {"tabId": 7, "selector": "#x"})
        expect(r and r.get("success") is True,
               f"{label}: permissive policy click should succeed, got {r}")
        expect("__tabOrigin" not in forwarded_actions(),
               f"{label}: permissive policy must not trigger an origin lookup")
        c.close()

    # --- Batch tabId defaulting is origin-checked ---
    write_policy({"default": {"deniedOrigins": ["*://mail.google.com"]}})
    set_tab_origins({7: "https://mail.google.com"})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("batch", {"tabId": 7, "steps": [{"action": "click", "payload": {"selector": "#x"}}]})
        expect(r and r.get("success") is False and str(r.get("error", "")).startswith("policy denied:"),
               f"{label}: batch step inheriting denied-origin tabId should be denied, got {r}")
        expect("click" not in forwarded_actions(),
               f"{label}: denied batch step must not forward")
        c.close()
    set_tab_origins({None: "https://github.com"})

    # --- policyCheck reports originDependent for tab-scoped actions ---
    write_policy({"default": {"deniedOrigins": ["*://mail.google.com"]}})
    with Host(label, cmd, env):
        c = Client("tok-alpha")
        r = c.req("policyCheck", {"action": "click", "payload": {"tabId": 7}})
        res = (r or {}).get("result", {})
        expect(res.get("originDependent") is True,
               f"{label}: policyCheck for tab-scoped action should be originDependent, got {res}")
        expect("__tabOrigin" not in forwarded_actions(),
               f"{label}: policyCheck must not forward an origin lookup")
        c.close()

    # --- Content redaction: redactPatterns mask getHTML/extractText/script ---
    write_policy({"default": {"redactPatterns": [r"\d{3}-\d{2}-\d{4}", "(?i)bearer [a-z0-9]+"]}})
    html_result = lambda a, p: {"success": True, "html": "<p>SSN 123-45-6789</p>"} if a == "getHTML" else (
        {"success": True, "text": "auth Bearer abc123 token"} if a == "extractText" else (
        {"success": True, "val": "SSN 999-88-7777"} if a == "executeScript" else {"echo": a}))
    with Host(label, cmd, env, result_fn=html_result):
        c = Client("tok-alpha")
        r = c.req("getHTML", {"tabId": 7})
        expect(r and "<redacted>" in json.dumps(r) and "123-45-6789" not in json.dumps(r),
               f"{label}: getHTML SSN should be redacted, got {r}")
        r = c.req("extractText", {"tabId": 7})
        expect(r and "<redacted>" in json.dumps(r) and "abc123" not in json.dumps(r),
               f"{label}: extractText bearer token should be redacted, got {r}")
        r = c.req("executeScript", {"tabId": 7, "code": "1"})
        expect(r and "<redacted>" in json.dumps(r) and "999-88-7777" not in json.dumps(r),
               f"{label}: executeScript SSN should be redacted, got {r}")
        c.close()

    # --- No redactPatterns: content passes through unchanged ---
    write_policy(PERMISSIVE)
    with Host(label, cmd, env, result_fn=lambda a, p: {"success": True, "html": "<p>SSN 123-45-6789</p>"}):
        c = Client("tok-alpha")
        r = c.req("getHTML", {"tabId": 7})
        expect(r and "123-45-6789" in json.dumps(r),
               f"{label}: getHTML without redactPatterns should be unchanged, got {r}")
        c.close()


def main():
    env = make_env()
    run_against("python", [sys.executable, os.path.join(SCRIPT_DIR, "bridge.py")], env)

    rust_bin = os.path.join(SCRIPT_DIR, "host-rs", "target", "release", "bridge-host")
    try:
        meta = json.loads(subprocess.check_output(
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
        print(f"\n{len(failures)} guardrails contract failure(s).")
        sys.exit(1)
    print("\nGuardrails contract OK (both hosts)")


if __name__ == "__main__":
    main()
