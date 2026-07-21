DRAFT - do not post without explicit approval

# Reddit post variants

## r/ClaudeAI

### Title

I built Chrome Bridge so Claude can use my real Chrome session with policy gates and human handoff

### Draft

I built Chrome Bridge for the browser tasks where a fresh automation profile is the wrong abstraction.

Claude Desktop/Code can already use MCP tools, but many real web workflows depend on the Chrome profile I actually use: existing cookies, SSO, passkeys, and sites where login or 2FA should stay human-controlled. Chrome Bridge exposes that real local Chrome profile through a Chrome MV3 extension plus a native-messaging host, with an MCP server layered on top of the same local bridge API as the CLI.

The important part is not just "Claude can click my browser." The host is the enforcement boundary. The API is localhost-only and token-gated. The default host posture is fail-closed unless local policy opts into actions/origins. Tab-scoped actions are checked against the live tab origin before forwarding. Policy can require confirmation tokens for sensitive actions. Audit logs are JSONL and omit payload/response bodies. Redaction can mask cookie values, sensitive storage keys, and page-derived content before the client sees it.

The handoff flow is what made it click for me. Claude can navigate until it reaches a login, captcha, 2FA, or payment step, then call `browser_wait_for_handoff`. Chrome Bridge focuses the real tab, marks its group as needing review, shows a compact bottom card, and pauses the tool call while I complete the sensitive step myself. When the expected selector, text, URL, or manual page change appears, the agent resumes.

There is also `browser_session_status`, which is a redacted auth probe: cookie names/counts plus a logged-in boolean for domains, never cookie values. That lets the agent decide whether it is already signed in without dumping credentials into the transcript.

It also has cooperative leasing for multiple local MCP clients, so one agent can hold the real browser profile while others are denied mutating actions until the lease is released or expires.

This is trusted-local software and it has real risk because it controls the browser profile you use. That is why the policy layer exists. I would like feedback from Claude users on the MCP tool shape, the handoff UX, and what policy defaults would make this feel safer in daily use.

## r/LocalLLaMA

### Title

Chrome Bridge: local-only real-profile browser control for any MCP client

### Draft

I built Chrome Bridge around a sovereignty problem I kept running into with browser agents: I want local agents to use the browser session I already have, but I do not want to move that session into a cloud browser, a vendor-owned agent browser, or a fresh automation profile that cannot access my existing logins.

Chrome Bridge is a local Chrome MV3 extension plus native-messaging host. Local clients talk newline-delimited JSON to a token-gated loopback API, and the MCP server is just one client of that same bridge. The browser stays on the machine. The Chrome profile is the real profile. There is no remote browser service in the control loop.

The trust model is explicit because this is powerful software. The host is the policy boundary: built-in defaults are fail-closed, local policy controls allowed actions and origins, tab-scoped actions are checked against the live tab origin, and sensitive actions can require confirmation tokens. The host writes JSONL audit events with request metadata while intentionally omitting payload and response bodies. Redaction can mask cookie values, sensitive storage keys, and configured page-content patterns before results reach the client.

The MCP surface works with clients that can launch a stdio MCP server. Tools cover tabs, snapshots, text extraction, screenshots, navigation, clicks, typing, forms, uploads, waits, session status, handoff, leasing, and a raw bridge escape hatch when policy allows it. You can scope the exposed MCP tools read-only or require an env flag before sensitive tools are registered; the host policy remains the real enforcement layer.

The real-profile angle adds two useful workflows. `browser_session_status` tells an agent whether domains appear logged in using cookie names/counts, never values. `browser_wait_for_handoff` lets an agent pause while a human completes login, 2FA, captcha, or payment in the focused tab, then resume after the expected page state appears.

Documented installers target macOS and Linux, and the native host has Python and Rust paths with the same baseline policy, audit, and redaction behavior. I am looking for feedback on local-agent trust boundaries, MCP defaults, and whether the lease/handoff model fits multi-agent local workflows.
