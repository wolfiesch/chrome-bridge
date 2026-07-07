# Multi-agent tokens and leasing

## Multi-client tokens and leasing

The bridge accepts multiple named client tokens and offers a cooperative, host-side lease so several agents can share one real Chrome profile without colliding. Both the Python and Rust hosts implement this identically; it is enforced entirely in the host (lease actions are never forwarded to the extension).

### Named tokens

`bridge_token.txt` (the legacy single token) is always accepted under the client name `default`. Additionally, if `bridge_tokens.txt` (override with `BRIDGE_TOKENS_FILE`) exists, each non-empty, non-`#` line is parsed as `name:token` (split on the first colon) and registered as an extra named client. See `bridge_tokens.txt.example`. A request is authorized if its token matches any known token; the matched token determines the requesting client's name. `bridge_tokens.txt` is a secret registry and is git-ignored.

### Lease protocol

Three host-answered actions (also exposed as MCP tools `browser_lease`, `browser_release`, `browser_lease_status`):

- `lease` - payload optional `{"ttlMs": int}` (default 300000). Acquires the lease when free, expired, or already yours; otherwise returns `leased by <owner>`.
- `release` - releases your lease (`released: true`); `released: false` when no live lease; `not lease owner` when another client holds it.
- `leaseStatus` - non-mutating snapshot `{owner, expiresAt, now}` (epoch ms; `owner` null when unheld).

While a live lease is held, every non-lease action from a different client (including `batch`) is rejected with `leased by <owner>` before forwarding, so the lease cannot be bypassed. Leases auto-expire after their TTL. `BRIDGE_SOCKET_IDLE_TIMEOUT` (default 300s) bounds how long a persistent connection may idle.

`verify_lease_contract.py` covers the basic named-token and lease semantics. `verify_lease_stress_contract.py` adds race/load coverage for simultaneous lease acquisition, non-owner denial without extension forwarding, owner concurrency, TTL expiry, release races, and TCP disconnect behavior.
