#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HARNESS = SCRIPT_DIR / "benchmark_harness.py"


def run(*args):
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        cwd=SCRIPT_DIR,
        text=True,
        capture_output=True,
        timeout=30,
    )


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="chrome-bridge-bench-") as tmp:
        out = Path(tmp) / "results.json"
        report = Path(tmp) / "report.md"

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

        second = Path(tmp) / "other.json"
        other = dict(data)
        other["adapter"] = "unknown-adapter"
        second.write_text(json.dumps(other), encoding="utf-8")
        multi_report = Path(tmp) / "multi-report.md"
        proc = run("compare", "--input", str(out), "--input", str(second), "--output", str(multi_report))
        require(proc.returncode == 0, f"multi-input compare failed: stdout={proc.stdout!r} stderr={proc.stderr!r}")
        multi_text = multi_report.read_text(encoding="utf-8")
        require("noop status" in multi_text and "unknown-adapter status" in multi_text, "multi-input operation columns missing")
        proc = run("compare", "--input", str(out), "--input", str(out), "--output", str(Path(tmp) / "dupe.md"))
        require(proc.returncode != 0 and "duplicate benchmark adapter noop" in proc.stderr, "duplicate adapter should fail")

        require("## Normalized Scorecard" in text, "normalized scorecard missing")
        require("## Gap Tickets" in text, "gap tickets missing")
        require("## Claim Discipline" in text, "claim discipline section missing")
        require("| Tool | Speed | Capability | Auth Reuse | Ergonomics | Overall |" in text, "scorecard table missing")

    print("Benchmark harness contract OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
