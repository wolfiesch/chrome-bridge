# Chrome Native Messaging Automation Bridge

A custom Chrome MV3 extension plus a native-messaging host lets trusted local clients drive Chrome without `--remote-debugging-port`, so Chrome never shows the focus-stealing remote-debugging popup. Automation runs inside your real, logged-in profile.

The bridge exposes an agent-ready browser-control surface: navigation and history, tab lifecycle, waits, scrolling, screenshots, content extraction, keyboard/pointer primitives, forms and file uploads, viewport control, and console/network/dialog diagnostics.

## Layout

```text
chrome-native-bridge/
├── extension/                          <- public unkeyed extension source copy
│   ├── manifest.json
│   ├── background.js
│   ├── wake.html
│   └── wake.js
├── background.js                       <- editable service-worker source
├── wake.html / wake.js                 <- extension wake page for suspended-worker recovery
├── manifest.json                       <- public unkeyed source manifest
├── extension_identity.py               <- local key and extension ID helper
├── bridge.py                           <- native host
├── broker.py                           <- opt-in launchd TCP broker for stable client port 9223
├── bridge_policy.example.json          <- explicit opt-in policy template
├── com.automation.bridge.json.template <- host-manifest template (setup.sh fills it in)
├── setup.sh / setup-rs.sh              <- generates token/policy, deploys extension, registers host
├── setup-broker.sh                     <- installs launchd broker mode on macOS
├── uninstall-broker.sh                 <- stops launchd broker mode
├── bridge_wake.py                      <- shared wake-page discovery/opening helper
├── test_client.py                      <- CLI client
├── benchmark_harness.py                <- benchmark and comparison harness
├── verify_bridge.py                    <- offline framing/auth test
├── verify_cli_contract.py              <- offline CLI dispatch test
├── verify_heartbeat_contract.py        <- offline heartbeat/structure test
├── verify_benchmark_harness.py         <- offline benchmark contract test
├── verify_install_contract.py           <- offline install/identity contract test
├── verify_agent_actions_live.py        <- manual live browser gate
└── README.md
```

`setup.sh` generates `bridge_token.txt` (0600 shared secret), installs `bridge_policy.json` from `bridge_policy.example.json` when absent, deploys a local keyed extension manifest, and writes `com.automation.bridge.json`. `setup-rs.sh` additionally generates `com.automation.bridge.rust.json` and the `bridge-host-launch.sh` wrapper. The optional `bridge_tokens.txt` named-token registry (see Multi-client tokens and leasing) is also a local secret. All of these are git-ignored and stay local. Keep Python files out of Chrome-loaded extension directories: running them creates `__pycache__`, and Chrome refuses folders containing `_`-prefixed names.

## Components

| File | Role |
|---|---|
| `extension/manifest.json` | Public unkeyed MV3 source manifest. `setup.sh` and `deploy.sh --with-local-key` write a keyed copy into the local extension directory for a deterministic unpacked ID. |
| `extension/background.js` | Service worker: connects to the native host, runs browser actions, uses `chrome.alarms` plus heartbeat messages to self-heal after idle or sleep, and handles wake-page messages. |
| `wake.html`, `wake.js` | Minimal extension page opened by the CLI after `ECONNREFUSED`; it messages the service worker to call `connectNative()` and then closes its tab. |
| `bridge.py` | Native host. Talks to Chrome over stdio and exposes a token-gated TCP server on `127.0.0.1:9223` for local clients. |
| `com.automation.bridge.json.template` | Host-manifest template. `setup.sh` substitutes the absolute host path and local or packaged extension ID. |
| `test_client.py` | Positional CLI client (`python3 test_client.py <action> ...`). |
| `.github/workflows/ci.yml` | Pull-request and `main` push gates for syntax, offline contracts, Rust parity, benchmarks, and packaging checks. |
| `.github/workflows/release.yml` | Tag-driven release workflow for `v*` tags after the CI command set passes. |
| `scripts/package_release.py` | Stdlib release packager for source archives, unpacked extension bundles, and Rust host binaries. |

## Requirements

- Google Chrome, Chrome Beta, or Chromium with Developer mode. The macOS installer also registers Chrome Canary.
- Python 3.9+ for the core bridge and CLI; Python 3.10+ for the MCP server (`mcp/`, matching `mcp/pyproject.toml`).
- macOS or Linux for the documented `setup.sh` and `setup-rs.sh` native-host installers. Broker mode and `setup-broker.sh` are macOS-only because they use launchd. Other platforms may be possible with manual Chrome native-host registration, but they are not covered by these installers.

