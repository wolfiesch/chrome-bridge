#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import importlib.util
from types import SimpleNamespace
from xml.etree import ElementTree

SCRIPT_DIR = Path(__file__).resolve().parent
HARNESS = SCRIPT_DIR / "benchmark_harness.py"
spec = importlib.util.spec_from_file_location("benchmark_harness", HARNESS)
benchmark_harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark_harness)



def run(*args, extra_env=None):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        cwd=SCRIPT_DIR,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="chrome-bridge-bench-") as tmp:
        out = Path(tmp) / "results.json"
        report = Path(tmp) / "report.md"
        require(benchmark_harness.monitored_item_count({"json": {"result": {"messages": []}}}) == 0,
                "empty console wrapper must not count as monitored data")
        require(benchmark_harness.monitored_item_count({"json": {"result": {"requests": [{"url": "/"}]}}}) == 1,
                "network wrapper must count nested request entries")

        ps_sample = "\n".join([
            "  PID   RSS COMMAND",
            "  101  2048 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "  102  4096 /tmp/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "  103  8192 /Applications/Notes.app/Contents/MacOS/Notes",
        ])
        require(benchmark_harness.browser_rss_mb_from_ps(ps_sample) == 4,
                "browser RSS parser must include benchmark-controlled browser families only")
        try:
            benchmark_harness.enforce_browser_rss_limit(3, ps_output=ps_sample)
        except RuntimeError as exc:
            require("browser RSS 4 MB exceeds limit 3 MB" in str(exc),
                    "browser RSS guard must report measured benchmark browser usage and limit")
            require(isinstance(exc, benchmark_harness.BrowserRssLimitExceeded),
                    "browser RSS guard must raise a fatal exception type")
        else:
            raise AssertionError("browser RSS guard must fail before live adapters can keep growing")
        junit_edge = Path(tmp) / "junit-edge.xml"
        benchmark_harness.write_junit_report(junit_edge, {
            "adapter&one": {
                "operations": [
                    {"name": "bad<op", "capability": "fail", "medianMs": 12, "errors": ["x < y & z"]},
                    {"name": "missing", "capability": "unsupported", "medianMs": 0, "errors": ["missing < dep & optional"]},
                ]
            }
        })
        root = ElementTree.parse(junit_edge).getroot()
        require(root.get("tests") == "2" and root.get("failures") == "1" and root.get("skipped") == "1",
                "JUnit report must count fail and unsupported statuses")
        xml_text = junit_edge.read_text(encoding="utf-8")
        require("bad&lt;op" in xml_text and "x &lt; y &amp; z" in xml_text,
                "JUnit report must XML-escape operation names and errors")
        require(root.find(".//failure") is not None and root.find(".//skipped") is not None,
                "JUnit report must map failures and unsupported rows to CI nodes")


        proc = run("run", "--adapter", "noop", "--iterations", "2", "--output", str(out))
        require(proc.returncode == 0, f"benchmark run failed: stdout={proc.stdout!r} stderr={proc.stderr!r}")
        require(out.is_file(), "benchmark JSON output was not written")

        data = json.loads(out.read_text(encoding="utf-8"))
        require(data.get("schemaVersion") == 1, "schemaVersion must be 1")
        require(data.get("adapter") == "noop", "adapter name not recorded")
        require(data.get("iterations") == 2, "iteration count not recorded")
        require(len(data.get("operations", [])) >= 10, "expected broad browser operation coverage")
        require("navigate" in {op.get("name") for op in data.get("operations", [])}, "navigate operation missing")
        require("fill" in {op.get("name") for op in data.get("operations", [])}, "fill operation missing")
        op_names = {op.get("name") for op in data.get("operations", [])}
        require("network-monitoring" in op_names, "network monitoring operation missing")
        require("shadow-dom-click" in op_names, "shadow DOM operation missing")
        require("iframe-fill" in op_names, "iframe fill operation missing")
        for op in data.get("operations", []):
            require(op.get("capability") in {"pass", "fail", "manual", "unsupported"}, f"bad capability status for {op}")
            require(isinstance(op.get("durationsMs"), list), f"durationsMs missing for {op.get('name')}")
            require("medianMs" in op, f"medianMs missing for {op.get('name')}")

        matrix = data.get("comparison", {}).get("tools", {})
        for name in ["chrome-native-bridge", "playwright", "claude-in-chrome", "codex-chrome-extension", "puppeteer", "chrome-devtools-mcp"]:
            require(name in matrix, f"comparison missing {name}")
        gaps = data.get("comparison", {}).get("gaps", [])
        require(any("isolated contexts" in gap.get("gap", "") for gap in gaps), "expected isolated-context gap")
        gap_names = {gap.get("gap", "") for gap in gaps}
        require("interactive destructive approval" in gap_names, "expected interactive approval residual gap")
        # Implemented this sprint, so they must no longer be listed as gaps.
        require("html text and script-result redaction" not in gap_names,
                "html/script redaction was implemented; must not remain a gap")
        require("tab-origin-aware policy enforcement" not in gap_names,
                "tab-origin policy was implemented; must not remain a gap")

        proc = run("compare", "--input", str(out), "--output", str(report))
        require(proc.returncode == 0, f"compare failed: stdout={proc.stdout!r} stderr={proc.stderr!r}")
        text = report.read_text(encoding="utf-8")
        require("# Browser Automation Benchmark Report" in text, "report title missing")
        require("Playwright" in text and "Chrome Native Bridge" in text, "report comparison names missing")
        require("Gap Backlog" in text, "gap backlog missing")

        live_out = Path(tmp) / "chrome-bridge-zero.json"
        proc = run("run", "--adapter", "chrome-bridge", "--iterations", "0", "--output", str(live_out))
        require(proc.returncode == 0, f"chrome-bridge zero-iteration run should not require --base-url: stdout={proc.stdout!r} stderr={proc.stderr!r}")
        live_data = json.loads(live_out.read_text(encoding="utf-8"))
        require(live_data.get("adapter") == "chrome-bridge", "chrome-bridge adapter name not recorded")

        for adapter in ["playwright", "puppeteer"]:
            adapter_out = Path(tmp) / f"{adapter}-zero.json"
            proc = run("run", "--adapter", adapter, "--iterations", "0", "--output", str(adapter_out))
            require(proc.returncode == 0, f"{adapter} zero-iteration run should not require optional deps: stdout={proc.stdout!r} stderr={proc.stderr!r}")
            adapter_data = json.loads(adapter_out.read_text(encoding="utf-8"))
            require(adapter_data.get("adapter") == adapter, f"{adapter} adapter name not recorded")

        for adapter in ["puppeteer", "chrome-devtools-mcp"]:
            zero_guard_out = Path(tmp) / f"{adapter}-zero-guard.json"
            proc = run(
                "run", "--adapter", adapter, "--iterations", "0",
                "--browser-rss-limit-mb", "1", "--output", str(zero_guard_out),
                extra_env={"CHROME_BRIDGE_BENCHMARK_PS_OUTPUT": ps_sample},
            )
            require(proc.returncode == 0,
                    f"{adapter} zero-iteration run must skip the RSS guard: stdout={proc.stdout!r} stderr={proc.stderr!r}")
            require(zero_guard_out.exists(),
                    f"{adapter} zero-iteration run must still write its report under a low RSS limit")

        guard_out = Path(tmp) / "rss-guard.json"
        old_override = os.environ.get("CHROME_BRIDGE_BENCHMARK_PS_OUTPUT")
        os.environ["CHROME_BRIDGE_BENCHMARK_PS_OUTPUT"] = ps_sample
        try:
            benchmark_harness.handle_run(SimpleNamespace(
                adapter="playwright",
                iterations=1,
                output=str(guard_out),
                base_url="http://127.0.0.1:1/",
                browser_rss_limit_mb=3,
            ))
        except benchmark_harness.BrowserRssLimitExceeded as exc:
            require("browser RSS 4 MB exceeds limit 3 MB" in str(exc),
                    "browser RSS guard failure must bubble out of the live runner path")
        else:
            raise AssertionError("browser RSS guard must make live runner paths fail fatally")
        finally:
            if old_override is None:
                os.environ.pop("CHROME_BRIDGE_BENCHMARK_PS_OUTPUT", None)
            else:
                os.environ["CHROME_BRIDGE_BENCHMARK_PS_OUTPUT"] = old_override
        require(not guard_out.exists(), "browser RSS guard must not write a normal unsupported report")
        proc = run(
            "run", "--adapter", "playwright", "--iterations", "1",
            "--browser-rss-limit-mb", "3", "--output", str(Path(tmp) / "rss-cli.json"),
            extra_env={"CHROME_BRIDGE_BENCHMARK_PS_OUTPUT": ps_sample},
        )
        require(proc.returncode != 0, "browser RSS CLI guard must fail")
        require(proc.stderr.strip() == "browser RSS 4 MB exceeds limit 3 MB",
                "browser RSS CLI guard must print a concise error")
        require("Traceback" not in proc.stderr, "browser RSS CLI guard must not print a traceback")

        second = Path(tmp) / "other.json"
        other = dict(data)
        other["adapter"] = "unknown-adapter"
        second.write_text(json.dumps(other), encoding="utf-8")
        multi_report = Path(tmp) / "multi-report.md"
        proc = run("compare", "--input", str(out), "--input", str(second), "--output", str(multi_report))
        require(proc.returncode == 0, f"multi-input compare failed: stdout={proc.stdout!r} stderr={proc.stderr!r}")
        multi_text = multi_report.read_text(encoding="utf-8")
        require("noop status" in multi_text and "unknown-adapter status" in multi_text, "multi-input operation columns missing")
        junit = Path(tmp) / "benchmark.xml"
        step_summary = Path(tmp) / "step-summary.md"
        step_summary.write_text("existing summary\n", encoding="utf-8")
        proc = run(
            "compare",
            "--input", str(out),
            "--output", str(Path(tmp) / "ci-report.md"),
            "--junit-output", str(junit),
            "--github-step-summary", str(step_summary),
        )
        require(proc.returncode == 0, f"CI compare exports failed: stdout={proc.stdout!r} stderr={proc.stderr!r}")
        junit_text = junit.read_text(encoding="utf-8")
        require("<testsuite name=\"browser-automation-benchmark\"" in junit_text, "JUnit testsuite missing")
        require("<testcase name=\"noop.ping\"" in junit_text, "JUnit testcase missing operation")
        step_text = step_summary.read_text(encoding="utf-8")
        require(step_text.startswith("existing summary\n"), "GitHub step summary export must append to existing content")
        require("## Browser Automation Benchmark" in step_text, "GitHub step summary heading missing")
        require("| Operation | noop status | noop median ms |" in step_text, "GitHub step summary timings missing")
        proc = run("compare", "--input", str(out), "--input", str(out), "--output", str(Path(tmp) / "dupe.md"))
        require(proc.returncode != 0 and "duplicate benchmark adapter noop" in proc.stderr, "duplicate adapter should fail")

        require("## Normalized Scorecard" in text, "normalized scorecard missing")
        require("First-party ecosystem integrations" not in text, "BENCH-004 should be removed after CI exports")
        require("### BENCH-004" not in text, "closed BENCH-004 ticket ID should not be reused")
        require("### BENCH-005: Interactive destructive approval" in text, "remaining gap should keep stable BENCH-005 ID")
        require("## Claim Discipline" in text, "claim discipline section missing")
        require("| Tool | Speed | Capability | Auth Reuse | Ergonomics | Overall |" in text, "scorecard table missing")

    print("Benchmark harness contract OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
