#!/usr/bin/env python3
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CLIENT = os.path.join(SCRIPT_DIR, "test_client.py")
ENV = os.environ.copy()
# Port 9 should be closed; recognized commands should attempt a connection and
# return 111. Unknown actions should return 64. This lets us test CLI dispatch
# without needing a live Chrome extension.
ENV["BRIDGE_PORT"] = "9"
ENV["BRIDGE_CONNECT_TIMEOUT_SECONDS"] = "0"
TOKEN_FIXTURE = "/tmp/chrome-bridge-contract-token.txt"
with open(TOKEN_FIXTURE, "w", encoding="utf-8") as f:
    f.write("contract-token\n")
ENV["BRIDGE_TOKEN_FILE"] = TOKEN_FIXTURE


UPLOAD_FIXTURE = "/tmp/chrome-bridge-upload.txt"
with open(UPLOAD_FIXTURE, "w", encoding="utf-8") as f:
    f.write("chrome bridge upload fixture\n")

CASES = [
    (["ping"], 111),
    (["navigate", "https://example.com"], 111),
    (["getCookies", "example.com"], 111),
    (["executeScript", "1", "document.title"], 111),
    (["getTabs"], 111),
    (["executeScriptCDP", "1", "document.title"], 111),
    (["click", "1", "#submit"], 111),
    (["type", "1", "input[name=q]", "hello"], 111),
    (["observe", "1"], 111),
    (["activateTab", "1"], 111),
    (["closeTab", "1"], 111),
    (["reload", "1"], 111),
    (["goBack", "1"], 111),
    (["goForward", "1"], 111),
    (["waitForLoad", "1", "5000"], 111),
    (["waitForSelector", "1", "#ready", "5000"], 111),
    (["waitForText", "1", "Loaded", "5000"], 111),
    (["waitForUrl", "1", "github.com", "5000"], 111),
    (["getCurrentState", "1"], 111),
    (["screenshot", "1", "/tmp/chrome-bridge-shot.png"], 111),
    (["extractText", "1", "2000"], 111),
    (["getHTML", "1", "/tmp/chrome-bridge-page.html"], 111),
    (["hover", "1", "#target"], 111),
    (["scroll", "1", "0", "500"], 111),
    (["scroll", "1", "0", "500", "#panel"], 111),
    (["press", "1", "Enter"], 111),
    (["drag", "1", "#from", "#to"], 111),
    (["fill", "1", "input[name=q]", "hello"], 111),
    (["select", "1", "select[name=kind]", "beta"], 111),
    (["uploadFile", "1", "input[type=file]", UPLOAD_FIXTURE], 111),
    (["setViewport", "1", "1280", "720", "1"], 111),
    (["startMonitoring", "1"], 111),
    (["stopMonitoring", "1"], 111),
    (["consoleMessages", "1"], 111),
    (["networkRequests", "1"], 111),
    (["handleDialog", "1", "accept"], 111),
    (["handleDialog", "1", "accept", "typed prompt value"], 111),
    (["startInterception", "1", "*api*", "continue"], 111),
    (["startInterception", "1", "*api*", "abort"], 111),
    (["startInterception", "1", "*api*", "fulfill", "200", '{"data":1}'], 111),
    (["stopInterception", "1"], 111),
    (["interceptedRequests", "1"], 111),
    (["downloadUrl", "https://example.com/file.zip"], 111),
    (["downloadUrl", "https://example.com/file.zip", "my_file.zip"], 111),
    (["storageState", "1", "/tmp/chrome-bridge-state.json"], 111),
    (["setGeolocation", "1", "37.7749", "-122.4194"], 111),
    (["setGeolocation", "1", "37.7749", "-122.4194", "100"], 111),
    (["clearGeolocation", "1"], 111),
    (["performanceMetrics", "1"], 111),
    (["noSuchAction"], 64),
]

failed = False
for args, expected in CASES:
    proc = subprocess.run([CLIENT, *args], env=ENV, text=True, capture_output=True)
    if proc.returncode != expected:
        failed = True
        print(f"FAIL {' '.join(args)}: expected exit {expected}, got {proc.returncode}")
        if proc.stdout:
            print("stdout:", proc.stdout.strip())
        if proc.stderr:
            print("stderr:", proc.stderr.strip())

missing = "/tmp/chrome-bridge-upload-missing.txt"
try:
    os.unlink(missing)
except FileNotFoundError:
    pass
proc = subprocess.run(
    [CLIENT, "uploadFile", "1", "input[type=file]", missing],
    env=ENV,
    text=True,
    capture_output=True,
)
if proc.returncode != 2 or "Upload file not found:" not in proc.stderr:
    failed = True
    print(f"FAIL uploadFile missing preflight: expected exit 2 and missing-file stderr, got {proc.returncode}")
    if proc.stdout:
        print("stdout:", proc.stdout.strip())
    if proc.stderr:
        print("stderr:", proc.stderr.strip())

# Offline dispatch assertions: import the client module and monkeypatch
# send_command_data so we can observe the exact action/payload/read_timeout_ms
# produced by main() without needing a live bridge.
import importlib.util

_spec = importlib.util.spec_from_file_location("test_client", CLIENT)
test_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_client)

captured = {}

def _fake_send_command_data(action, payload=None, read_timeout_ms=None):
    captured["action"] = action
    captured["payload"] = payload
    captured["read_timeout_ms"] = read_timeout_ms
    # Mimic a successful response so send_command returns exit code 0.
    return 0, {"success": True}, ""

test_client.send_command_data = _fake_send_command_data

def dispatch(argv):
    captured.clear()
    saved_argv = sys.argv
    sys.argv = [CLIENT, *argv]
    try:
        test_client.main()
    except SystemExit:
        pass
    finally:
        sys.argv = saved_argv
    return dict(captured)

def check(name, got, expected):
    global failed
    if got != expected:
        failed = True
        print(f"FAIL {name}: expected {expected}, got {got}")

result = dispatch(["sessionStatus", "a.com", "b.com"])
check("sessionStatus action", result.get("action"), "sessionStatus")
check("sessionStatus payload", result.get("payload"), {"domains": ["a.com", "b.com"]})

result = dispatch(["waitForHandoff", "please log in", "selector", "#ok", "60000"])
check("waitForHandoff action", result.get("action"), "waitForHandoff")
check(
    "waitForHandoff payload",
    result.get("payload"),
    {"message": "please log in", "until": {"mode": "selector", "selector": "#ok"}, "timeoutMs": 60000},
)
check("waitForHandoff read_timeout_ms", result.get("read_timeout_ms"), 60000)

if failed:
    sys.exit(1)
print("CLI contract OK")