## Trusted-local security warning

This bridge controls your real Chrome profile. It can read page content, take screenshots, drive forms, download files, inspect cookies through redacted probes, attach Chrome's debugger, and run script/debugger actions when policy allows them. Install it only for trusted local automation on a machine you control. Keep `bridge_token.txt`, `bridge_tokens.txt`, `extension_key.pem`, `bridge_policy.json`, debug logs, and audit logs private and git-ignored.

The example policy is conservative: built-in defaults allow only `ping`, policy inspection, and lease actions until a local policy opts into more. Some README examples and live gates need explicit action/origin grants for the sites you intend to automate.

## Setup

Default local install:

```bash
./setup.sh
```

The script generates or reuses `extension_key.pem`, deploys `background.js` plus a keyed manifest into a per-user extension directory, registers the native host for that deterministic extension ID, creates `bridge_token.txt` when absent, and installs `bridge_policy.json` from the example template when absent. It prints the extension directory at the end.

Then:

1. Open `chrome://extensions/` and enable Developer mode.
2. Load unpacked: the extension directory printed by `./setup.sh`.
3. Enable only one bridge extension at a time. Duplicate bridge extensions race to bind port `9223`.
4. Verify:
   ```bash
   python3 test_client.py ping
   python3 test_client.py policyCheck getTabs '{}'
   ```
   Expected: `ping` succeeds. `policyCheck getTabs '{}'` is allowed when setup installed the example policy.

Advanced setup:

```bash
./setup.sh --extension-id <id>
```

Use this for an already-packaged or future Web Store extension ID. It registers that packaged/store extension ID separately and does not generate or inject a local extension key for the developer-mode unpacked copy.

```bash
cargo build --release --manifest-path host-rs/Cargo.toml
./setup-rs.sh
```

Use this to register the Rust host with the same extension-ID resolution flow.

## Launchd broker mode

Broker mode is optional on macOS. launchd keeps a small Python broker listening on public port `9223`; Chrome-launched Python or Rust native hosts bind backend port `19223`. Clients keep using `BRIDGE_PORT=9223`, or no override. On first install, `setup-broker.sh` seeds the state-dir token from the repo token so the existing `chrome-bridge` CLI keeps working; if both token files already exist and differ, the script warns and clients should set `BRIDGE_TOKEN_FILE` to the state token path.

Install Python-host broker mode:

```bash
./setup-broker.sh --host python
```

Install Rust-host broker mode after building Rust:

```bash
cargo build --release --manifest-path host-rs/Cargo.toml
./setup-broker.sh --host rust
```

After setup completes, load the state-dir extension path printed by `setup-broker.sh` and disable any older bridge extension. Broker mode uses state under `~/Library/Application Support/chrome-native-bridge` by default, including its own extension key, extension ID, token, policy, and launcher. If you are migrating from a repo-local install, reload exactly the printed state-dir extension so the loaded extension ID matches the broker native-host registration.

Verify the broker process and public endpoint:

```bash
launchctl print gui/$UID/gg.wolfie.chrome-native-bridge.broker
chrome-bridge ping
```

Disable broker mode:

```bash
./uninstall-broker.sh
```

`extension_key.pem` is a private local identity key for the developer-mode unpacked extension. Keep it git-ignored and never commit it. A packaged or Web Store extension has a separate store-managed ID; register that ID with `./setup.sh --extension-id <store-id>`.

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
chrome-bridge githubAttachUploadedFiles <tabId> <inputSelector> [formSelector] [timeoutMs]
chrome-bridge githubSubmitComment <tabId> [formSelector] [timeoutMs]
```

`type` focuses and inserts text. `fill` clears first, then inserts text. `click`, `type`, `hover`, `drag`, `fill`, `select`, and `uploadFile` accept plain CSS plus semantic selector prefixes: `css=<selector>`, `label=<text>`, `text=<text>`, and `role=<role>[name=<accessible-name>]`. Use `<host> >>> <shadow-selector>` for open shadow DOM and `frame=<iframe-selector> >> <target-selector>` for iframe targets; these forms also work for `<select>` elements and file inputs. `uploadFile` expands local paths and fails before contacting Chrome when any file is missing.

For GitHub comments, use `uploadFile` first, then `githubAttachUploadedFiles` to call GitHub's `<file-attachment>` component without opening arbitrary `executeScript*` access. Use `githubSubmitComment` instead of a broad submit-button click on draft PRs; it only clicks an exact `Comment` or `Add comment` button and refuses `Close with comment`. Both GitHub-specific actions also verify the target tab is on `https://github.com`.

