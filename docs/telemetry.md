# Local usage diagnostics

## Local usage diagnostics

`usage_telemetry.py` is an advanced local diagnostic script. It mines local agent logs to count how often the bridge's browser tools are used and breaks the total down by source so you can see each one's magnitude as a share of the whole. It is not product telemetry: it only reads local files you point it at and never sends data anywhere.

- **claude** - Claude Code transcripts under `~/.claude/projects` (`--projects-dir`). MCP `tool_use` blocks matching `--server-match` (default `chrome[-_]devtools`).
- **codex** - Codex rollout sessions under `~/.codex/sessions` (`--codex-dir`). Canonical `mcp_tool_call_end` events (deduped by `call_id`; the bare `function_call` twin is ignored) whose `server`/`tool` match `--server-match`.
- **bridge** - the host's own `bridge_audit.jsonl` (`--bridge-audit`). Already bridge-specific, so `--server-match` is not applied; forwarded actions that log two rows under one `requestId` collapse to one call.

```bash
python3 usage_telemetry.py --format json --since 2025-01-01
```

Each report carries `total_calls`, a `by_source` map (`calls` + fractional `share`), and per-source/per-tool counts. Restrict sources with `--sources` (e.g. `--sources claude,codex`) and drop blocked bridge requests with `--exclude-denied`.

```bash
python3 usage_telemetry.py --sources codex,bridge --format text
```

It only reads transcript/audit files and never contacts the bridge or Chrome.
