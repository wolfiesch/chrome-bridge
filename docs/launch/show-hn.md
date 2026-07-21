DRAFT - do not post without explicit approval

# Show HN title

Show HN: Chrome Bridge - agents use my real Chrome without getting the keys

## Alternate titles

1. Show HN: I built a governed bridge from local agents to real Chrome
2. Show HN: Chrome Bridge lets agents use logged-in Chrome with policy gates
3. Show HN: A trusted-local Chrome bridge for agent handoffs and audits

## First comment draft

I built Chrome Bridge because the browser automation choices I had kept missing the same thing: agents are useful when they can work inside the browser session I already use, but my real Chrome profile is also one of the most sensitive things on my laptop.

So the project is not trying to be generic browser automation. The goal is a trusted local real-profile handoff: a local agent can drive my already-logged-in Chrome through a native-messaging bridge, but the host enforces policy before actions reach the extension.

The governance layer is the main story. The TCP API is loopback-only and token-gated. The host policy is fail-closed by default when no valid local policy exists, with explicit allow/deny rules for actions and origins. For tab-scoped actions such as click, type, HTML extraction, or script execution, the host resolves the live tab origin and applies the same site policy before forwarding. Sensitive actions can require same-channel confirmation tokens. Responses can be redacted by policy, including cookie values, sensitive storage keys, and page-derived text matched by configured redaction patterns. Audit logs are JSONL and record request metadata such as timestamp, client, action, targets, decision, reason, and request ID, without payload or response bodies.

The feature I use most is `waitForHandoff`: the agent pauses, focuses the real tab, marks its tab group as needing review, and shows a compact bottom card while I do the thing it should not do - login, 2FA, captcha, payment confirmation. It resumes when the expected selector, URL, text, or manual page change is reached. `sessionStatus` is the companion redacted login probe: it reports cookie counts, cookie names, and a logged-in boolean for domains, never cookie values, so the agent can decide whether a handoff is needed without dumping secrets.

There is also cooperative multi-agent leasing, so multiple local clients can share one real profile without stepping on each other. While one named token holds the lease, non-lease actions from other clients are rejected by the host until release or expiry.

This is trusted-local software with real risk. It can control the browser profile you actually use. That is exactly why the policy layer, audit log, redaction, confirmation flow, and local-only token model exist.

The repo includes Python and Rust native-host paths with baseline policy/audit/redaction parity, plus CLI and MCP surfaces for local clients on macOS and Linux installer paths. I would especially like feedback on the trust model, policy ergonomics, MCP tool shape, and whether the human handoff flow matches how people actually want agents to handle login and other sensitive browser steps.
