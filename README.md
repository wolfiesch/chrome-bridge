# Chrome Native Messaging Automation Bridge

A custom Chrome MV3 extension plus Python native-messaging host lets local scripts drive Chrome without `--remote-debugging-port`, so Chrome never shows the focus-stealing remote-debugging popup. Automation runs inside your real, logged-in profile.

The bridge exposes an agent-ready browser-control surface: navigation and history, tab lifecycle, waits, scrolling, screenshots, content extraction, keyboard/pointer primitives, forms and file uploads, viewport control, and console/network/dialog diagnostics.

## Layout

```text
chrome-native-bridge/
├── extension/                          <- LOAD THIS in chrome://extensions
│   ├── manifest.json
│   └── background.js
├── background.js                       <- editable source, sync into extension/ after edits
├── manifest.json                       <- editable source, sync into extension/ after edits
├── bridge.py                           <- native host
├── com.automation.bridge.json.template <- host-manifest template (setup.sh fills it in)
├── setup.sh                            <- generates token, renders + registers host manifest
├── test_client.py                      <- CLI client
├── benchmark_harness.py                <- benchmark and comparison harness
├── verify_bridge.py                    <- offline framing/auth test
├── verify_cli_contract.py              <- offline CLI dispatch test
├── verify_heartbeat_contract.py        <- offline heartbeat/structure test
├── verify_benchmark_harness.py         <- offline benchmark contract test
├── verify_agent_actions_live.py        <- manual live browser gate
└── README.md
```

`setup.sh` generates `bridge_token.txt` (0600 shared secret) and `com.automation.bridge.json`. Both are git-ignored and stay local. Keep Python files out of `extension/`: running them creates `__pycache__`, and Chrome refuses to load extension folders containing `_`-prefixed names.

## Components

| File | Role |
|---|---|
| `extension/manifest.json` | MV3 extension manifest. No fixed `key`, so Chrome assigns your own extension ID on load. |
| `extension/background.js` | Service worker: connects to the native host, runs browser actions, and uses `chrome.alarms` plus heartbeat messages to self-heal after idle or sleep. |
| `bridge.py` | Native host. Talks to Chrome over stdio and exposes a token-gated TCP server on `127.0.0.1:9223` for local clients. |
| `com.automation.bridge.json.template` | Host-manifest template. `setup.sh` substitutes the absolute `bridge.py` path and your extension ID. |
| `test_client.py` | Positional CLI client (`python3 test_client.py <action> ...`). |

## Requirements

- Google Chrome, Chrome Beta/Canary, or Chromium with Developer mode.
- Python 3.9+.
- macOS or Linux. `setup.sh` auto-registers the native host for each installed variant. Windows works but requires manual native-host registration.

## Setup

1. Open `chrome://extensions/` and enable Developer mode.
2. Load unpacked extension folder: this repo's `extension/`. Copy the assigned extension ID.
3. Register the native host with that ID:
   ```bash
   ./setup.sh <extension-id>
   ```
   This generates a fresh local token and registers the host for your OS.
4. Enable only one bridge extension at a time. Duplicate bridge extensions race to bind port `9223`.
5. Verify:
   ```bash
   python3 test_client.py ping
   ```
   Expected: `{"success": true, "result": "pong"}`.

## Command reference

Examples below write `chrome-bridge <action>` as shorthand for `python3 test_client.py <action>`. Symlink `test_client.py` onto your `PATH` as `chrome-bridge` if you want the short form literally.

### Core

```bash
chrome-bridge ping
chrome-bridge navigate <url>
chrome-bridge getTabs
chrome-bridge getCookies <domain>
chrome-bridge executeScript <tabId> <code>
chrome-bridge executeScriptCDP <tabId> <code>
chrome-bridge observe <tabId>
```

### Navigation and tabs

```bash
chrome-bridge activateTab <tabId>
chrome-bridge closeTab <tabId>
chrome-bridge reload <tabId>
chrome-bridge goBack <tabId>
chrome-bridge goForward <tabId>
```

### Waits

```bash
chrome-bridge waitForLoad <tabId> [timeoutMs]
chrome-bridge waitForSelector <tabId> <selector> [timeoutMs]
chrome-bridge waitForText <tabId> <text> [timeoutMs]
chrome-bridge waitForUrl <tabId> <substring> [timeoutMs]
```

### Page state and content

```bash
chrome-bridge getCurrentState <tabId>
chrome-bridge screenshot <tabId> <outputPath>
chrome-bridge extractText <tabId> [maxChars]
chrome-bridge getHTML <tabId> <outputPath>
```

`screenshot` writes a PNG file and prints path, MIME type, and byte count only. `getHTML` writes UTF-8 HTML to a file and prints path and byte count only.

### Pointer, keyboard, and forms

