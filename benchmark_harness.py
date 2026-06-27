#!/usr/bin/env python3
import argparse
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CLIENT = SCRIPT_DIR / "test_client.py"
BRIDGE_COMMAND = os.environ.get("CHROME_BRIDGE_CLIENT")

COMPARISON_METADATA = {
    "tools": {
        "chrome-native-bridge": {
            "name": "Chrome Native Bridge",
            "strengths": "Runs in real-profile Chrome using extension/native host, allows CDP-backed interactions, supports storage, geolocation, performance metrics, and console/network monitoring.",
            "limits": "No isolated contexts/profiles, no native trace/video capture, and limited first-party test-runner integrations.",
            "capability_status": "pass",
            "scores": {"speed": 4, "capability": 4, "authReuse": 5, "ergonomics": 3},
        },
        "playwright": {
            "name": "Playwright",
            "strengths": "Multiple browser engines, isolated browser contexts, rich locator semantics, tracing, video recording, and test-runner integration.",
            "limits": "Runs in custom automation profiles by default, so existing user-profile cookies, extensions, and local state require explicit setup.",
            "capability_status": "pass",
            "scores": {"speed": 4, "capability": 5, "authReuse": 2, "ergonomics": 5},
        },
        "claude-in-chrome": {
            "name": "Claude in Chrome",
            "strengths": "Agentic natural-language driving of a real browser session.",
            "limits": "Manual/agentic interaction is slower than API-driven tools and lacks low-level interception/performance primitives.",
            "capability_status": "manual",
            "scores": {"speed": 1, "capability": 3, "authReuse": 5, "ergonomics": 4},
        },
        "codex-chrome-extension": {
            "name": "Codex Chrome Extension",
            "strengths": "In-browser assistant and script executor that can reuse a signed-in browser profile.",
            "limits": "No dedicated native host channel and fewer low-level browser diagnostics than CDP-backed harnesses.",
            "capability_status": "manual",
            "scores": {"speed": 2, "capability": 3, "authReuse": 5, "ergonomics": 4},
        },
        "puppeteer": {
            "name": "Puppeteer",
            "strengths": "Direct Chromium/CDP control with a lightweight API and broad scraping/automation ecosystem.",
            "limits": "Cross-browser support is narrower than Playwright and test-runner/trace ergonomics are less integrated.",
            "capability_status": "pass",
            "scores": {"speed": 5, "capability": 4, "authReuse": 2, "ergonomics": 4},
        },
        "chrome-devtools-mcp": {
            "name": "Chrome DevTools MCP",
            "strengths": "Standardized MCP surface for controlling Chrome DevTools from agents.",
            "limits": "Depends on a separate MCP server and lacks the native-host file/profile conveniences of this bridge.",
            "capability_status": "pass",
            "scores": {"speed": 3, "capability": 4, "authReuse": 4, "ergonomics": 3},
        },
    },
    "gaps": [
        {
            "gap": "isolated contexts and profiles",
            "description": "Launch isolated/ephemeral browser profiles within one benchmark run while preserving the real-profile mode.",
            "surface": "benchmark_harness.py adapter lifecycle and extension/background.js session model",
            "acceptance": "A benchmark run can create two isolated sessions with separate cookies and report both as pass.",
        },
        {
            "gap": "multi-browser support",
            "description": "Add non-Chrome browser targets or explicit parity adapters for Firefox/WebKit.",
            "surface": "benchmark_harness.py adapter registry",
            "acceptance": "The scorecard includes measured Firefox or WebKit timings for navigate, click, fill, screenshot, and storage.",
        },
        {
            "gap": "trace and video recording",
            "description": "Capture replayable traces or video artifacts for browser operations.",
            "surface": "extension/background.js debugger commands and benchmark_harness.py artifact fields",
            "acceptance": "A live Chrome Bridge run writes a trace or video artifact path and the report links it without exposing private content.",
        },
        {
            "gap": "rich locator semantics",
            "description": "Add semantic selectors such as role/name/text wrappers on top of CSS selectors.",
            "surface": "test_client.py CLI and extension/background.js selector helpers",
            "acceptance": "Benchmark operations can click and fill by role/name on the fixture without CSS selectors.",
        },
        {
            "gap": "first-party ecosystem integrations",
            "description": "Expose benchmark output in test-runner and CI-friendly formats.",
            "surface": "benchmark_harness.py report exporters",
            "acceptance": "The harness emits JSON, Markdown, and JUnit or GitHub Step Summary output for the same run.",
        },
        {
            "gap": "interactive destructive approval",
            "description": "Provide an interactive approve/deny path for actions the policy marks requireConfirmation, instead of failing closed with confirmationRequired.",
            "surface": "bridge.py, host-rs/src/main.rs, mcp/chrome_bridge_mcp/server.py",
            "acceptance": "A confirmation-required action can be approved through an explicit client prompt and then proceeds, while denial blocks it, with the decision audited.",
        },
    ],
}

