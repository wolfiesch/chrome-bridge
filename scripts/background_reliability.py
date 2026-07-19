#!/usr/bin/env python3
"""Exercise a task-owned tab and report any focus or tab-ownership violations."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import test_client  # noqa: E402


def bridge(action, payload=None):
    code, response, stderr = test_client.send_command_data(action, payload or {})
    if code != 0 or not response or response.get("success") is not True:
        raise RuntimeError(stderr or json.dumps(response))
    return response.get("result")


def active_tabs(tabs):
    return sorted(
        (tab.get("windowId"), tab.get("id"))
        for tab in tabs
        if tab.get("active") is True
    )


def frontmost_app():
    if sys.platform != "darwin":
        return None
    try:
        asn = subprocess.check_output(["lsappinfo", "front"], text=True).strip()
        info = subprocess.check_output(
            ["lsappinfo", "info", "-only", "bundleID", asn], text=True
        ).strip()
        return info.split('="', 1)[1].rstrip('"')
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=60)
    parser.add_argument("--interval-seconds", type=float, default=2)
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--output", default="/tmp/chrome-bridge-background-reliability.json")
    parser.add_argument("--screenshot-every", type=int, default=5)
    args = parser.parse_args()

    started = time.time()
    baseline_tabs = bridge("getTabs")
    baseline_active = active_tabs(baseline_tabs)
    baseline_ids = {tab.get("id") for tab in baseline_tabs}
    baseline_frontmost = frontmost_app()
    session = bridge("createTaskSession", {"name": "Reliability test"})
    session_id = session["sessionId"]
    owned_ids = set()
    violations = []
    samples = []
    iteration = 0
    run_error = None

    try:
        opened = bridge("navigateTaskSession", {
            "sessionId": session_id,
            "url": args.url,
            "active": False,
            "reuse": True,
        })
        owned_ids.add(opened["tabId"])
        deadline = time.monotonic() + max(0, args.duration_seconds)
        while True:
            tabs = bridge("getTabs")
            current_active = active_tabs(tabs)
            current_frontmost = frontmost_app()
            current_ids = {tab.get("id") for tab in tabs}
            unexpected = sorted(current_ids - baseline_ids - owned_ids)
            owned = [tab for tab in tabs if tab.get("id") in owned_ids]
            owned_ready = any(
                tab.get("status") == "complete"
                and str(tab.get("url") or "").startswith(("http://", "https://"))
                for tab in owned
            )
            if current_active != baseline_active:
                violations.append({"kind": "active_tabs_changed", "iteration": iteration, "actual": current_active})
            if baseline_frontmost and current_frontmost != baseline_frontmost:
                violations.append({"kind": "frontmost_app_changed", "iteration": iteration, "actual": current_frontmost})
            if unexpected:
                violations.append({"kind": "unexpected_tabs", "iteration": iteration, "tabIds": unexpected})
            if any(tab.get("active") is True for tab in owned):
                violations.append({"kind": "owned_tab_became_active", "iteration": iteration})
            if owned_ready and args.screenshot_every > 0 and iteration % args.screenshot_every == 0:
                bridge("screenshot", {"tabId": opened["tabId"], "format": "png", "quiet": True})
            samples.append({
                "iteration": iteration,
                "elapsedSeconds": round(time.time() - started, 3),
                "activeTabs": current_active,
                "frontmostApp": current_frontmost,
                "ownedTabs": owned,
            })
            iteration += 1
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.05, args.interval_seconds))
    except Exception as exc:
        run_error = str(exc)
    finally:
        cleanup_error = None
        try:
            bridge("closeTaskSession", {"sessionId": session_id})
        except Exception as exc:
            cleanup_error = str(exc)

    report = {
        "success": not violations and cleanup_error is None and run_error is None,
        "startedAt": started,
        "durationSeconds": round(time.time() - started, 3),
        "baselineActiveTabs": baseline_active,
        "baselineFrontmostApp": baseline_frontmost,
        "sessionId": session_id,
        "ownedTabIds": sorted(owned_ids),
        "sampleCount": len(samples),
        "violations": violations,
        "cleanupError": cleanup_error,
        "runError": run_error,
        "samples": samples,
    }
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "success", "durationSeconds", "sampleCount", "violations", "runError", "cleanupError"
    )}, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
