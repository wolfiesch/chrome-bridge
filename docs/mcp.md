# MCP server

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
- `browser_policy_check` - ask the host what its policy would decide for an action/payload without forwarding it
- `browser_wait_for` (`mode`: `load|selector|text|url`)

Sensitive:

- `browser_get_cookies`
- `browser_session_status` - redacted auth/session probe (cookie names/counts + `loggedIn` per domain, never values)

Mutating:

- `browser_navigate`
- `browser_click`, `browser_type`, `browser_fill`, `browser_hover`
- `browser_scroll`, `browser_press`, `browser_drag`
- `browser_select`
- `browser_upload_file` (validates local paths before contacting Chrome)
- `browser_tab_control` (`op`: `activate|close|reload|back|forward`), `browser_lease`, `browser_release`
- `browser_set_cpu_throttling`, `browser_set_network_conditions`, `browser_clear_network_conditions`, `browser_set_color_scheme`, `browser_set_user_agent`
- `browser_wait_for_handoff` - pause automation, focus the real tab with an on-page banner, and wait for a human to finish login/2FA/captcha before resuming
- `browser_confirm_action` - resend an action with a host-issued confirmation token

Escape hatch (sensitive):

- `browser_action` - escape hatch for any raw bridge action (interception, geolocation, monitoring, console/network logs, `downloadUrl`, `storageState`, `executeScript`, `setViewport`, `handleDialog`, `batch`, ...)

### Resources

- `browser://tabs` - live tab list.
- `browser://tab/{id}/state` - current state of a tab.

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
      "args": ["--from", "/ABSOLUTE/PATH/TO/chrome-bridge/mcp", "chrome-bridge-mcp"],
      "env": {
        "BRIDGE_REPO_ROOT": "/ABSOLUTE/PATH/TO/chrome-bridge",
        "BRIDGE_PORT": "9223"
      }
    }
  }
}
```

The server honors `BRIDGE_PORT`, `BRIDGE_TOKEN_FILE`, `BRIDGE_CONNECT_TIMEOUT_SECONDS`, `BRIDGE_MCP_READONLY`, and `BRIDGE_MCP_ALLOW_SENSITIVE`, and reads the same `bridge_token.txt`. Chrome with the loaded extension must be running and the native host registered (`./setup.sh` or `./setup-rs.sh`).

### HTTP transport

By default the server speaks stdio. Set `BRIDGE_MCP_TRANSPORT=http` to serve over streamable HTTP instead, bound to `BRIDGE_MCP_HTTP_HOST` (default `127.0.0.1`) and `BRIDGE_MCP_HTTP_PORT` (default `8723`). Note: the server forwards a single ambient bridge token, so all HTTP clients share one bridge identity. Cooperative leasing (below) arbitrates only between distinct token identities (e.g. separate stdio servers each pointed at their own named token); per-request token propagation over one HTTP endpoint is not yet implemented.