```bash
chrome-bridge click <tabId> <selector>
chrome-bridge type <tabId> <selector> <text>
chrome-bridge hover <tabId> <selector>
chrome-bridge scroll <tabId> <deltaX> <deltaY> [selector]
chrome-bridge press <tabId> <keySpec>
chrome-bridge drag <tabId> <fromSelector> <toSelector>
chrome-bridge fill <tabId> <selector> <text>
chrome-bridge select <tabId> <selector> <value>
chrome-bridge uploadFile <tabId> <selector> <path...>
```

`type` focuses and inserts text. `fill` clears first, then inserts text. `uploadFile` expands local paths and fails before contacting Chrome when any file is missing.

### Viewport

```bash
chrome-bridge setViewport <tabId> <width> <height> [deviceScaleFactor]
```

### Diagnostics, interception, downloads, storage, geolocation, and metrics

```bash
chrome-bridge startMonitoring <tabId>
chrome-bridge stopMonitoring <tabId>
chrome-bridge consoleMessages <tabId>
chrome-bridge networkRequests <tabId>
chrome-bridge handleDialog <tabId> accept|dismiss [promptText]
chrome-bridge startInterception <tabId> <urlPattern> continue|abort|fulfill [status] [body]
chrome-bridge stopInterception <tabId>
chrome-bridge interceptedRequests <tabId>
chrome-bridge downloadUrl <url> [filename]
chrome-bridge storageState <tabId> <outputPath>
chrome-bridge setGeolocation <tabId> <latitude> <longitude> [accuracy]
chrome-bridge clearGeolocation <tabId>
chrome-bridge performanceMetrics <tabId>
```

`startMonitoring` leaves Chrome's debugger attached to the tab until `stopMonitoring`, so Chrome's debugger infobar may persist on monitored tabs. `startInterception` leaves Fetch/debugger attached until `stopInterception`. `networkRequests` and `interceptedRequests` store URLs as origin plus pathname and report `hasQuery` instead of query strings. `downloadUrl` writes into Chrome's configured download location; Chrome rejects arbitrary absolute output paths. `storageState` writes cookies, localStorage, and sessionStorage to disk and prints metadata only. `setGeolocation` grants geolocation for the tab origin through Chrome content settings, applies a CDP geolocation override, and `clearGeolocation` resets that origin to `ask`.

## Raw-output safety

These commands can reveal private browsing context:

- `getTabs`
- `getCurrentState`
- `extractText`
- `getHTML`
- `screenshot`
- `consoleMessages`
- `networkRequests`
- `interceptedRequests`
- `storageState`
  - Raw output is written to the requested file and may include cookies, localStorage, and sessionStorage.
  - Do not paste the file contents into transcripts.
Never paste raw cookies, raw tab URLs/titles, screenshot contents, raw HTML, or network URLs into transcripts unless the user explicitly asks for that output.

Use redacted summaries:

```bash
chrome-bridge getTabs | python3 -c 'import sys,json,urllib.parse as u; d=json.load(sys.stdin); tabs=d.get("result", []); print("success:", d.get("success")); print("tab_count:", len(tabs) if isinstance(tabs, list) else tabs); print("active_domains:", sorted({u.urlparse(t.get("url","")).netloc for t in tabs if isinstance(t, dict) and t.get("active")}))'
```

```bash
chrome-bridge networkRequests <tabId> | python3 -c 'import sys,json; d=json.load(sys.stdin); reqs=d.get("result",{}).get("requests",[]); print("success:", d.get("success")); print("request_count:", len(reqs)); print("paths:", sorted({r.get("url","") for r in reqs if isinstance(r, dict)})[:10]); print("any_query:", any(r.get("hasQuery") for r in reqs if isinstance(r, dict)))'
```

Cookie checks should print counts and names only:

```bash
chrome-bridge getCookies "github.com" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("result", []); print("success:", d.get("success")); print("cookie_count:", len(r) if isinstance(r, list) else r); print("cookie_names:", sorted(c.get("name","") for c in r) if isinstance(r, list) else [])'
```

## Verification

Offline checks (no browser needed), run from the repo root:

```bash
PYTHONDONTWRITEBYTECODE=1 ./verify_cli_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_heartbeat_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_bridge.py
PYTHONDONTWRITEBYTECODE=1 ./verify_benchmark_harness.py
python3 benchmark_harness.py run --adapter noop --iterations 2 --output /tmp/results.json
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile bridge.py test_client.py benchmark_harness.py verify_bridge.py verify_cli_contract.py verify_heartbeat_contract.py verify_benchmark_harness.py verify_agent_actions_live.py verify_capability_matrix.py
node --check background.js
diff -q manifest.json extension/manifest.json
diff -q background.js extension/background.js
```

Manual live gates after reloading the unpacked extension (opens real Chrome tabs):

```bash
python3 test_client.py ping
PYTHONDONTWRITEBYTECODE=1 ./verify_agent_actions_live.py
PYTHONDONTWRITEBYTECODE=1 ./verify_capability_matrix.py
```

`verify_capability_matrix.py` binds its HTTP fixture to port `0`, derives the URL at runtime, writes screenshots/HTML/storage to temp files, and prints compact redacted JSON.