### Viewport

```bash
chrome-bridge setViewport <tabId> <width> <height> [deviceScaleFactor]
```

### Emulation

```bash
chrome-bridge setCpuThrottling <tabId> <rate>
chrome-bridge setNetworkConditions <tabId> <offline:0|1> [latencyMs] [downBps] [upBps]
chrome-bridge clearNetworkConditions <tabId>
chrome-bridge setColorScheme <tabId> light|dark|no-preference
chrome-bridge setUserAgent <tabId> <userAgent>
```

`setCpuThrottling` sets Chrome's CPU throttling rate; use `rate >= 1`, with `1` disabling throttling. `setNetworkConditions` applies CDP `Network.emulateNetworkConditions` and persists until `clearNetworkConditions` resets it. `setColorScheme` overrides `prefers-color-scheme`. `setUserAgent` overrides the tab's user agent string.

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
chrome-bridge policyCheck <action> [payloadJson]
chrome-bridge policy info
chrome-bridge policy show
chrome-bridge policy doctor
chrome-bridge policy allow-action <action> [client]
chrome-bridge policy allow-origin <pattern> [client]
```

`startMonitoring` leaves Chrome's debugger attached to the tab until `stopMonitoring`, so Chrome's debugger infobar may persist on monitored tabs. `startInterception` leaves Fetch/debugger attached until `stopInterception`. `networkRequests` and `interceptedRequests` store URLs as origin plus pathname and report `hasQuery` instead of query strings. `downloadUrl` writes into Chrome's configured download location; Chrome rejects arbitrary absolute output paths. `storageState` writes cookies, localStorage, and sessionStorage to disk and prints metadata only. `setGeolocation` grants geolocation for the tab origin through Chrome content settings, applies a CDP geolocation override, and `clearGeolocation` resets that origin to `ask`.

`policyCheck` is host-side and never forwards to Chrome: it reports what `bridge_policy.json` would decide (`allowed`, `reason`, `confirmationRequired`, `redact`, `audit`) for the given action/payload. Tab-scoped actions also include `originDependent: true` because the live tab origin is additionally checked at forward time.

The `policy` subcommands let an agent self-service policy when an action is denied. `policy info` asks the host for the active `bridge_policy.json` / audit-log paths (always answerable, even under a deny-all policy, and it never returns policy contents over the wire). `policy show` prints the local policy file; `policy doctor` reads recent deny events from the audit log and proposes the precise fix for each: a `policy allow-action`/`policy allow-origin` command when an item is missing from an allow-list (`not allowed`), or a manual deny-list edit when a deny-list pattern matched (`denied`, which a grant cannot override). `policy allow-action`/`policy allow-origin` edit the policy file in place (mode `600`); with no client argument they edit the section the host says governs this client, and an explicitly named client always edits its own `clients.<name>` section so a new name never silently broadens the shared `default`. Every deny response also carries a structured `policyDenial` companion (`kind`, `suggestedPatch`, `policyFile`, `batchStep`) alongside the byte-stable `policy denied: <reason>` error string.

### Real-profile moat: session probe and human handoff

These two commands exploit what sets this bridge apart from Playwright/Puppeteer: it drives your **real, already-logged-in Chrome profile**, so existing sessions (cookies, SSO, passkeys) are ambient. Neither command ever reads, imports, or overwrites cookie values — they only observe and hand control to you.

```bash
chrome-bridge sessionStatus <domain> [<domain> ...]
chrome-bridge waitForHandoff <message> [mode] [selectorOrUrlOrText] [timeoutMs] [tabId]
```

`sessionStatus` is a **redacted auth probe**: for each domain it reports cookie count, cookie *names* (never values), whether a session/auth cookie is present, and a `loggedIn` boolean — enough to decide "is this profile already signed in to X?" without exposing secrets. Treat its output as sensitive: cookie names plus logged-in status can reveal which accounts and sites the profile uses.

`waitForHandoff` **pauses automation and hands control to you**: it focuses the target tab, shows an in-page banner with your `message`, and blocks until the page reaches an expected state, then resumes the agent. Use it for interactive steps an agent should not perform — login, 2FA, captcha, payment confirmation. `mode` is `manual` (default; resolves when you change the page), `selector`, `url`, or `text`; the positional argument after `mode` is the selector/URL-substring/text to wait for. `timeoutMs` defaults to 120000. The CLI raises its socket read timeout to cover the wait, so long handoffs do not time out in transport. Under MCP auto-lease, the cooperative lease is extended to span the whole handoff window so another agent cannot mutate the profile while you are acting.

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
PYTHONDONTWRITEBYTECODE=1 ./verify_broker_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_bridge.py
PYTHONDONTWRITEBYTECODE=1 ./verify_benchmark_harness.py
PYTHONDONTWRITEBYTECODE=1 ./verify_moat_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_guardrails_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_install_contract.py
python3 benchmark_harness.py run --adapter noop --iterations 2 --output /tmp/results.json
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile bridge.py broker.py bridge_wake.py test_client.py benchmark_harness.py extension_identity.py verify_bridge.py verify_cli_contract.py verify_broker_contract.py verify_heartbeat_contract.py verify_benchmark_harness.py verify_install_contract.py verify_agent_actions_live.py verify_capability_matrix.py
node --check background.js
node --check wake.js
diff -q manifest.json extension/manifest.json
diff -q background.js extension/background.js
diff -q wake.html extension/wake.html
diff -q wake.js extension/wake.js
```

