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
- Python 3.9+ for the core bridge and CLI; Python 3.10+ for the MCP server (`mcp/`).
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

## Rust host (parity port)

`host-rs/` is a behavior-identical Rust port of `bridge.py` (same MV3 extension, same native-messaging framing, same token-gated `127.0.0.1:9223` TCP API). The Python host remains the reference; the Rust host is a drop-in replacement for the native-host process only.

### Build

```bash
cargo build --release --manifest-path host-rs/Cargo.toml
```

Produces `host-rs/target/release/bridge-host`.

### Register

```bash
./setup-rs.sh <extension-id>
```

Registers the Rust binary as the native host. It reuses the same `bridge_token.txt` and the same `com.automation.bridge` host name, so the unchanged extension talks to it transparently. Only one host (Python or Rust) can own the `com.automation.bridge` registration / port `9223` at a time.

### Verify

Build first, then run the parity checks:

```bash
PYTHONDONTWRITEBYTECODE=1 ./verify_rust_host.py
```

This runs the same framing/auth/large-payload parity checks (ping/pong, 500KB round-trip, invalid-token rejection) against the Rust host on port `9225`.

The Rust host honors the same env vars: `BRIDGE_PORT` (default 9223), `BRIDGE_TOKEN_FILE`, `BRIDGE_LOG_FILE`.

## MCP server

`mcp/` exposes the bridge to MCP clients (Claude Desktop, Cursor, Cline) so an agent drives your real, logged-in Chrome profile through the standard Model Context Protocol. It is a pure client of the token-gated `127.0.0.1:9223` TCP API; the extension, wire protocol, and host are unchanged.

The server reuses `test_client.py`'s transport verbatim, so the MCP tools and the CLI stay in lockstep.

### Tools

P2 ships a grouped tool set. Tab-scoped tools take an optional `tab_id`; omit it to target the active tab.

Read-only:

- `browser_list_tabs`
- `browser_snapshot` (accessibility snapshot)
- `browser_extract_text`
- `browser_screenshot` (returned inline as an image)
- `browser_get_html`
- `browser_wait_for` (`mode`: `load|selector|text|url`)

Sensitive:

- `browser_get_cookies`

Mutating:

- `browser_navigate`
- `browser_click`, `browser_type`, `browser_fill`, `browser_hover`
- `browser_scroll`, `browser_press`, `browser_drag`
- `browser_select`
- `browser_upload_file` (validates local paths before contacting Chrome)
- `browser_tab_control` (`op`: `activate|close|reload|back|forward`)

Escape hatch (sensitive):

- `browser_action` — escape hatch for any raw bridge action (interception, geolocation, monitoring, console/network logs, `downloadUrl`, `storageState`, `executeScript`, `setViewport`, `handleDialog`, `batch`, ...)

### Resources

- `browser://tabs` — live tab list.
- `browser://tab/{id}/state` — current state of a tab.

### Scoping

The server reads two env flags to scope the exposed surface:

- `BRIDGE_MCP_READONLY=1` registers only the read-only tools, hiding navigate/click/type/upload, tab mutations, and `browser_action`.
- `BRIDGE_MCP_ALLOW_SENSITIVE=1` is required to expose sensitive tools (`browser_get_cookies` and the `browser_action` escape hatch), which are hidden by default.

Tools carry `readOnly`/`destructive` annotations so clients can prompt appropriately.

### Register

Copy `mcp/claude_desktop_config.example.json` into your MCP client config and set the absolute paths:

```json
{
  "mcpServers": {
    "chrome-bridge": {
      "command": "uvx",
      "args": ["--from", "/ABSOLUTE/PATH/TO/chrome-native-bridge/mcp", "chrome-bridge-mcp"],
      "env": {
        "BRIDGE_REPO_ROOT": "/ABSOLUTE/PATH/TO/chrome-native-bridge",
        "BRIDGE_PORT": "9223"
      }
    }
  }
}
```