## Benchmarking against other browser automation surfaces

The benchmark harness measures speed for the selected adapter. `chrome-bridge`, `playwright`, and `puppeteer` are live-measurable; Claude in Chrome, Codex Chrome extension, and Chrome DevTools MCP remain static capability metadata until adapters exist. The report also emits a normalized scorecard and gap tickets.

Run the offline contract adapter:

```bash
python3 benchmark_harness.py run --adapter noop --iterations 2 --output /tmp/results.json
```

Run measured adapters:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --output /tmp/chrome-bridge-results.json
python3 benchmark_harness.py run --adapter playwright --iterations 5 --output /tmp/playwright-results.json
python3 benchmark_harness.py run --adapter puppeteer --iterations 5 --output /tmp/puppeteer-results.json
```

`chrome-bridge`, `playwright`, and `puppeteer` start a local HTTP fixture by default. To benchmark another page, pass `--base-url`:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --base-url http://127.0.0.1:PORT/ --output /tmp/results.json
```

Missing optional dependencies or browser binaries are reported as unsupported/fail in the adapter output without breaking the noop/offline checks.

Generate the Markdown report:

```bash
python3 benchmark_harness.py compare --input /tmp/results.json --output /tmp/report.md
```

### Batched bridge actions

Multi-step Chrome Bridge operations (console/network monitoring, dialog handling) use a composite `batch` action so several sub-commands run in a single native-message round trip instead of one CLI spawn and TCP round trip each. The batch fails as a whole if any sub-command throws or returns `success: false`.

```bash
python3 test_client.py batch '[{"action":"startMonitoring"},{"action":"click","payload":{"selector":"#log"}},{"action":"consoleMessages","delayMs":100}]' <tabId>
```

Batching cut each monitoring op from roughly three round trips to one (~605 ms to ~142 ms, ~77% lower) and dropped the full Chrome Bridge run from ~34 s to ~8 s.

### Measured head-to-head

Median ms per operation, 5 iterations, identical local HTTP fixture, all 18 ops `pass` for every adapter. Navigation is normalized to `domcontentloaded` across adapters so `wait-load` is comparable.

| Operation | Chrome Bridge | Playwright | Puppeteer |
| --- | ---: | ---: | ---: |
| ping | 37.05 | 3.97 | 2.79 |
| navigate | 48.29 | 10.85 | 67.03 |
| wait-load | 292.80 | 8.97 | 76.26 |
| wait-selector | 38.74 | 28.02 | 45.39 |
| click | 39.88 | 40.04 | 73.85 |
| fill | 38.95 | 6.21 | 25.10 |
| select | 39.01 | 4.92 | 24.09 |
| upload | 46.97 | 44.47 | 21.62 |
| screenshot | 89.09 | 51.30 | 32.89 |
| extract-text | 38.11 | 3.98 | 3.64 |
| get-html | 37.00 | 2.45 | 0.93 |
| observe-state | 39.39 | 2.21 | 4.63 |
| console-monitoring | 142.27 | 90.29 | 64.13 |
| network-monitoring | 140.71 | 83.37 | 63.94 |
| dialog-handling | 140.47 | 29.87 | 42.22 |
| storage-state | 42.26 | 3.54 | 6.63 |
| geolocation | 85.06 | 9.18 | 8.96 |
| performance-metrics | 38.85 | 1.93 | 1.19 |

Median-of-medians: Chrome Bridge ~41 ms, Puppeteer ~25 ms, Playwright ~9 ms. The Chrome Bridge gap is structural: each command crosses CLI -> TCP -> native host -> extension -> CDP, while Playwright/Puppeteer hold one in-process CDP connection. Chrome Bridge's edge is real-profile auth reuse, not raw latency. Timings vary with machine load; rerun locally for current numbers.

## Troubleshooting

The host writes a local `bridge_debug.log` (git-ignored) next to `bridge.py`:

```bash
tail -f bridge_debug.log
```

- `Connection refused` after retry: Chrome is closed, no bridge extension is enabled, or the service worker did not wake.
- `FATAL: could not bind 127.0.0.1:9223`: two bridge extensions are enabled.
- `unauthorized`: token mismatch. Re-run `./setup.sh <extension-id>` and reload the extension.

## Security notes

- TCP API is localhost-only and requires the shared token.
- Payload bodies such as cookies and DOM are not logged by the host.
- `executeScript` uses `chrome.scripting` in the page MAIN world and can be blocked by strict page CSP.
- `executeScriptCDP`, browser interactions, waits, screenshots, viewport control, monitoring, interception, geolocation, and performance metrics use `chrome.debugger`.
- `downloads`, `contentSettings`, `host_permissions: <all_urls>`, cookie access, debugger access, and script execution are powerful. Use this profile for trusted automation only.
- The bridge still intentionally lacks Playwright-style isolated browser contexts/profiles and multi-browser support; it controls the real Chrome profile.
