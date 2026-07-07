# Command reference

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

These two commands exploit what sets this bridge apart from Playwright/Puppeteer: it drives your **real, already-logged-in Chrome profile**, so existing sessions (cookies, SSO, passkeys) are ambient. Neither command ever reads, imports, or overwrites cookie values - they only observe and hand control to you.

```bash
chrome-bridge sessionStatus <domain> [<domain> ...]
chrome-bridge waitForHandoff <message> [mode] [selectorOrUrlOrText] [timeoutMs] [tabId]
```

`sessionStatus` is a **redacted auth probe**: for each domain it reports cookie count, cookie *names* (never values), whether a session/auth cookie is present, and a `loggedIn` boolean - enough to decide "is this profile already signed in to X?" without exposing secrets. Treat its output as sensitive: cookie names plus logged-in status can reveal which accounts and sites the profile uses.

`waitForHandoff` **pauses automation and hands control to you**: it focuses the target tab, shows an in-page banner with your `message`, and blocks until the page reaches an expected state, then resumes the agent. Use it for interactive steps an agent should not perform - login, 2FA, captcha, payment confirmation. `mode` is `manual` (default; resolves when you change the page), `selector`, `url`, or `text`; the positional argument after `mode` is the selector/URL-substring/text to wait for. `timeoutMs` defaults to 120000. The CLI raises its socket read timeout to cover the wait, so long handoffs do not time out in transport. Under MCP auto-lease, the cooperative lease is extended to span the whole handoff window so another agent cannot mutate the profile while you are acting.

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
