# Benchmarks

## Benchmarking against other browser automation surfaces

The benchmark harness measures speed for selected adapters. `chrome-bridge`, `playwright`, `puppeteer`, and `chrome-devtools-mcp` are live-measurable; Claude in Chrome and Codex Chrome Extension remain manual/static capability metadata. The report also emits a normalized scorecard, claim-discipline note, and gap tickets.

Run the offline contract adapter:

```bash
python3 benchmark_harness.py run --adapter noop --iterations 2 --output /tmp/results.json
```

Run measured adapters:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --output /tmp/chrome-bridge-results.json
python3 benchmark_harness.py run --adapter playwright --iterations 5 --output /tmp/playwright-results.json
python3 benchmark_harness.py run --adapter puppeteer --iterations 5 --output /tmp/puppeteer-results.json
python3 benchmark_harness.py run --adapter chrome-devtools-mcp --iterations 5 --output /tmp/chrome-devtools-results.json
```

`chrome-bridge`, `playwright`, `puppeteer`, and `chrome-devtools-mcp` start a local HTTP fixture by default. To benchmark another page, pass `--base-url`:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --base-url http://127.0.0.1:PORT/ --output /tmp/results.json
```

Missing optional dependencies or browser binaries are reported as unsupported/fail in the adapter output without breaking the noop/offline checks. Shadow DOM, iframe, and semantic locator user-action parity are measured as explicit capability rows.

Generate the Markdown report, with optional CI-friendly JUnit XML and GitHub Step Summary outputs:

```bash
python3 benchmark_harness.py compare --input /tmp/results.json --output /tmp/report.md --junit-output /tmp/benchmark.xml --github-step-summary /tmp/summary.md
python3 benchmark_harness.py compare --input /tmp/chrome-bridge-results.json --input /tmp/chrome-devtools-results.json --output /tmp/head-to-head.md
```

### Persistent in-process client

The benchmark harness talks to the bridge over one keep-alive TCP connection (see `BridgeClient` in `benchmark_harness.py`) instead of spawning `python3 test_client.py` per operation. The native host (`bridge.py`) serves many newline-delimited requests per connection, awaiting each extension response on a per-request queue before reading the next, so request/response order is preserved on a shared socket.

This avoids per-operation Python interpreter startup and TCP connection setup in the harness path. Exact latency depends on the local browser profile, machine load, adapter versions, and benchmark run. Generate a fresh report before making comparative speed claims. The CLI (`test_client.py`) still uses one connection per command; the persistent client is the harness/agent path. Set `CHROME_BRIDGE_CLIENT` to force the harness back onto an external launcher.

### Batched bridge actions

The bridge supports a composite `batch` action for workflows where several sub-commands should share one native-message request. The batch fails as a whole if any sub-command throws or returns `success: false`.

```bash
python3 test_client.py batch '[{"action":"startMonitoring"},{"action":"click","payload":{"selector":"#log"}},{"action":"consoleMessages","delayMs":100}]' <tabId>
```

Treat batching as a capability, not a benchmark claim. If you publish batching latency, cite a fresh raw result artifact from a harness path that actually invokes `batch`.

### Generating head-to-head results

Do not treat static README examples as maintained speed evidence. Run the measured adapters locally, then compare the generated result files:

```bash
python3 benchmark_harness.py run --adapter chrome-bridge --iterations 5 --output /tmp/chrome-bridge-results.json
python3 benchmark_harness.py run --adapter playwright --iterations 5 --output /tmp/playwright-results.json
python3 benchmark_harness.py run --adapter puppeteer --iterations 5 --output /tmp/puppeteer-results.json
python3 benchmark_harness.py compare --input /tmp/chrome-bridge-results.json --input /tmp/playwright-results.json --input /tmp/puppeteer-results.json --output /tmp/head-to-head.md
```

Only rows marked `measured` in the generated report support speed or capability claims. Static metadata rows describe expected strengths and limits only. When publishing exact timings, keep the raw JSON and generated Markdown report, and record the source commit, host build identity, OS/hardware/browser/tool versions, command lines, iteration count, timeout/warmup policy, fixture URL, profile/cache state, and run timestamp.