Manual live gates after reloading the unpacked extension (opens real Chrome tabs):

```bash
python3 test_client.py ping
PYTHONDONTWRITEBYTECODE=1 ./verify_live_install_smoke.py
PYTHONDONTWRITEBYTECODE=1 ./verify_agent_actions_live.py
PYTHONDONTWRITEBYTECODE=1 ./verify_capability_matrix.py
```

`verify_capability_matrix.py` skips `downloadUrl` by default in live profiles because Chrome's "Ask where to save each file before downloading" setting can open a modal save dialog and block unattended smoke runs. To exercise that capability intentionally, run:

```bash
CHROME_BRIDGE_TEST_DOWNLOAD=1 PYTHONDONTWRITEBYTECODE=1 ./verify_capability_matrix.py
```

`verify_live_install_smoke.py` uses a temporary HOME/XDG_CONFIG_HOME and exits 0 with `SKIP live install smoke: Chrome/Chromium executable not found` only when no Chrome/Chromium executable is available.

The default sample policy is intentionally fail-closed and denies loopback URLs. For these localhost live gates, temporarily use an explicit smoke-test policy, then restore your normal policy:

```json
{
  "default": {
    "allowedActions": ["*"],
    "allowedOrigins": ["http://127.0.0.1:*"],
    "deniedActions": [],
    "deniedOrigins": [],
    "requireConfirmation": [],
    "redact": true,
    "audit": true
  }
}
```

`verify_capability_matrix.py` binds its HTTP fixture to port `0`, derives the URL at runtime, writes screenshots/HTML/storage to temp files, and prints compact redacted JSON.

## Release packaging

Pull requests run `.github/workflows/ci.yml`. Tags that match `v*` run `.github/workflows/release.yml`.

The extension artifact is an unpacked, developer-mode bundle and remains unkeyed. A packaged or Web Store extension uses its own store-managed ID and must be registered separately:

```bash
./setup.sh --extension-id <store-id>
```

Build local release artifacts with:

```bash
python3 scripts/package_release.py --version <version> --dist dist
```

## Rust host (parity port)

`host-rs/` is a behavior-identical Rust port of `bridge.py` (same MV3 extension, same native-messaging framing, same token-gated `127.0.0.1:9223` TCP API). The Python host remains the reference; the Rust host is a drop-in replacement for the native-host process only.

### Build

```bash
cargo build --release --manifest-path host-rs/Cargo.toml
```

Cargo may place the binary under the target directory reported by `cargo metadata`; `setup-rs.sh` resolves that path automatically.

### Register

```bash
./setup-rs.sh
./setup-rs.sh --extension-id <id>
```

Registers the Rust binary as the native host. Because native-messaging manifests cannot pass environment variables and the binary otherwise resolves token/log paths relative to its own directory, `setup-rs.sh` generates a small `bridge-host-launch.sh` wrapper that exports the repo-root `BRIDGE_TOKEN_FILE`/`BRIDGE_TOKENS_FILE`/`BRIDGE_LOG_FILE` and registers that launcher. It reuses the same `bridge_token.txt` and the same `com.automation.bridge` host name, so the unchanged extension talks to it transparently. Only one host (Python or Rust) can own the `com.automation.bridge` registration / port `9223` at a time.

### Verify

