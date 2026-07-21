DRAFT - do not post without explicit approval

# Directory listing copy

Reusable draft copy for MCP directories such as mcpservers.org, PulseMCP, Glama, and Smithery.

## One-line description

Chrome Bridge lets MCP agents drive your real logged-in Chrome through a local policy-governed native bridge.

## Short description

Chrome Bridge exposes your real local Chrome profile to MCP clients through a token-gated native-messaging bridge with host-enforced policy, origin checks, audit JSONL, redaction, confirmation tokens, human handoff, session probes, and cooperative leasing.

## Long description

Chrome Bridge is a trusted-local browser control bridge for agents that need to work in the Chrome profile you already use. Instead of launching a fresh Playwright or Puppeteer profile, or opening Chrome with a remote-debugging port, it routes local clients through a Chrome MV3 extension and native-messaging host.

The MCP server is a client of the same token-gated `127.0.0.1:9223` bridge API used by the CLI. The host policy is the enforcement boundary: built-in defaults are fail-closed, actions and origins can be explicitly allowed or denied, tab-scoped actions are checked against the live tab origin, sensitive actions can require confirmation tokens, and policy decisions can be inspected before forwarding. Audit logs are JSONL and intentionally omit payload and response bodies. Redaction can mask cookie values, sensitive storage keys, and page-derived content before results reach the client.

Chrome Bridge also includes real-profile workflows that are awkward in empty-profile automation. `browser_session_status` gives a redacted auth probe with cookie names/counts and logged-in status, never values. `browser_wait_for_handoff` pauses the agent, focuses the tab, marks the task group as needing review, shows a compact bottom card, lets a human complete login/2FA/captcha/payment, and resumes when the expected page state appears. Cooperative leasing helps multiple local agents share one real profile without colliding.

Python and Rust native hosts share the same baseline policy, audit, and redaction behavior. Documented installers target macOS and Linux.

## Category and tag suggestions

- MCP server
- Browser automation
- Chrome
- Native messaging
- Local-first
- Agent tools
- Security
- Human-in-the-loop
- Developer tools

## Install snippet

```json
{
  "mcpServers": {
    "chrome-bridge": {
      "command": "uvx",
      "args": ["--from", "/ABSOLUTE/PATH/TO/chrome-bridge/mcp", "chrome-bridge-mcp"],
      "env": {
        "BRIDGE_REPO_ROOT": "/ABSOLUTE/PATH/TO/chrome-bridge",
        "BRIDGE_PORT": "9223"
      }
    }
  }
}
```

Chrome with the loaded extension must be running and the native host must be registered with `./setup.sh` or `./setup-rs.sh`. The MCP server reads the same local bridge token and honors `BRIDGE_PORT`, `BRIDGE_TOKEN_FILE`, `BRIDGE_CONNECT_TIMEOUT_SECONDS`, `BRIDGE_MCP_READONLY`, and `BRIDGE_MCP_ALLOW_SENSITIVE`.

## Links

- Repository: https://github.com/wolfiesch/chrome-bridge (verified from `git remote -v`)
- Release: https://github.com/wolfiesch/chrome-bridge/releases/tag/v1.0.1 (verified from local `v1.0.1` tag)