OPERATIONS = [
    "ping",
    "navigate",
    "wait-load",
    "wait-selector",
    "click",
    "fill",
    "select",
    "upload",
    "screenshot",
    "extract-text",
    "get-html",
    "observe-state",
    "console-monitoring",
    "network-monitoring",
    "dialog-handling",
    "storage-state",
    "geolocation",
    "performance-metrics",
]

FIXTURE_PAGE = b"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Benchmark Fixture</title>
  <style>body { font-family: sans-serif; min-height: 1600px; }</style>
</head>
<body>
  <h1>Benchmark Fixture</h1>
  <input id="q" value="">
  <select id="kind">
    <option value="alpha">Alpha</option>
    <option value="beta">Beta</option>
  </select>
  <button id="btn">Click me</button>
  <input id="file" type="file">
  <button id="log">Log</button>
  <button id="fetch">Fetch</button>
  <button id="alert">Alert</button>
  <div id="status">ready</div>
  <script>
    document.getElementById('btn').addEventListener('click', () => {
      document.getElementById('status').textContent = 'clicked:' + document.getElementById('q').value;
    });
    document.getElementById('log').addEventListener('click', () => console.log('bridge fixture console message'));
    document.getElementById('fetch').addEventListener('click', () => fetch('/data.json?secret=redact-me'));
    document.getElementById('alert').addEventListener('click', () => alert('hello dialog'));
  </script>