Build first, then run the parity checks:

```bash
PYTHONDONTWRITEBYTECODE=1 ./verify_rust_host.py
```

This runs the same framing/auth/large-payload parity checks (ping/pong, 500KB round-trip, invalid-token rejection) against the Rust host on port `9225`.

The Rust host honors the same env vars: `BRIDGE_PORT` (default 9223), `BRIDGE_TOKEN_FILE`, `BRIDGE_TOKENS_FILE`, `BRIDGE_LOG_FILE`, and in addition `BRIDGE_POLICY_FILE` (default `bridge_policy.json`) and `BRIDGE_AUDIT_LOG_FILE` (default `bridge_audit.jsonl`). It enforces the same host policy, audit logging, and response redaction as the Python host.

## MCP server

`mcp/` exposes the bridge to MCP clients (Claude Desktop, Cursor, Cline) so an agent drives your real, logged-in Chrome profile through the standard Model Context Protocol. It is a pure client of the token-gated `127.0.0.1:9223` TCP API; the extension, wire protocol, and host are unchanged.

The server reuses `test_client.py`'s transport verbatim, so the MCP tools and the CLI stay in lockstep.

### Tools

The MCP server ships a grouped tool set. Tab-scoped tools take an optional `tab_id`; omit it to target the active tab.

Read-only:

- `browser_list_tabs`
- `browser_snapshot` (accessibility snapshot)
- `browser_extract_text`
- `browser_screenshot` (returned inline as an image)
- `browser_get_html`, `browser_lease_status`
- `browser_policy_check` — ask the host what its policy would decide for an action/payload without forwarding it
- `browser_wait_for` (`mode`: `load|selector|text|url`)

Sensitive:

- `browser_get_cookies`
- `browser_session_status` — redacted auth/session probe (cookie names/counts + `loggedIn` per domain, never values)

Mutating:

- `browser_navigate`
- `browser_click`, `browser_type`, `browser_fill`, `browser_hover`
- `browser_scroll`, `browser_press`, `browser_drag`
- `browser_select`
- `browser_upload_file` (validates local paths before contacting Chrome)
- `browser_tab_control` (`op`: `activate|close|reload|back|forward`), `browser_lease`, `browser_release`
- `browser_set_cpu_throttling`, `browser_set_network_conditions`, `browser_clear_network_conditions`, `browser_set_color_scheme`, `browser_set_user_agent`
- `browser_wait_for_handoff` — pause automation, focus the real tab with an on-page banner, and wait for a human to finish login/2FA/captcha before resuming
- `browser_confirm_action` — resend an action with a host-issued confirmation token

Escape hatch (sensitive):

- `browser_action` — escape hatch for any raw bridge action (interception, geolocation, monitoring, console/network logs, `downloadUrl`, `storageState`, `executeScript`, `setViewport`, `handleDialog`, `batch`, ...)

### Resources

- `browser://tabs` — live tab list.
- `browser://tab/{id}/state` — current state of a tab.

### Scoping

The server reads two env flags to scope the exposed surface:

- `BRIDGE_MCP_READONLY=1` registers only the read-only tools, hiding navigate/click/type/upload, tab mutations, `browser_confirm_action`, and `browser_action`.
- `BRIDGE_MCP_ALLOW_SENSITIVE=1` is required to expose sensitive tools (`browser_get_cookies`, `browser_session_status`, and the raw `browser_action` escape hatch), which are hidden by default. The host policy remains the enforcement boundary even when this escape hatch is exposed.

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

### HTTP transport

By default the server speaks stdio. Set `BRIDGE_MCP_TRANSPORT=http` to serve over streamable HTTP instead, bound to `BRIDGE_MCP_HTTP_HOST` (default `127.0.0.1`) and `BRIDGE_MCP_HTTP_PORT` (default `8723`). Note: the server forwards a single ambient bridge token, so all HTTP clients share one bridge identity. Cooperative leasing (below) arbitrates only between distinct token identities (e.g. separate stdio servers each pointed at their own named token); per-request token propagation over one HTTP endpoint is not yet implemented.

## Multi-client tokens and leasing

The bridge accepts multiple named client tokens and offers a cooperative, host-side lease so several agents can share one real Chrome profile without colliding. Both the Python and Rust hosts implement this identically; it is enforced entirely in the host (lease actions are never forwarded to the extension).

### Named tokens

