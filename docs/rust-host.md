# Rust host

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