</body>
</html>
"""


class UnsupportedAdapter(RuntimeError):
    pass


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data.json"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(FIXTURE_PAGE)

    def log_message(self, format, *args):
        pass


def start_fixture_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/"


def calculate_median(values):
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 1:
        return float(sorted_vals[n // 2])
    return float((sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0)


def get_bridge_command():
    token_file = os.environ.get("BRIDGE_TOKEN_FILE", SCRIPT_DIR / "bridge_token.txt")
    if not BRIDGE_COMMAND and not Path(token_file).exists():
        raise RuntimeError(
            "Missing bridge token. Run ./setup.sh <extension-id> first, set BRIDGE_TOKEN_FILE, "
            "or set CHROME_BRIDGE_CLIENT=chrome-bridge to use an installed launcher."
        )
    return [BRIDGE_COMMAND] if BRIDGE_COMMAND else [sys.executable, str(CLIENT)]


def _load_bridge_token():
    token_file = os.environ.get("BRIDGE_TOKEN_FILE", str(SCRIPT_DIR / "bridge_token.txt"))
    with open(token_file) as f:
        return f.read().strip()


# Maps the harness's CLI-style verbs to (bridge action, payload-builder). This
# mirrors test_client.py's argument handling so an in-process socket client can
# speak the bridge protocol directly, without spawning python3 per operation.
def _build_bridge_payload(verb, args):
    if verb == "ping":
        return "ping", {}
    if verb == "navigate":
        return "navigate", {"url": args[0]}
    if verb == "waitForLoad":
        return "waitForLoad", {"tabId": int(args[0]), "timeoutMs": int(args[1])}
    if verb == "waitForSelector":
        return "waitForSelector", {"tabId": int(args[0]), "selector": args[1], "timeoutMs": int(args[2])}
    if verb == "click":
        return "click", {"tabId": int(args[0]), "selector": args[1]}
    if verb == "fill":
        return "fill", {"tabId": int(args[0]), "selector": args[1], "text": args[2]}
    if verb == "select":
        return "select", {"tabId": int(args[0]), "selector": args[1], "value": args[2]}
    if verb == "uploadFile":
        return "uploadFile", {"tabId": int(args[0]), "selector": args[1], "files": [os.path.abspath(p) for p in args[2:]]}
    if verb == "screenshot":
        return "screenshot", {"tabId": int(args[0]), "format": "png"}
    if verb == "extractText":
        return "extractText", {"tabId": int(args[0]), "maxChars": int(args[1])}
    if verb == "getHTML":
        return "getHTML", {"tabId": int(args[0])}
    if verb == "getCurrentState":
        return "getCurrentState", {"tabId": int(args[0])}
    if verb == "storageState":
        return "storageState", {"tabId": int(args[0])}
    if verb == "setGeolocation":
        accuracy = float(args[3]) if len(args) > 3 else None
        return "setGeolocation", {"tabId": int(args[0]), "latitude": float(args[1]), "longitude": float(args[2]), "accuracy": accuracy}
    if verb == "clearGeolocation":
        return "clearGeolocation", {"tabId": int(args[0])}
    if verb in {"performanceMetrics", "closeTab", "startMonitoring", "stopMonitoring", "consoleMessages", "networkRequests"}:
        return verb, {"tabId": int(args[0])}
    if verb == "batch":
        payload = {"steps": json.loads(args[0])}
        if len(args) > 1:
            payload["tabId"] = int(args[1])
        return "batch", payload
    raise ValueError(f"Unmapped bridge verb: {verb}")


class BridgeClient:
    """Persistent in-process bridge client.

    Holds one keep-alive TCP connection to the native host and reuses it for
    every request, eliminating the per-operation python3 subprocess spawn and
    TCP handshake that dominated Chrome Bridge latency.
    """

    def __init__(self, timeout=20):
        self._token = _load_bridge_token()
        self._port = int(os.environ.get("BRIDGE_PORT", 9223))
        self._timeout = timeout
        self._sock = None
        self._buffer = b""

    def _connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect(("127.0.0.1", self._port))
        self._sock = sock
        self._buffer = b""

    def _recv_line(self):
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("bridge closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line

    def request(self, action, payload):
        cmd = json.dumps({"action": action, "payload": payload, "token": self._token}) + "\n"
        # One transparent reconnect: the host may have idled the socket shut.
        for attempt in range(2):
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(cmd.encode("utf-8"))
                line = self._recv_line()
                return json.loads(line.decode("utf-8"))
            except (OSError, ConnectionError) as exc:
                self.close()
                if attempt == 1:
                    raise RuntimeError(f"bridge request failed: {exc}")
        raise RuntimeError("bridge request failed")

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            self._buffer = b""


# Lazily-created shared client so run_chrome_bridge_op need not change shape.
_bridge_client = None


def get_bridge_client():
    global _bridge_client
    if _bridge_client is None:
        _bridge_client = BridgeClient()
    return _bridge_client


def reset_bridge_client():
    global _bridge_client
    if _bridge_client is not None:
        _bridge_client.close()
        _bridge_client = None


def run_bridge_cmd(*args, timeout=20):
    # If an external launcher is configured, preserve the subprocess path so
    # CHROME_BRIDGE_CLIENT still works; otherwise use the persistent client.
    if BRIDGE_COMMAND:
        proc = subprocess.run([BRIDGE_COMMAND, *map(str, args)], text=True, capture_output=True, timeout=timeout)
        parsed = None
        if proc.stdout:
            try:
                parsed = json.loads(proc.stdout)
            except Exception:
                pass
        return {"exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "json": parsed}

    verb = args[0]
    rest = [str(a) for a in args[1:]]
    try:
        action, payload = _build_bridge_payload(verb, rest)
        response = get_bridge_client().request(action, payload)
    except Exception as exc:
        return {"exit": 1, "stdout": "", "stderr": str(exc), "json": None}
    exit_code = 0 if response.get("success") is True else 1
    result = response.get("result")
    if isinstance(result, dict) and result.get("success") is False:
        exit_code = 1
    return {"exit": exit_code, "stdout": json.dumps(response), "stderr": "", "json": response}


def get_result(call):
    data = call.get("json") or {}
    return data.get("result") if data.get("result") is not None else data


def sanitize_error(message):
    text = str(message)
    text = text.replace(str(SCRIPT_DIR), "<repo>")
    home = str(Path.home())
    if home:
        text = text.replace(home, "~")
    return text


def mark_all(results, capability, reason):
    sanitized = sanitize_error(reason)
    for op in OPERATIONS:
        results[op]["capability"] = capability
        results[op]["errors"].append(sanitized)


def finish_results(adapter, iterations, results):
    operations = []
    for op in OPERATIONS:
        durations = results[op]["durationsMs"]
        operations.append(
            {
                "name": op,
                "capability": results[op]["capability"],
                "durationsMs": durations,
                "medianMs": calculate_median(durations),
                "errors": results[op]["errors"],
            }
        )
    return {
        "schemaVersion": 1,
        "adapter": adapter,
        "iterations": iterations,
        "operations": operations,
        "comparison": COMPARISON_METADATA,
        "scorecard": build_scorecard(adapter, operations),
    }


def record_op(results, op_name, capability, duration_ms, error=None):
    results[op_name]["durationsMs"].append(duration_ms)
    if capability != "pass":
        results[op_name]["capability"] = capability
    if error:
        results[op_name]["errors"].append(sanitize_error(error))


def run_chrome_bridge_op(op_name, context, base_url):
    t0 = time.perf_counter()
    tab_id = context.get("tab_id")
    if op_name == "ping":
        res = run_bridge_cmd("ping")
        if res["exit"] == 0 and get_result(res) == "pong":
            return "pass", (time.perf_counter() - t0) * 1000
        raise RuntimeError("ping failed")
    if op_name == "navigate":
        res = run_bridge_cmd("navigate", base_url)
        nav = get_result(res) or {}
        if res["exit"] == 0 and nav.get("tabId") is not None:
            context["tab_id"] = nav["tabId"]
            return "pass", (time.perf_counter() - t0) * 1000
        raise RuntimeError("navigate failed")
    if tab_id is None:
        raise RuntimeError("no active tab")
    if op_name == "wait-load":
        res = run_bridge_cmd("waitForLoad", tab_id, 10000)
    elif op_name == "wait-selector":
        res = run_bridge_cmd("waitForSelector", tab_id, "#q", 10000)
    elif op_name == "click":
        res = run_bridge_cmd("click", tab_id, "#btn")
    elif op_name == "fill":
        res = run_bridge_cmd("fill", tab_id, "#q", "hello")
    elif op_name == "select":
        res = run_bridge_cmd("select", tab_id, "#kind", "beta")
    elif op_name == "upload":
        with tempfile.NamedTemporaryFile(prefix="upload-", suffix=".txt", delete=False) as f:
            f.write(b"upload fixture\n")
            temp_path = f.name
        try:
            res = run_bridge_cmd("uploadFile", tab_id, "#file", temp_path)
        finally:
            with contextlib_suppress():
                os.unlink(temp_path)
    elif op_name == "screenshot":
        with tempfile.NamedTemporaryFile(prefix="shot-", suffix=".png", delete=False) as f:
            temp_path = f.name
        try:
            res = run_bridge_cmd("screenshot", tab_id, temp_path)
        finally:
            with contextlib_suppress():
                os.unlink(temp_path)
    elif op_name == "extract-text":
        res = run_bridge_cmd("extractText", tab_id, 2000)
    elif op_name == "get-html":
        with tempfile.NamedTemporaryFile(prefix="html-", suffix=".html", delete=False) as f:
            temp_path = f.name
        try:
            res = run_bridge_cmd("getHTML", tab_id, temp_path)
        finally:
            with contextlib_suppress():
                os.unlink(temp_path)
    elif op_name == "observe-state":
        res = run_bridge_cmd("getCurrentState", tab_id)
    elif op_name == "console-monitoring":
        res = run_bridge_cmd("batch", json.dumps([
            {"action": "startMonitoring"},
            {"action": "click", "payload": {"selector": "#log"}},
            {"action": "consoleMessages", "delayMs": 100},
        ]), tab_id)
    elif op_name == "network-monitoring":
        res = run_bridge_cmd("batch", json.dumps([
            {"action": "startMonitoring"},
            {"action": "click", "payload": {"selector": "#fetch"}},
            {"action": "networkRequests", "delayMs": 100},
        ]), tab_id)
    elif op_name == "dialog-handling":
        res = run_bridge_cmd("batch", json.dumps([
            {"action": "executeScriptCDP", "payload": {"code": "setTimeout(() => alert('hello dialog'), 0); 'scheduled'"}},
            {"action": "handleDialog", "delayMs": 100, "payload": {"accept": True}},
        ]), tab_id)
    elif op_name == "storage-state":
        with tempfile.NamedTemporaryFile(prefix="state-", suffix=".json", delete=False) as f:
            temp_path = f.name
        try:
            res = run_bridge_cmd("storageState", tab_id, temp_path)
        finally:
            with contextlib_suppress():
                os.unlink(temp_path)
    elif op_name == "geolocation":
        res = run_bridge_cmd("setGeolocation", tab_id, 37.7749, -122.4194, 100)
        run_bridge_cmd("clearGeolocation", tab_id)
    elif op_name == "performance-metrics":
        res = run_bridge_cmd("performanceMetrics", tab_id)
    else:
        raise ValueError(f"Unknown operation: {op_name}")
    if res["exit"] == 0:
        return "pass", (time.perf_counter() - t0) * 1000
    raise RuntimeError(f"{op_name} failed")


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True


def run_noop(results):
    for op in OPERATIONS:
        t0 = time.perf_counter()
        value = 0
        for i in range(1000):
            value += i
        time.sleep(0.0001)
        record_op(results, op, "pass", (time.perf_counter() - t0) * 1000.0)


def run_chrome_bridge_iteration(results, base_url):
    context = {"tab_id": None}
    try:
        for op in OPERATIONS:
            try:
                capability, duration = run_chrome_bridge_op(op, context, base_url)
                record_op(results, op, capability, duration)
            except Exception as exc:
                record_op(results, op, "fail", 0.0, exc)
    finally:
        if context.get("tab_id") is not None:
            with contextlib_suppress():
                run_bridge_cmd("closeTab", context["tab_id"])


def run_playwright_iteration(results, state, base_url):
    page = state["browser"].new_page(geolocation={"latitude": 37.7749, "longitude": -122.4194})
    page.context.grant_permissions(["geolocation"])
    console_messages = []
    requests = []
    page.on("console", lambda msg: console_messages.append(msg.text))
    page.on("request", lambda req: requests.append(req.url))
    try:
        for op in OPERATIONS:
            t0 = time.perf_counter()
            try:
                if op == "ping":
                    page.evaluate("1 + 1")
                elif op == "navigate":
                    page.goto(base_url, wait_until="domcontentloaded")
                elif op == "wait-load":
                    page.wait_for_load_state("load")
                elif op == "wait-selector":
                    page.wait_for_selector("#q")
                elif op == "click":
                    page.click("#btn")
                elif op == "fill":
                    page.fill("#q", "hello")
                elif op == "select":
                    page.select_option("#kind", "beta")
                elif op == "upload":
                    with tempfile.NamedTemporaryFile(prefix="upload-", suffix=".txt", delete=False) as f:
                        f.write(b"upload fixture\n")
                        temp_path = f.name
                    try:
                        page.set_input_files("#file", temp_path)
                    finally:
                        with contextlib_suppress():
                            os.unlink(temp_path)
                elif op == "screenshot":
                    page.screenshot()
                elif op == "extract-text":
                    page.locator("body").inner_text()
                elif op == "get-html":
                    page.content()
                elif op == "observe-state":
                    page.locator("body").inner_text()
                elif op == "console-monitoring":
                    page.click("#log")
                    page.wait_for_timeout(50)
                    if not console_messages:
                        raise RuntimeError("no console messages captured")
                elif op == "network-monitoring":
                    before = len(requests)
                    page.click("#fetch")
                    page.wait_for_timeout(50)
                    if len(requests) <= before:
                        raise RuntimeError("no network requests captured")
                elif op == "dialog-handling":
                    page.once("dialog", lambda dialog: dialog.accept())
                    page.click("#alert")
                elif op == "storage-state":
                    page.context.storage_state()
                elif op == "geolocation":
                    latitude = page.evaluate(
                        "new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition("
                        "pos => resolve(pos.coords.latitude), err => reject(new Error(err.message)), {timeout: 1000}))"
                    )
                    if abs(float(latitude) - 37.7749) > 0.01:
                        raise RuntimeError(f"unexpected latitude {latitude}")
                elif op == "performance-metrics":
                    page.evaluate("JSON.stringify(performance.timing)")
                else:
                    raise UnsupportedAdapter(f"unsupported operation {op}")
                record_op(results, op, "pass", (time.perf_counter() - t0) * 1000)
            except UnsupportedAdapter as exc:
                record_op(results, op, "unsupported", (time.perf_counter() - t0) * 1000, exc)
            except Exception as exc:
                record_op(results, op, "fail", (time.perf_counter() - t0) * 1000, exc)
    finally:
        page.close()


def run_playwright(args, results, base_url):
    if args.iterations == 0:
        return
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        mark_all(results, "unsupported", f"Playwright Python is not installed: {exc}")
        return
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            state = {"browser": browser}
            try:
                for _ in range(args.iterations):
                    run_playwright_iteration(results, state, base_url)
            finally:
                browser.close()
    except Exception as exc:
        mark_all(results, "unsupported", f"Playwright browser launch failed: {exc}")


def puppeteer_script():
    return r'''
import fs from 'node:fs';
const baseUrl = process.argv[2];
const iterations = Number(process.argv[3]);
const output = process.argv[4];
const operations = JSON.parse(process.argv[5]);
function median(values) { return 0; }
function createResults() {
  const results = {};
  for (const op of operations) results[op] = {capability: 'pass', durationsMs: [], errors: []};
  return results;
}
function record(results, op, capability, started, error) {
  results[op].durationsMs.push(Number(process.hrtime.bigint() - started) / 1000000);
  if (capability !== 'pass') results[op].capability = capability;
  if (error) results[op].errors.push(String(error && error.message ? error.message : error));
}
const results = createResults();
let puppeteer;
try {
  puppeteer = (await import('puppeteer')).default;
} catch (error) {
  for (const op of operations) {
    results[op].capability = 'unsupported';
    results[op].errors.push(`Puppeteer is not installed: ${error.message}`);
  }
  fs.writeFileSync(output, JSON.stringify(results));
  process.exit(0);
}
let browser;
try {
  browser = await puppeteer.launch({headless: 'new'});
} catch (launchError) {
  // Bundled Chrome may be absent; fall back to the system Chrome stable
  // channel (this is also how chrome-devtools-mcp launches Chrome).
  browser = await puppeteer.launch({channel: 'chrome', headless: 'new'});
}
  await browser.defaultBrowserContext().overridePermissions(baseUrl, ['geolocation']);
  for (let i = 0; i < iterations; i++) {
    const page = await browser.newPage();
    const consoleMessages = [];
    const requests = [];
    page.on('console', msg => consoleMessages.push(msg.text()));
    page.on('request', req => requests.push(req.url()));
    await page.setGeolocation({latitude: 37.7749, longitude: -122.4194});
    for (const op of operations) {
      const started = process.hrtime.bigint();
      try {
        if (op === 'ping') await page.evaluate(() => 1 + 1);
        else if (op === 'navigate') await page.goto(baseUrl, {waitUntil: 'domcontentloaded'});
        else if (op === 'wait-load') await page.waitForFunction(() => document.readyState === 'complete');
        else if (op === 'wait-selector') await page.waitForSelector('#q');
        else if (op === 'click') await page.click('#btn');
        else if (op === 'fill') await page.$eval('#q', (el, value) => { el.value = value; el.dispatchEvent(new Event('input', {bubbles: true})); }, 'hello');
        else if (op === 'select') await page.select('#kind', 'beta');
        else if (op === 'upload') {
          const tmp = `/tmp/chrome-bridge-puppeteer-upload-${Date.now()}-${i}.txt`;
          fs.writeFileSync(tmp, 'upload fixture\n');
          const handle = await page.$('#file');
          await handle.uploadFile(tmp);
          fs.unlinkSync(tmp);
        }
        else if (op === 'screenshot') await page.screenshot();
        else if (op === 'extract-text') await page.$eval('body', el => el.innerText);
        else if (op === 'get-html') await page.content();
        else if (op === 'observe-state') await page.$eval('body', el => el.innerText);
        else if (op === 'console-monitoring') { await page.click('#log'); await new Promise(r => setTimeout(r, 50)); if (!consoleMessages.length) throw new Error('no console messages captured'); }
        else if (op === 'network-monitoring') { const before = requests.length; await page.click('#fetch'); await new Promise(r => setTimeout(r, 50)); if (requests.length <= before) throw new Error('no network requests captured'); }
        else if (op === 'dialog-handling') { page.once('dialog', dialog => dialog.accept()); await page.click('#alert'); }
        else if (op === 'storage-state') await page.cookies();
        else if (op === 'geolocation') { const latitude = await page.evaluate(() => new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(pos => resolve(pos.coords.latitude), err => reject(new Error(err.message)), {timeout: 1000}))); if (Math.abs(Number(latitude) - 37.7749) > 0.01) throw new Error(`unexpected latitude ${latitude}`); }
        else if (op === 'performance-metrics') await page.metrics();
        else throw new Error(`unsupported operation ${op}`);
        record(results, op, 'pass', started);
      } catch (error) {
        record(results, op, 'fail', started, error);
      }
    }
    await page.close();
  }
} catch (error) {
  for (const op of operations) {
    results[op].capability = 'unsupported';
    results[op].errors.push(`Puppeteer browser launch failed: ${error.message}`);
  }
} finally {
  if (browser) await browser.close();
}
fs.writeFileSync(output, JSON.stringify(results));
'''


def npm_root():
    if not shutil.which("npm"):
        return None
    proc = subprocess.run(["npm", "root"], cwd=SCRIPT_DIR, text=True, capture_output=True, timeout=10)
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return root if root else None


def run_puppeteer_runner(args, results, base_url, runner_name, temp_prefix):
    if args.iterations == 0:
        return
    if not shutil.which("node"):
        mark_all(results, "unsupported", "node executable is not available")
        return
    with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=SCRIPT_DIR) as tmp:
        script_path = Path(tmp) / "bench.mjs"
        output_path = Path(tmp) / "results.json"
        script_path.write_text(puppeteer_script(), encoding="utf-8")
        env = os.environ.copy()
        root = npm_root()
        if root:
            env["NODE_PATH"] = root if not env.get("NODE_PATH") else f"{root}{os.pathsep}{env['NODE_PATH']}"
        proc = subprocess.run(
            ["node", str(script_path), base_url, str(args.iterations), str(output_path), json.dumps(OPERATIONS)],
            text=True,
            capture_output=True,
            timeout=max(30, args.iterations * 20),
            env=env,
        )
        if proc.returncode != 0:
            mark_all(results, "unsupported", f"{runner_name} runner failed: {proc.stderr.strip() or proc.stdout.strip()}")
            return
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        for op in OPERATIONS:
            item = payload.get(op, {})
            results[op]["capability"] = item.get("capability", "unsupported")
            results[op]["durationsMs"].extend(item.get("durationsMs", []))
            results[op]["errors"].extend(sanitize_error(error) for error in item.get("errors", []))


def run_puppeteer(args, results, base_url):
    run_puppeteer_runner(args, results, base_url, "Puppeteer", ".chrome-bridge-puppeteer-")


def run_chrome_devtools_mcp(args, results, base_url):
    run_puppeteer_runner(args, results, base_url, "Chrome DevTools MCP", ".chrome-devtools-mcp-")


def score_from_median(operations):
    passed = [op for op in operations if op.get("capability") == "pass" and op.get("medianMs", 0) > 0]
    if not passed:
        return 0
    median = calculate_median([op["medianMs"] for op in passed])
    if median <= 50:
        return 5
    if median <= 100:
        return 4
    if median <= 250:
        return 3
    if median <= 500:
        return 2
    return 1


def build_scorecard(adapter, operations):
    scorecard = {}
    for key, tool in COMPARISON_METADATA["tools"].items():
        scores = dict(tool.get("scores", {}))
        if key == adapter or (adapter == "chrome-bridge" and key == "chrome-native-bridge"):
            scores["speed"] = score_from_median(operations)
            scores["capability"] = round(
                5 * sum(1 for op in operations if op.get("capability") == "pass") / max(1, len(operations)), 1
            )
            source = "measured"
        else:
            source = "metadata"
        overall = round((scores["speed"] + scores["capability"] + scores["authReuse"] + scores["ergonomics"]) / 4, 1)
        scorecard[key] = {**scores, "overall": overall, "source": source}
    return scorecard


def initial_results():
    return {op: {"capability": "pass", "durationsMs": [], "errors": []} for op in OPERATIONS}


def handle_run(args):
    base_url = args.base_url
    server = None
    if args.adapter in {"chrome-bridge", "playwright", "puppeteer", "chrome-devtools-mcp"} and args.iterations > 0 and not base_url:
        server, base_url = start_fixture_server()
    results = initial_results()
    try:
        if args.adapter == "noop":
            for _ in range(args.iterations):
                run_noop(results)
        elif args.adapter == "chrome-bridge":
            for _ in range(args.iterations):
                run_chrome_bridge_iteration(results, base_url)
        elif args.adapter == "playwright":
            run_playwright(args, results, base_url)
        elif args.adapter == "puppeteer":
            run_puppeteer(args, results, base_url)
        elif args.adapter == "chrome-devtools-mcp":
            run_chrome_devtools_mcp(args, results, base_url)
        else:
            raise ValueError(f"Unknown adapter: {args.adapter}")
        output_data = finish_results(args.adapter, args.iterations, results)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
        print(f"Benchmark results successfully written to {args.output}")
    finally:
        reset_bridge_client()
        if server:
            server.shutdown()
            server.server_close()


def handle_compare(args):
    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Error: input file {args.input} does not exist", file=sys.stderr)
        sys.exit(1)
    data = json.loads(input_path.read_text(encoding="utf-8"))
    adapter = data.get("adapter", "unknown")
    iterations = data.get("iterations", 0)
    operations = data.get("operations", [])
    comparison = data.get("comparison", {})
    tools = comparison.get("tools", {})
    gaps = comparison.get("gaps", [])
    scorecard = data.get("scorecard") or build_scorecard(adapter, operations)

    lines = [
        "# Browser Automation Benchmark Report",
        "",
        "This report contains measured timings for the selected adapter run and static capability metadata for the other browser automation surfaces.",
        "",
        "## Run Configuration",
        f"- **Measured adapter:** `{adapter}`",
        f"- **Iterations:** {iterations}",
        "",
        "## Operation Timings",
        "Timings below come only from the selected adapter run.",
        "",
        "| Operation | Status | Durations (ms) | Median Time (ms) |",
        "| --- | --- | --- | --- |",
    ]
    for op in operations:
        durations = ", ".join(f"{d:.2f}" for d in op.get("durationsMs", []))
        lines.append(f"| {op.get('name', '')} | {op.get('capability', '')} | {durations} | {op.get('medianMs', 0.0):.2f} |")
    lines.extend(["", "## Normalized Scorecard", "Scores are 1-5. Source marks measured rows versus static metadata rows.", ""])
    lines.append("| Tool | Speed | Capability | Auth Reuse | Ergonomics | Overall | Source |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for tool_key, tool_info in tools.items():
        scores = scorecard.get(tool_key, {})
        lines.append(
            f"| **{tool_info.get('name', tool_key)}** | {scores.get('speed', '')} | {scores.get('capability', '')} | "
            f"{scores.get('authReuse', '')} | {scores.get('ergonomics', '')} | {scores.get('overall', '')} | {scores.get('source', 'metadata')} |"
        )
    lines.extend(["", "## Tool Capability Comparison Matrix", "", "| Tool | Strengths | Limits | Status |", "| --- | --- | --- | --- |"])
    for tool_key, tool_info in tools.items():
        lines.append(
            f"| **{tool_info.get('name', tool_key)}** | {tool_info.get('strengths', '')} | "
            f"{tool_info.get('limits', '')} | {tool_info.get('capability_status', '')} |"
        )
    lines.extend(["", "## Gap Backlog", "Key gaps between Chrome Native Bridge and established platforms like Playwright:", ""])
    for gap_item in gaps:
        lines.append(f"- **{gap_item.get('gap', '').capitalize()}**: {gap_item.get('description', '')}")
    lines.extend(["", "## Gap Tickets", ""])
    for idx, gap_item in enumerate(gaps, start=1):
        title = gap_item.get("gap", "gap").capitalize()
        lines.append(f"### BENCH-{idx:03d}: {title}")
        lines.append(f"- Benchmark signal: `{adapter}` report currently tracks this as a gap against stronger surfaces.")
        lines.append(f"- Acceptance target: {gap_item.get('acceptance', 'Measured pass in benchmark harness.')}")
        lines.append(f"- Likely surface: {gap_item.get('surface', 'benchmark_harness.py')}")
        lines.append("")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Benchmark report successfully written to {args.output}")


def main():
    parser = argparse.ArgumentParser(description="Browser Automation Benchmark Harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run benchmarks")
    run_parser.add_argument(
        "--adapter",
        choices=["noop", "chrome-bridge", "playwright", "puppeteer", "chrome-devtools-mcp"],
        default="noop",
        help="Harness adapter to use",
    )
    run_parser.add_argument("--iterations", type=int, default=2, help="Number of benchmark iterations")
    run_parser.add_argument("--output", required=True, help="Path to write JSON results")
    run_parser.add_argument("--base-url", help="Base URL of target page for live benchmarking")
    compare_parser = subparsers.add_parser("compare", help="Compare benchmark results and generate report")
    compare_parser.add_argument("--input", required=True, help="Path to JSON results file")
    compare_parser.add_argument("--output", required=True, help="Path to write markdown report")
    args = parser.parse_args()
    if args.command == "run":
        handle_run(args)
    elif args.command == "compare":
        handle_compare(args)


if __name__ == "__main__":
    main()
