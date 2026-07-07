# Browser-use adapter

This adapter shows how to use Chrome Bridge as the local real-profile execution layer for browser-use-style agent frameworks.

Chrome Bridge is not a generic fresh-profile browser driver. It talks to the local native-messaging bridge so an agent can operate the user's existing logged-in Chrome profile under the same bridge controls used by the CLI and MCP server: fail-closed policy checks, audit logging, redaction, confirmation tokens where configured, human handoff, redacted session probes, and multi-agent leasing at the host layer.

## Files

- `chrome_bridge_session.py` - stdlib-only TCP client with a persistent newline-delimited JSON socket to the bridge host.
- `example_agent.py` - commented standalone agent loop that uses `ChromeBridgeSession`, including a `waitForHandoff` login/2FA step. It imports `browser_use` only to demonstrate where framework wiring would live; it does not depend on undocumented browser-use internals.

## Install and run

1. Install and start Chrome Bridge using the repository setup instructions.
2. Make sure Chrome is running with the extension loaded.
3. Ensure the bridge token file is available. By default the adapter reads:

   ```bash
   bridge_token.txt
   ```

   To use another token path:

   ```bash
   export BRIDGE_TOKEN_FILE=/path/to/bridge_token.txt
   ```

4. If the bridge listens on a non-default port, set:

   ```bash
   export BRIDGE_PORT=9223
   ```

5. Grant only the actions and origins your agent needs in `bridge_policy.json`. Typical actions for this adapter are:

   ```text
   navigate
   getTabs
   observe
   getCurrentState
   click
   type
   fill
   extractText
   screenshot
   waitForSelector
   waitForHandoff
   sessionStatus
   ```

6. Use the session directly from your agent code:

   ```python
   from chrome_bridge_session import ChromeBridgeSession

   with ChromeBridgeSession() as chrome:
       chrome.navigate("https://example.com")
       chrome.wait_for_selector("main")
       print(chrome.extract_text(max_chars=4000))
   ```

7. Run the standalone example only when you have changed its placeholder URL/selectors to a site allowed by your local policy:

   ```bash
   python3 adapters/browser_use/example_agent.py
   ```

## Browser-use integration status

`example_agent.py` deliberately keeps browser-use wiring behind a TODO. Browser-use has changed its custom browser/action integration surfaces across releases, and this repo should not publish fabricated private APIs. The stable part is the action-execution layer: `ChromeBridgeSession` exposes methods an agent loop can call for navigation, state observation, clicks, typing, extraction, screenshots, waits, handoff, and redacted session checks.

When integrating with a specific browser-use version, map its documented custom action/browser hooks to these methods rather than bypassing Chrome Bridge with Playwright/Puppeteer.

## Security note

This adapter is a raw bridge-token holder. It connects directly to the local TCP bridge and sends the token on every request. MCP tool scoping does not apply to this adapter.

The enforcement boundary is the Chrome Bridge host policy, especially `bridge_policy.json`, plus the host audit/redaction controls. Keep the token file private, grant the minimum required actions and origins, and treat `sessionStatus` output as sensitive because redacted cookie names and login state can still reveal account usage.
