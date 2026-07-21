#!/usr/bin/env python3
"""Offline contract checks for quiet reads and task-scoped debugger reuse."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent
FAILURES = []


def expect(condition, message):
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")


def function_body(text, name, next_name):
    start = f"async function {name}"
    end = f"async function {next_name}"
    expect(start in text, f"missing function {name}")
    expect(end in text, f"missing function boundary {next_name}")
    if start not in text or end not in text:
        return ""
    return text.split(start, 1)[1].split(end, 1)[0]


for path in (ROOT / "background.js", ROOT / "extension" / "background.js"):
    text = path.read_text(encoding="utf-8")
    label = str(path.relative_to(ROOT))

    for needle in (
        "TASK_DEBUGGER_IDLE_MS",
        "taskDebuggers",
        "findTaskSessionForTab",
        "ensureTaskDebugger",
        "withTaskDebugger",
        "detachTaskSessionDebuggers",
        "observeTabWithoutDebugger",
        "pageContainsText",
        "readBodyLengthInTab",
    ):
        expect(needle in text, f"{label} missing quiet-debugger contract: {needle}")

    with_debugger = function_body(text, "withDebugger", "evaluateWithDebugger")
    expect(
        "findTaskSessionForTab" in with_debugger and "withTaskDebugger" in with_debugger,
        f"{label} must reuse a debugger for task-owned tabs",
    )
    expect(
        "debuggerAttach" in with_debugger and "debuggerDetach" in with_debugger,
        f"{label} must preserve one-shot compatibility for unowned tabs",
    )

    wait_for_text = function_body(text, "waitForText", "getCurrentState")
    expect("pageContainsText" in wait_for_text, f"{label} waitForText must use a quiet page probe")
    expect("withDebugger" not in wait_for_text, f"{label} waitForText must not attach the debugger")

    extract_text = function_body(text, "extractText", "getHTML")
    expect("chrome.scripting.executeScript" in extract_text, f"{label} extractText must use chrome.scripting")
    expect("withDebugger" not in extract_text, f"{label} extractText must not attach the debugger")

    get_html = function_body(text, "getHTML", "getElementCenter")
    expect("chrome.scripting.executeScript" in get_html, f"{label} getHTML must use chrome.scripting")
    expect("withDebugger" not in get_html, f"{label} getHTML must not attach the debugger")

    observe = function_body(text, "observeTab", "startMonitoring")
    expect(
        "options.compact" in observe and "observeTabWithoutDebugger" in observe,
        f"{label} compact observe must use the quiet DOM snapshot",
    )
    expect(
        "withDebugger" in observe and "Accessibility.getFullAXTree" in observe,
        f"{label} full observe must preserve the detailed debugger fallback",
    )

    close_session = function_body(text, "closeTaskSession", "tabOrigin")
    expect(
        "await detachTaskSessionDebuggers(sessionId)" in close_session,
        f"{label} closing a task session must detach its debugger leases",
    )
    if "await detachTaskSessionDebuggers(sessionId)" in close_session and "await chrome.tabs.remove(tabIds)" in close_session:
        expect(
            close_session.index("await detachTaskSessionDebuggers(sessionId)")
            < close_session.index("await chrome.tabs.remove(tabIds)"),
            f"{label} must detach task debuggers before closing tabs",
        )

    find_session = function_body(text, "findTaskSessionForTab", "detachTaskDebugger")
    expect(
        "tab.groupId === session.groupId" in find_session,
        f"{label} must recognize tabs manually moved into a task group",
    )

    task_debugger = function_body(text, "withTaskDebugger", "detachTaskSessionDebuggers")
    expect("busyCount" in task_debugger, f"{label} must not idle-detach during an active task command")

    monitoring = function_body(text, "startMonitoring", "stopMonitoring")
    expect(
        "findTaskSessionForTab" in monitoring and "ensureTaskDebugger" in monitoring,
        f"{label} monitoring must join a task-owned debugger connection",
    )

    interception = function_body(text, "startInterception", "stopInterception")
    expect(
        "findTaskSessionForTab" in interception and "ensureTaskDebugger" in interception,
        f"{label} interception must join a task-owned debugger connection",
    )


commands = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
expect("compact snapshots" in commands and "without attaching Chrome's debugger" in commands,
       "commands documentation must explain quiet reads")
expect("task-owned tabs" in commands and "reuses one debugger connection" in commands,
       "commands documentation must explain task-scoped debugger reuse")

verification = (ROOT / "docs" / "verification.md").read_text(encoding="utf-8")
expect("verify_quiet_debugger_contract.py" in verification,
       "verification guide must run the quiet-debugger contract")

if FAILURES:
    raise SystemExit(1)
print("Quiet debugger contract OK")