The server honors `BRIDGE_PORT`, `BRIDGE_TOKEN_FILE`, `BRIDGE_CONNECT_TIMEOUT_SECONDS`, `BRIDGE_MCP_READONLY`, and `BRIDGE_MCP_ALLOW_SENSITIVE`, and reads the same `bridge_token.txt`. Chrome with the loaded extension must be running and the native host registered (`./setup.sh` or `./setup-rs.sh`).

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

### Persistent in-process client

The benchmark harness talks to the bridge over one keep-alive TCP connection (see `BridgeClient` in `benchmark_harness.py`) instead of spawning `python3 test_client.py` per operation. The native host (`bridge.py`) serves many newline-delimited requests per connection, awaiting each extension response on a per-request queue before reading the next, so request/response order is preserved on a shared socket.

This removed the per-operation Python interpreter startup (~30 ms) and TCP handshake that dominated latency. Median-of-medians dropped from ~41 ms to ~6 ms, and pure-overhead ops (`wait-selector`, `get-html`, `performance-metrics`) fell to ~2 ms — essentially one socket round trip. The CLI (`test_client.py`) still uses one connection per command; the persistent client is the harness/agent fast path. Set `CHROME_BRIDGE_CLIENT` to force the harness back onto an external launcher.

### Batched bridge actions

Multi-step Chrome Bridge operations (console/network monitoring, dialog handling) use a composite `batch` action so several sub-commands run in a single native-message round trip. The batch fails as a whole if any sub-command throws or returns `success: false`.

```bash
python3 test_client.py batch '[{"action":"startMonitoring"},{"action":"click","payload":{"selector":"#log"}},{"action":"consoleMessages","delayMs":100}]' <tabId>
```

Batching collapses the three sub-commands of each monitoring op into one round trip; the residual ~100 ms is the deliberate `delayMs` settle window, not transport.

### Measured head-to-head

Median ms per operation, 5 iterations, identical local HTTP fixture, all three adapters run back-to-back in one session, all 18 ops `pass` for every adapter. Navigation is normalized to `domcontentloaded` so `wait-load` is comparable.

| Operation | Chrome Bridge | Playwright | Puppeteer |
| --- | ---: | ---: | ---: |
| ping | 4.83 | 1.97 | 0.34 |
| navigate | 16.36 | 10.93 | 7.96 |
| wait-load | 255.97 | 5.19 | 18.84 |
| wait-selector | 1.96 | 11.29 | 5.69 |
| click | 9.71 | 26.38 | 8.87 |
| fill | 3.47 | 3.58 | 1.26 |
| select | 2.48 | 2.61 | 1.51 |
| upload | 7.62 | 6.18 | 6.42 |
| screenshot | 51.06 | 61.32 | 28.26 |
| extract-text | 2.35 | 1.38 | 2.32 |
| get-html | 2.44 | 1.41 | 0.58 |
| observe-state | 4.35 | 1.72 | 2.19 |
| console-monitoring | 106.26 | 72.85 | 57.70 |
| network-monitoring | 105.96 | 83.63 | 55.83 |
| dialog-handling | 104.40 | 32.29 | 14.21 |
| storage-state | 3.65 | 2.12 | 0.54 |
| geolocation | 17.43 | 11.75 | 1.19 |
| performance-metrics | 1.91 | 0.95 | 0.35 |

Median-of-medians: Chrome Bridge ~6.2 ms, Playwright ~5.7 ms, Puppeteer ~4.0 ms. With the persistent client, Chrome Bridge is competitive with the in-process drivers rather than multiples slower; it wins `wait-selector` outright and beats Playwright on `click` (though it trails Puppeteer there slightly). Two real gaps remain: `wait-load` (~256 ms — `waitForLoad` polls more conservatively than Playwright's load-state signal) and the monitoring ops (the 100 ms settle window). The earlier "~41 ms, 4x slower" figure was per-operation subprocess spawn, now eliminated. Timings vary with machine load; rerun locally for current numbers.

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
