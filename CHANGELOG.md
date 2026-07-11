# Changelog

## Unreleased

- Added durable task sessions with owned tabs, named Chrome tab groups, safe session cleanup, CLI commands, and typed MCP tools.
- Added a machine-readable background reliability harness that detects active-tab changes, frontmost-app changes, unexpected tabs, and owned tabs becoming active.
- Routine CLI and broker retries no longer open a visible extension wake tab when the native backend is unavailable.
- Navigation now opens inactive tabs by default; pass `--foreground` for an intentional user-visible tab.
- Screenshots now use the background-safe debugger path by default; pass `--visible` for an intentional visible-tab capture.
- Added `scripts/diagnose_install.py` to report deployed-file drift and broker/backend connection state without waking Chrome.

All notable user-facing changes for Chrome Native Messaging Automation Bridge are recorded here.

## 1.0.1 - Public release candidate

### Security and trust model

- Added recursive host-side redaction for sensitive results returned through `batch`, matching standalone action redaction for cookies, storage state, HTML/text extraction, and script results across the Python and Rust hosts.
- Kept `executeScriptCDP` out of the sample policy's default allowed actions; users must opt into high-risk debugger/script capabilities deliberately.
- Clarified that same-channel confirmation is accidental-use friction for trusted token holders, not protection from a compromised bridge token.
- Documented the trusted-local security model earlier in the README.

### Release packaging

- Source release archives are built from tracked files only, so ignored or untracked local artifacts do not leak into public zips.
- The unpacked extension artifact now contains the complete developer-mode extension surface: `background.js`, `manifest.json`, `wake.html`, and `wake.js`.
- Local policy backups, tokens, generated manifests, virtualenvs, lockfiles, debug logs, audit logs, and WIP patches are excluded from release artifacts.

### Installation and workflows

- CI and release workflows now run the same core gate set, including broker, GitHub attachment, install, live-smoke, Rust parity, guardrail, lease, and benchmark contract checks.
- `setup.sh` and `setup-rs.sh` no longer imply that an unpacked extension was deployed when `--extension-id` registers a packaged or store-managed extension ID.
- `setup-broker.sh` now prints the state-dir extension path and token-file advice after a successful broker setup.
- Live install smoke now passes the selected host port through setup and verifies the reported setup JSON shape.

### Documentation

- Removed stale static benchmark timing tables and replaced them with instructions for generating fresh local reports.
- Narrowed platform-support language to documented macOS/Linux installer paths.
- Clarified broker-mode state directory identity, MCP versioning, local usage diagnostics, and release artifact boundaries.

### Excluded from this release

- XChat response-capture work is intentionally not included in this public release candidate. The local WIP was preserved outside Git under `.wip/` for a future hardening pass.
