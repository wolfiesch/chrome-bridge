# Verification and release packaging

## Verification

Offline checks (no browser needed), run from the repo root:

```bash
PYTHONDONTWRITEBYTECODE=1 ./verify_cli_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_heartbeat_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_broker_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_task_session_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_bridge.py
PYTHONDONTWRITEBYTECODE=1 ./verify_benchmark_harness.py
PYTHONDONTWRITEBYTECODE=1 ./verify_moat_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_guardrails_contract.py
PYTHONDONTWRITEBYTECODE=1 ./verify_install_contract.py
python3 benchmark_harness.py run --adapter noop --iterations 2 --output /tmp/results.json
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile bridge.py broker.py bridge_wake.py test_client.py benchmark_harness.py extension_identity.py scripts/background_reliability.py verify_bridge.py verify_cli_contract.py verify_broker_contract.py verify_heartbeat_contract.py verify_task_session_contract.py verify_benchmark_harness.py verify_install_contract.py verify_agent_actions_live.py verify_capability_matrix.py
node --check background.js
node --check wake.js
diff -q manifest.json extension/manifest.json
diff -q background.js extension/background.js
diff -q wake.html extension/wake.html
diff -q wake.js extension/wake.js
```

Manual live gates after reloading the unpacked extension (opens real Chrome tabs):

```bash
python3 test_client.py ping
python3 scripts/background_reliability.py --duration-seconds 60 --output /tmp/background-reliability.json
PYTHONDONTWRITEBYTECODE=1 ./verify_live_install_smoke.py
PYTHONDONTWRITEBYTECODE=1 ./verify_agent_actions_live.py
PYTHONDONTWRITEBYTECODE=1 ./verify_capability_matrix.py
```

`verify_capability_matrix.py` skips `downloadUrl` by default in live profiles because Chrome's "Ask where to save each file before downloading" setting can open a modal save dialog and block unattended smoke runs. To exercise that capability intentionally, run:

```bash
CHROME_BRIDGE_TEST_DOWNLOAD=1 PYTHONDONTWRITEBYTECODE=1 ./verify_capability_matrix.py
```

`verify_live_install_smoke.py` uses a temporary HOME/XDG_CONFIG_HOME and exits 0 with `SKIP live install smoke: Chrome/Chromium executable not found` only when no Chrome/Chromium executable is available.

The default sample policy is intentionally fail-closed and denies loopback URLs. For these localhost live gates, temporarily use an explicit smoke-test policy, then restore your normal policy:

```json
{
  "default": {
    "allowedActions": ["*"],
    "allowedOrigins": ["http://127.0.0.1:*"],
    "deniedActions": [],
    "deniedOrigins": [],
    "requireConfirmation": [],
    "redact": true,
    "audit": true
  }
}
```

`verify_capability_matrix.py` binds its HTTP fixture to port `0`, derives the URL at runtime, writes screenshots/HTML/storage to temp files, and prints compact redacted JSON.

## Release packaging

Pull requests run `.github/workflows/ci.yml`. Tags that match `v*` run `.github/workflows/release.yml`.

The extension artifact is an unpacked, developer-mode bundle and remains unkeyed. A packaged or Web Store extension uses its own store-managed ID and must be registered separately:

```bash
./setup.sh --extension-id <store-id>
```

Build local release artifacts with:

```bash
python3 scripts/package_release.py --version <version> --dist dist
```
