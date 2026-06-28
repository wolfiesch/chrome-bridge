#!/usr/bin/env python3
import contextlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
BASE_URL = f"http://{HOST}:{PORT}/"
UPLOAD_FIXTURE = "/tmp/chrome-bridge-live-upload.txt"
SHOT_PATH = "/tmp/chrome-bridge-live.png"
HTML_PATH = "/tmp/chrome-bridge-live.html"
SCRIPT_DIR = Path(__file__).resolve().parent
def resolve_policy_path():
    p_host = None
    resolved_from_host = False
    try:
        proc = subprocess.run(["chrome-bridge", "policy", "info"], text=True, capture_output=True, timeout=5)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            p_file = (data.get("result") or {}).get("policyFile")
            if p_file:
                p_host = Path(p_file)
                resolved_from_host = True
    except Exception as e:
        sys.stderr.write(f"RESOLVER WARNING: failed to query host policy path: {e}\n")

    p_env = os.environ.get("BRIDGE_POLICY_FILE")
    if p_env:
        p_env_path = Path(p_env)
        if p_host and p_host.resolve() != p_env_path.resolve():
            raise AssertionError(
                f"Mismatch between environment BRIDGE_POLICY_FILE ({p_env_path}) "
                f"and active host policy file ({p_host})"
            )
        return p_env_path, resolved_from_host

    if p_host:
        sys.stderr.write(f"RESOLVED ACTIVE POLICY FILE PATH FROM HOST: {p_host}\n")
        return p_host, True

    return SCRIPT_DIR / "bridge_policy.json", False

def restore_policy(backup, policy_path, backup_mode=None):
    if backup is None:
        with contextlib.suppress(FileNotFoundError):
            policy_path.unlink()
    else:
        policy_path.write_bytes(backup)
        if backup_mode is not None:
            try:
                os.chmod(policy_path, backup_mode)
            except OSError:
                pass

PAGE = b"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Chrome Bridge Live Test</title>
  <style>
    body { font-family: system-ui, sans-serif; min-height: 1600px; }
    #from, #to { width: 80px; height: 40px; margin: 20px; padding: 8px; border: 1px solid #333; }
    #to { margin-top: 300px; }
    #panel { height: 120px; overflow: auto; border: 1px solid #999; }
    #spacer { height: 600px; }
  </style>
</head>
<body>
  <h1>Chrome Bridge Live Test</h1>
  <input id="q" name="q" value="">
  <button id="btn">Click me</button>
  <button id="log">Log</button>
  <button id="fetch">Fetch</button>
  <button id="alert">Alert</button>
  <select id="kind" name="kind"><option value="alpha">Alpha</option><option value="beta">Beta</option></select>
  <input id="file" type="file">
  <div id="status">ready</div>
  <div id="from" draggable="true">from</div>
  <div id="to">to</div>
  <div id="panel"><div id="spacer">scroll panel</div></div>
  <script>
    document.querySelector('#btn').addEventListener('click', () => {
      document.querySelector('#status').textContent = 'clicked:' + document.querySelector('#q').value;
    });
    document.querySelector('#log').addEventListener('click', () => console.log('bridge fixture console message'));
    document.querySelector('#fetch').addEventListener('click', () => fetch('/data.json?secret=redact-me').then(r => r.json()).then(d => console.log('fetch', d.ok)));
    document.querySelector('#alert').addEventListener('click', () => alert('hello dialog'));
  </script>
