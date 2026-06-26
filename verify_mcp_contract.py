#!/usr/bin/env python3
"""Offline contract test for the MCP server.

Stands up a mock TCP bridge (same newline-framed protocol as bridge.py),
imports the MCP tool functions, and asserts each tool emits the exact
{action, payload} the CLI sends, that an omitted tab_id resolves the active
tab, that screenshots return inline image content, and that bridge failures map
to BridgeError. No browser or real host needed.
"""
import json
import os
import socket
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

# Point the MCP package at this checkout and a fixed test port, then make it
# importable.
PORT = 9226
os.environ["BRIDGE_REPO_ROOT"] = SCRIPT_DIR
os.environ["BRIDGE_PORT"] = str(PORT)
os.environ["BRIDGE_CONNECT_TIMEOUT_SECONDS"] = "5"
TOKEN_FIXTURE = "/tmp/chrome-bridge-mcp-token.txt"
with open(TOKEN_FIXTURE, "w", encoding="utf-8") as f:
    f.write("mcp-token\n")
os.environ["BRIDGE_TOKEN_FILE"] = TOKEN_FIXTURE

sys.path.insert(0, os.path.join(SCRIPT_DIR, "mcp"))
from chrome_bridge_mcp import server  # noqa: E402
from chrome_bridge_mcp.transport import BridgeError  # noqa: E402

# Captured (action, payload) of the LAST request the mock bridge received.
received = []
received_lock = threading.Lock()


# The active result function; swap it to change mock behavior without rebinding.
_result_fn = None


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(8)
    srv.settimeout(0.5)
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if not buf.strip():
                continue
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            if req.get("token") != "mcp-token":
                conn.sendall((json.dumps({"success": False, "error": "unauthorized"}) + "\n").encode())
                continue
            action, payload = req.get("action"), req.get("payload")
            with received_lock:
                received.append((action, payload))
            try:
                result = _result_fn(action, payload)
                resp = {"success": True, "result": result}
            except Exception as exc:  # noqa: BLE001
                resp = {"success": False, "error": str(exc)}
            conn.sendall((json.dumps(resp) + "\n").encode())
    srv.close()


stop_event = threading.Event()

failures = []


def expect(cond, msg):
    if not cond:
        failures.append(msg)
        print(f"FAIL: {msg}")


def last_request():
    with received_lock:
        return received[-1] if received else (None, None)


# Default mock: active-tab tabs list, and per-action canned results.
TABS = [
    {"id": 11, "active": False, "url": "https://a.test", "title": "A"},
    {"id": 22, "active": True, "url": "https://b.test", "title": "B"},
]


def default_result(action, payload):
    if action == "getTabs":
        return TABS
    if action == "navigate":
        return {"tabId": 99}
    if action == "observe":
        return [{"role": "button", "name": "OK"}]
    if action == "extractText":
        return {"success": True, "text": "hello"}
    if action == "screenshot":
        return {"success": True, "mimeType": "image/png", "dataUrl": "data:image/png;base64,QUJD"}
    return {"success": True, "tabId": payload.get("tabId")}


def main():
    global _result_fn
    _result_fn = default_result
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(0.2)

    # 1. list_tabs -> getTabs, no payload tabId.
    server.browser_list_tabs()
    expect(last_request()[0] == "getTabs", "list_tabs should call getTabs")

    # 2. navigate -> navigate with url.
    server.browser_navigate("https://x.test")
    action, payload = last_request()
    expect(action == "navigate" and payload == {"url": "https://x.test"}, "navigate payload mismatch")

    # 3. snapshot with explicit tab_id -> observe with that tabId (no active-tab lookup).
    with received_lock:
        received.clear()
    server.browser_snapshot(tab_id=11)
    expect(last_request() == ("observe", {"tabId": 11}), "snapshot explicit tabId mismatch")
    with received_lock:
        only_observe = [a for a, _ in received] == ["observe"]
    expect(only_observe, "snapshot with explicit tabId must not call getTabs")

    # 4. snapshot with omitted tab_id -> resolves active tab (22) via getTabs.
    with received_lock:
        received.clear()
    server.browser_snapshot()
    with received_lock:
        seq = [a for a, _ in received]
    expect(seq == ["getTabs", "observe"], f"snapshot active-tab resolve sequence wrong: {seq}")
    expect(last_request() == ("observe", {"tabId": 22}), "snapshot should target active tab 22")

    # 5. extract_text default max_chars.
    server.browser_extract_text(tab_id=11)
    expect(last_request() == ("extractText", {"tabId": 11, "maxChars": 20000}), "extract_text payload mismatch")

    # 6. click / type / fill payloads.
    server.browser_click("#go", tab_id=11)
    expect(last_request() == ("click", {"tabId": 11, "selector": "#go"}), "click payload mismatch")
    server.browser_type("#q", "hello", tab_id=11)
    expect(last_request() == ("type", {"tabId": 11, "selector": "#q", "text": "hello"}), "type payload mismatch")
    server.browser_fill("#q", "hi", tab_id=11)
    expect(last_request() == ("fill", {"tabId": 11, "selector": "#q", "text": "hi"}), "fill payload mismatch")

    # 7. wait_for modes map to the right actions.
    server.browser_wait_for("load", tab_id=11)
    expect(last_request() == ("waitForLoad", {"tabId": 11, "timeoutMs": 10000}), "wait_for load mismatch")
    server.browser_wait_for("selector", tab_id=11, selector="#r", timeout_ms=2000)
    expect(last_request() == ("waitForSelector", {"tabId": 11, "selector": "#r", "timeoutMs": 2000}), "wait_for selector mismatch")
    server.browser_wait_for("text", tab_id=11, text="Done")
    expect(last_request() == ("waitForText", {"tabId": 11, "text": "Done", "timeoutMs": 10000}), "wait_for text mismatch")
    server.browser_wait_for("url", tab_id=11, url_substring="x.test")
    expect(last_request() == ("waitForUrl", {"tabId": 11, "substring": "x.test", "timeoutMs": 10000}), "wait_for url mismatch")

    # 8. tab_control ops.
    for op, act in [("activate", "activateTab"), ("close", "closeTab"), ("reload", "reload"), ("back", "goBack"), ("forward", "goForward")]:
        server.browser_tab_control(op, tab_id=11)
        expect(last_request() == (act, {"tabId": 11}), f"tab_control {op} should call {act}")

    # 9. browser_action passthrough.
    server.browser_action("performanceMetrics", {"tabId": 11})
    expect(last_request() == ("performanceMetrics", {"tabId": 11}), "browser_action passthrough mismatch")

    # 10. screenshot returns inline image content from the data URL.
    shot = server.browser_screenshot(tab_id=11)
    expect(getattr(shot, "type", None) == "image" and shot.data == "QUJD" and shot.mimeType == "image/png",
           "screenshot should return ImageContent decoded from dataUrl")

    # 11. invalid wait mode raises before any call.
    try:
        server.browser_wait_for("bogus", tab_id=11)
        expect(False, "invalid wait_for mode should raise")
    except BridgeError:
        pass

    # 12. bridge failure result maps to BridgeError (swap behavior, same server).
    def failing(action, payload):
        raise RuntimeError("boom")

    _result_fn = failing
    try:
        server.browser_click("#x", tab_id=11)
        expect(False, "bridge failure should raise BridgeError")
    except BridgeError as exc:
        expect("boom" in str(exc), "BridgeError should carry the bridge error message")
    stop_event.set()
    t.join(timeout=5)

    if failures:
        print(f"\n{len(failures)} contract failure(s).")
        sys.exit(1)
    print("MCP contract OK")


if __name__ == "__main__":
    main()