`bridge_token.txt` (the legacy single token) is always accepted under the client name `default`. Additionally, if `bridge_tokens.txt` (override with `BRIDGE_TOKENS_FILE`) exists, each non-empty, non-`#` line is parsed as `name:token` (split on the first colon) and registered as an extra named client. See `bridge_tokens.txt.example`. A request is authorized if its token matches any known token; the matched token determines the requesting client's name. `bridge_tokens.txt` is a secret registry and is git-ignored.

### Lease protocol

Three host-answered actions (also exposed as MCP tools `browser_lease`, `browser_release`, `browser_lease_status`):

- `lease` — payload optional `{"ttlMs": int}` (default 300000). Acquires the lease when free, expired, or already yours; otherwise returns `leased by <owner>`.
- `release` — releases your lease (`released: true`); `released: false` when no live lease; `not lease owner` when another client holds it.
- `leaseStatus` — non-mutating snapshot `{owner, expiresAt, now}` (epoch ms; `owner` null when unheld).

While a live lease is held, every non-lease action from a different client (including `batch`) is rejected with `leased by <owner>` before forwarding, so the lease cannot be bypassed. Leases auto-expire after their TTL. `BRIDGE_SOCKET_IDLE_TIMEOUT` (default 300s) bounds how long a persistent connection may idle.

`verify_lease_contract.py` covers the basic named-token and lease semantics. `verify_lease_stress_contract.py` adds race/load coverage for simultaneous lease acquisition, non-owner denial without extension forwarding, owner concurrency, TTL expiry, release races, and TCP disconnect behavior.

## Benchmarking against other browser automation surfaces

The benchmark harness measures speed for selected adapters. `chrome-bridge`, `playwright`, `puppeteer`, and `chrome-devtools-mcp` are live-measurable; Claude in Chrome and Codex Chrome Extension remain manual/static capability metadata. The report also emits a normalized scorecard, claim-discipline note, and gap tickets.

Run the offline contract adapter:

```bash
python3 benchmark_harness.py run --adapter noop --iterations 2 --output /tmp/results.json
```

Run measured adapters:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --output /tmp/chrome-bridge-results.json
python3 benchmark_harness.py run --adapter playwright --iterations 5 --output /tmp/playwright-results.json
python3 benchmark_harness.py run --adapter puppeteer --iterations 5 --output /tmp/puppeteer-results.json
python3 benchmark_harness.py run --adapter chrome-devtools-mcp --iterations 5 --output /tmp/chrome-devtools-results.json
```

`chrome-bridge`, `playwright`, `puppeteer`, and `chrome-devtools-mcp` start a local HTTP fixture by default. To benchmark another page, pass `--base-url`:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --base-url http://127.0.0.1:PORT/ --output /tmp/results.json
```

Missing optional dependencies or browser binaries are reported as unsupported/fail in the adapter output without breaking the noop/offline checks. Shadow DOM, iframe, and semantic locator user-action parity are measured as explicit capability rows.

Generate the Markdown report, with optional CI-friendly JUnit XML and GitHub Step Summary outputs:

```bash
python3 benchmark_harness.py compare --input /tmp/results.json --output /tmp/report.md --junit-output /tmp/benchmark.xml --github-step-summary /tmp/summary.md
python3 benchmark_harness.py compare --input /tmp/chrome-bridge-results.json --input /tmp/chrome-devtools-results.json --output /tmp/head-to-head.md
```

### Persistent in-process client

The benchmark harness talks to the bridge over one keep-alive TCP connection (see `BridgeClient` in `benchmark_harness.py`) instead of spawning `python3 test_client.py` per operation. The native host (`bridge.py`) serves many newline-delimited requests per connection, awaiting each extension response on a per-request queue before reading the next, so request/response order is preserved on a shared socket.

This avoids per-operation Python interpreter startup and TCP connection setup in the harness path. Exact latency depends on the local browser profile, machine load, adapter versions, and benchmark run. Generate a fresh report before making comparative speed claims. The CLI (`test_client.py`) still uses one connection per command; the persistent client is the harness/agent path. Set `CHROME_BRIDGE_CLIENT` to force the harness back onto an external launcher.

### Batched bridge actions

The bridge supports a composite `batch` action for workflows where several sub-commands should share one native-message request. The batch fails as a whole if any sub-command throws or returns `success: false`.

```bash
python3 test_client.py batch '[{"action":"startMonitoring"},{"action":"click","payload":{"selector":"#log"}},{"action":"consoleMessages","delayMs":100}]' <tabId>
```

