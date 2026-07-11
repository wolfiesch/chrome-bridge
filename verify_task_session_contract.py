#!/usr/bin/env python3
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
failures = []


def expect(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")


for path in (ROOT / "background.js", ROOT / "extension" / "background.js"):
    text = path.read_text(encoding="utf-8")
    for needle in (
        'case "createTaskSession"',
        'case "navigateTaskSession"',
        'case "getTaskSessions"',
        'case "closeTaskSession"',
        'TASK_SESSIONS_KEY',
        'chrome.storage.local',
        'chrome.tabs.onRemoved.addListener',
        'chrome.tabs.group',
        'active: active === true',
        'closedTabIds: tabIds',
    ):
        expect(needle in text, f"{path.name} missing task-session contract: {needle}")
    close_body = text.split("async function closeTaskSession", 1)[1].split("chrome.tabs.onRemoved", 1)[0]
    expect(
        close_body.index("await saveTaskSessions(sessions)") < close_body.index("await chrome.tabs.remove(tabIds)"),
        f"{path.name} must persist session deletion before tab removal events fire",
    )

for path in (ROOT / "manifest.json", ROOT / "extension" / "manifest.json"):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expect("tabGroups" in manifest.get("permissions", []), f"{path} missing tabGroups permission")

bridge = (ROOT / "bridge.py").read_text(encoding="utf-8")
expect("'navigateTaskSession'" in bridge, "host missing navigateTaskSession policy classification")
expect("'closeTaskSession'" in bridge, "host missing closeTaskSession policy classification")

harness = (ROOT / "scripts" / "background_reliability.py").read_text(encoding="utf-8")
for needle in ("active_tabs_changed", "frontmost_app_changed", "unexpected_tabs", "owned_tab_became_active", "owned_ready", "runError"):
    expect(needle in harness, f"reliability harness missing invariant {needle}")

if failures:
    raise SystemExit(1)
print("Task session contract OK")