</body>
</html>"""

DATA = b'{"ok": true}'


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data.json"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(DATA)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, _format, *_args):
        return


class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def start_server():
    server = ReusableThreadingHTTPServer((HOST, PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_bridge(*args, timeout=20):
    for arg in args:
        if isinstance(arg, int) and arg > 1000:
            timeout = max(timeout, int(arg / 1000) + 15)
    proc = subprocess.run(["chrome-bridge", *map(str, args)], text=True, capture_output=True, timeout=timeout)
    parsed = None
    if proc.stdout.strip():
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(proc.stdout)
    return {
        "args": list(map(str, args)),
        "exit": proc.returncode,
        "json": parsed,
        "stderr": proc.stderr.strip(),
    }


def result(call):
    data = call.get("json") or {}
    item = data.get("result")
    return item if isinstance(item, dict) else item


def record(summary, name, call, extra=None):
    entry = {"exit": call["exit"]}
    if extra:
        entry.update(extra)
    if call["exit"] != 0 and call.get("stderr"):
        entry["stderr"] = call["stderr"]
    summary[name] = entry
    return call


def require(condition, message, call=None):
    if not condition:
        err = f"\nSTDERR: {call.get('stderr')}" if isinstance(call, dict) and call.get("stderr") else ""
        out = f"\nSTDOUT: {call.get('stdout')}" if isinstance(call, dict) and call.get("stdout") else ""
        raise AssertionError(f"{message}{err}{out}")


def main():
    backup = None
    policy_path = None
    backup_mode = None
    policy_installed = False
    try:
        p_path, resolved_from_host = resolve_policy_path()
        require(resolved_from_host or "BRIDGE_POLICY_FILE" in os.environ, "Expected policy path to be resolved from host via 'policy info'")
        policy_path = p_path
        if policy_path.exists():
            backup = policy_path.read_bytes()
            try:
                backup_mode = os.stat(policy_path).st_mode & 0o777
            except OSError:
                pass
        policy = {
            "default": {
                "allowedActions": [
                    "ping", "navigate", "waitForLoad", "waitForSelector", "click", "fill",
                    "select", "uploadFile", "screenshot", "extractText", "getHTML", "type", "drag",
                    "scroll", "press", "hover", "startMonitoring", "consoleMessages",
                    "setViewport", "setUserAgent", "setNetworkConditions", "clearNetworkConditions",
                    "setCpuThrottling", "setColorScheme", "networkRequests", "executeScriptCDP",
                    "handleDialog", "stopMonitoring", "getCurrentState", "startInterception",
                    "interceptedRequests", "stopInterception", "downloadUrl", "storageState",
                    "setGeolocation", "clearGeolocation", "performanceMetrics", "closeTab", "policyInfo"
                ],
                "allowedOrigins": ["http://127.0.0.1:*", "*://127.0.0.1:*"],
                "deniedActions": [],
                "deniedOrigins": [],
                "requireConfirmation": [],
                "redact": True,
                "audit": False,
            }
        }
        policy_path.write_text(json.dumps(policy, separators=(",", ":")), encoding="utf-8")
        policy_installed = True
        try:
            os.chmod(policy_path, 0o600)
        except OSError:
            pass
        time.sleep(1.1)  # let policy file mtime advance for host hot-reload
        main_inner()
    finally:
        if policy_installed and policy_path is not None:
            restore_policy(backup, policy_path, backup_mode)


def main_inner():
    Path(UPLOAD_FIXTURE).write_text("upload fixture\n", encoding="utf-8")
    for path in [SHOT_PATH, HTML_PATH]:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    server = start_server()
    summary = {}
    tab_id = None
    monitoring_started = False
    try:
        call = run_bridge("ping")
        record(summary, "ping", call, {"result": result(call)})
        require(call["exit"] == 0 and result(call) == "pong", "ping failed")
        call = run_bridge("navigate", BASE_URL)
        nav = result(call)
        tab_id = nav.get("tabId") if isinstance(nav, dict) else None
        record(summary, "navigate", call, {"tabId": tab_id})
        require(call["exit"] == 0 and tab_id, "navigate did not return tabId")

        checks = [
            ("waitForLoad", run_bridge("waitForLoad", tab_id, 20000), None),
            ("waitForSelector", run_bridge("waitForSelector", tab_id, "#q", 20000), None),
            ("fill", run_bridge("fill", tab_id, "#q", "hello"), None),
            ("select", run_bridge("select", tab_id, "#kind", "beta"), None),
            ("hover", run_bridge("hover", tab_id, "#btn"), None),
            ("uploadFile", run_bridge("uploadFile", tab_id, "#file", UPLOAD_FIXTURE), None),
        ]
        for name, item, extra in checks:
            record(summary, name, item, extra)
            require(item["exit"] == 0, f"{name} failed", item)
        call = run_bridge("executeScriptCDP", tab_id, "document.querySelector('#q').value")
        value = result(call).get("val") if isinstance(result(call), dict) else None
        record(summary, "filledValue", call, {"value": value})
        require(call["exit"] == 0 and value == "hello", "fill did not set value")

        call = run_bridge("executeScriptCDP", tab_id, "document.querySelector('#file').files.length")
        file_count = result(call).get("val") if isinstance(result(call), dict) else None
        record(summary, "uploadedFiles", call, {"files": file_count})
        require(call["exit"] == 0 and file_count == 1, "uploadFile did not set file input")

        call = run_bridge("click", tab_id, "#btn")
        record(summary, "click", call)
        require(call["exit"] == 0, "click failed")
        time.sleep(0.2)

        call = run_bridge("executeScriptCDP", tab_id, "document.querySelector('#status').textContent")
        status = result(call).get("val") if isinstance(result(call), dict) else None
        record(summary, "clickedStatus", call, {"status": status})
        require(call["exit"] == 0 and status == "clicked:hello", "click did not update status")

        call = run_bridge("drag", tab_id, "#from", "#to")
        record(summary, "drag", call)
        require(call["exit"] == 0, "drag failed")

        call = run_bridge("press", tab_id, "Enter")
        record(summary, "press", call)
        require(call["exit"] == 0, "press failed")

        call = run_bridge("scroll", tab_id, 0, 300)
        record(summary, "scroll", call)
        require(call["exit"] == 0, "scroll failed")

        call = run_bridge("screenshot", tab_id, SHOT_PATH)
        shot = call.get("json") or {}
        record(summary, "screenshot", call, {"bytes": shot.get("bytes"), "mimeType": shot.get("mimeType")})
        require(call["exit"] == 0 and shot.get("bytes", 0) > 1000 and Path(SHOT_PATH).is_file(), "screenshot failed")

        call = run_bridge("getHTML", tab_id, HTML_PATH)
        html = call.get("json") or {}
        record(summary, "getHTML", call, {"bytes": html.get("bytes")})
        require(call["exit"] == 0 and "Chrome Bridge Live Test" in Path(HTML_PATH).read_text(encoding="utf-8"), "getHTML failed")

        call = run_bridge("extractText", tab_id, 2000)
        text = result(call).get("text", "") if isinstance(result(call), dict) else ""
        record(summary, "extractText", call, {"containsTitle": "Chrome Bridge Live Test" in text, "chars": len(text)})
        require(call["exit"] == 0 and "Chrome Bridge Live Test" in text, "extractText missing title")

        call = run_bridge("setViewport", tab_id, 800, 600, 1)
        viewport = result(call)
        record(summary, "setViewport", call, {"width": viewport.get("width"), "height": viewport.get("height")})
        require(call["exit"] == 0 and viewport.get("width") == 800 and viewport.get("height") == 600, "setViewport failed")

        call = run_bridge("startMonitoring", tab_id)
        record(summary, "startMonitoring", call)
        require(call["exit"] == 0, "startMonitoring failed")
        monitoring_started = True

        for name, selector in [("monitorLogClick", "#log"), ("monitorFetchClick", "#fetch")]:
            call = run_bridge("click", tab_id, selector)
            record(summary, name, call)
            require(call["exit"] == 0, f"{name} failed")
        time.sleep(1)

        call = run_bridge("executeScriptCDP", tab_id, "setTimeout(() => alert('hello dialog'), 0); 'scheduled'")
        record(summary, "monitorAlertSchedule", call)
        require(call["exit"] == 0, "alert scheduling failed")
        time.sleep(0.2)

        call = run_bridge("handleDialog", tab_id, "accept")
        record(summary, "handleDialog", call)
        require(call["exit"] == 0, "handleDialog failed")

        call = run_bridge("consoleMessages", tab_id)
        messages = result(call).get("messages", []) if isinstance(result(call), dict) else []
        record(summary, "consoleMessages", call, {"count": len(messages)})
        require(call["exit"] == 0 and len(messages) >= 1, "consoleMessages missing events")

        call = run_bridge("networkRequests", tab_id)
        requests = result(call).get("requests", []) if isinstance(result(call), dict) else []
        has_query_strings = any("?" in item.get("url", "") for item in requests if isinstance(item, dict))
        record(summary, "networkRequests", call, {"count": len(requests), "queryStringsRedacted": not has_query_strings})
        require(call["exit"] == 0 and len(requests) >= 1 and not has_query_strings, "networkRequests failed redaction check")

        call = run_bridge("stopMonitoring", tab_id)
        record(summary, "stopMonitoring", call)
        require(call["exit"] == 0, "stopMonitoring failed")
        monitoring_started = False

        # print will now happen in finally
    finally:
        print("SUMMARY: " + json.dumps(summary, sort_keys=True, separators=(",", ":")))
        if monitoring_started and tab_id is not None:
            call = run_bridge("stopMonitoring", tab_id)
            record(summary, "stopMonitoringFinally", call)
        server.shutdown()

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        # Try to print summary if main was running
        sys.exit(1)