Treat batching as a capability, not a benchmark claim. If you publish batching latency, cite a fresh raw result artifact from a harness path that actually invokes `batch`.

### Generating head-to-head results

Do not treat static README examples as maintained speed evidence. Run the measured adapters locally, then compare the generated result files:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --output /tmp/chrome-bridge-results.json
python3 benchmark_harness.py run --adapter playwright --iterations 5 --output /tmp/playwright-results.json
python3 benchmark_harness.py run --adapter puppeteer --iterations 5 --output /tmp/puppeteer-results.json
python3 benchmark_harness.py compare --input /tmp/chrome-bridge-results.json --input /tmp/playwright-results.json --input /tmp/puppeteer-results.json --output /tmp/head-to-head.md
```

Only rows marked `measured` in the generated report support speed or capability claims. Static metadata rows describe expected strengths and limits only. When publishing exact timings, keep the raw JSON and generated Markdown report, and record the source commit, host build identity, OS/hardware/browser/tool versions, command lines, iteration count, timeout/warmup policy, fixture URL, profile/cache state, and run timestamp.

## Local usage diagnostics

`usage_telemetry.py` is an advanced local diagnostic script. It mines local agent logs to count how often the bridge's browser tools are used and breaks the total down by source so you can see each one's magnitude as a share of the whole. It is not product telemetry: it only reads local files you point it at and never sends data anywhere.

- **claude** — Claude Code transcripts under `~/.claude/projects` (`--projects-dir`). MCP `tool_use` blocks matching `--server-match` (default `chrome[-_]devtools`).
- **codex** — Codex rollout sessions under `~/.codex/sessions` (`--codex-dir`). Canonical `mcp_tool_call_end` events (deduped by `call_id`; the bare `function_call` twin is ignored) whose `server`/`tool` match `--server-match`.
- **bridge** — the host's own `bridge_audit.jsonl` (`--bridge-audit`). Already bridge-specific, so `--server-match` is not applied; forwarded actions that log two rows under one `requestId` collapse to one call.

```bash
python3 usage_telemetry.py --format json --since 2025-01-01
```

Each report carries `total_calls`, a `by_source` map (`calls` + fractional `share`), and per-source/per-tool counts. Restrict sources with `--sources` (e.g. `--sources claude,codex`) and drop blocked bridge requests with `--exclude-denied`.

```bash
python3 usage_telemetry.py --sources codex,bridge --format text
```

It only reads transcript/audit files and never contacts the bridge or Chrome.

## Troubleshooting

The host writes a local `bridge_debug.log` (git-ignored) next to `bridge.py`:

```bash
tail -f bridge_debug.log
```

- `Connection refused` after retry in direct mode: Chrome is closed, no bridge extension is enabled, or the service worker did not wake. The CLI tries one external wake by opening `chrome-extension://<extension-id>/wake.html`; set `BRIDGE_WAKE_DISABLED=1` to skip it, `BRIDGE_EXTENSION_ID` or `BRIDGE_EXTENSION_ID_FILE` to override ID discovery, and `BRIDGE_WAKE_COMMAND` to override the opener in tests or nonstandard Chrome installs.
- `Connection refused` in broker mode: launchd broker is not loaded. Run `launchctl print gui/$UID/gg.wolfie.chrome-native-bridge.broker`.
- `broker backend unavailable: native host did not start`: broker is up, but Chrome, the extension, or the native host did not wake within `BRIDGE_BROKER_BACKEND_TIMEOUT_SECONDS`. Reload the extension and check `broker_debug.log` plus `bridge_debug.log`.
- `FATAL: could not bind 127.0.0.1:9223`: two direct-mode bridge extensions are enabled, or direct mode is racing the broker.
- `unauthorized`: token mismatch, or the native-host manifest authorized the wrong extension ID. Re-run `./setup.sh`, reload the printed extension directory, and disable duplicate bridge extensions.

## Security notes

- TCP API is localhost-only and requires the shared token.
- Payload bodies such as cookies and DOM are not logged by the host.
- Host policy in `bridge_policy.json` (`BRIDGE_POLICY_FILE`) is the enforcement layer for every raw TCP/CLI/MCP client: the TCP API is localhost-only and token-gated, but token holders bypass MCP scoping, so deny/allow/confirmation rules are enforced in the native host before any action reaches the extension.
- MCP `readonly`/`allow_sensitive` controls are usability scoping, not the security boundary, because a client with the token can call the raw TCP API directly. Use `bridge_policy.json` for real restrictions; use `browser_policy_check` (or `test_client.py policyCheck`) to see what the host would decide.
- Built-in host defaults are fail-closed when no valid `bridge_policy.json` exists: only `ping`, `policyCheck`, `policyInfo`, and lease actions are allowed. `setup.sh` copies `bridge_policy.example.json` to make normal local automation an explicit opt-in. `policyInfo` is additionally answered host-side before the action gate (like `policyCheck`), so a client can always discover the active policy/audit file paths even under a deny-all policy; it returns only those paths, never policy contents.
- Actions listed in `requireConfirmation` return an opaque `confirmationToken`; clients must resend the same action and payload through `browser_confirm_action` or `test_client.py confirm` before the host forwards it. This same-channel confirmation is friction against accidental use by a trusted token holder, not protection from a compromised token holder.
- Site policy (`allowedOrigins`/`deniedOrigins`) applies to tab-scoped actions too, not just URL-carrying ones. For an action whose payload has no URL/domain (e.g. `click`, `type`, `executeScript`, `getHTML` on a `tabId`), the host resolves that tab's live origin through a reserved internal lookup and evaluates policy against it before forwarding. When policy constrains origins and the origin cannot be resolved, the action is denied (fail-closed). `policyCheck` cannot see the live origin without forwarding, so its result includes `originDependent: true` for such actions to flag that the real request is additionally origin-checked.
- Python host only: set `BRIDGE_POLICY_APPROVAL_MODE=gui` (default on macOS) to show a native prompt when an otherwise-allowed action is blocked only because the target origin is not in `allowedOrigins`. The prompt offers `Deny`, `Allow This Time`, and `Always Allow`. `Allow This Time` creates a TTL-bound one-shot in-memory origin grant for the exact client/action/payload/target; `Always Allow` adds that origin to the local `bridge_policy.json`. Origin approval only authorizes the site, then the host re-runs normal policy evaluation, so actions in `requireConfirmation` still require a confirmation token. Set `BRIDGE_POLICY_APPROVAL_MODE=off` to disable prompts, or `BRIDGE_POLICY_APPROVAL_MODE=command` with `BRIDGE_POLICY_APPROVAL_COMMAND` for tests/custom frontends. Rust host users currently get the conservative deny/doctor flow without this GUI approval UX.
- GitHub attachment helpers (`githubAttachUploadedFiles`, `githubSubmitComment`) are narrow tab-scoped actions. They remain subject to host origin policy and also reject any tab whose URL origin is not `https://github.com`, so they do not require broad `executeScript*` allowlisting.
- Audit logs are JSONL at `BRIDGE_AUDIT_LOG_FILE` / `bridge_audit.jsonl`, one event per request with `ts`, `client`, `action`, `targets`, `decision`, `reason`, `requestId`. They intentionally omit payload and response bodies.
- Cookie and storage-state redaction is enabled by default through policy (`redact`): cookie values and sensitive storage keys are replaced with `<redacted>` before responses reach the client. Page-derived content from `getHTML`, `extractText`, `executeScript`, and `executeScriptCDP` is additionally masked against the client policy's `redactPatterns` (a list of regexes; use inline flags like `(?i)` for case-insensitivity) before it reaches the client.
- The Python and Rust native hosts enforce the same baseline policy, audit, and redaction rules; this parity is covered by the guardrails contract (`verify_guardrails_contract.py`). Python's optional origin-approval prompt is an extra UX layer that can persist a local policy grant before the baseline policy is re-evaluated.
- `executeScript` uses `chrome.scripting` in the page MAIN world and can be blocked by strict page CSP.
- `executeScriptCDP`, browser interactions, waits, screenshots, viewport control, emulation, monitoring, interception, geolocation, and performance metrics use `chrome.debugger`.
- `downloads`, `contentSettings`, `host_permissions: <all_urls>`, cookie access, debugger access, and script execution are powerful. Use this profile for trusted automation only.
- `sessionStatus` reports cookie names and counts only, never cookie values; `waitForHandoff` only focuses a tab, shows a banner, and waits — neither reads, imports, nor overwrites cookies. Operating on the real profile is the bridge's design, not a leak, but `sessionStatus` output (which sites/accounts are logged in) is itself sensitive — keep it out of transcripts.
- The bridge still intentionally lacks Playwright-style isolated browser contexts/profiles and multi-browser support; it controls the real Chrome profile. That ambient real-profile session is the point: it is what lets an agent reuse your existing logins and hand off to you for steps it should not do itself.
