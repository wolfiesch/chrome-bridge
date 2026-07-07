"""Example Chrome Bridge action layer for a browser-use-style agent loop.

Run this only after Chrome Bridge is installed, Chrome is running, and your
bridge_policy.json allows the actions and origins you intend to automate.
"""

from __future__ import annotations

from typing import Iterable, Optional

from chrome_bridge_session import ChromeBridgeError, ChromeBridgeSession

try:
    import browser_use  # type: ignore  # noqa: F401
except ImportError:  # pragma: no cover - example-only guidance
    browser_use = None  # type: ignore
    BROWSER_USE_IMPORT_ERROR = (
        "browser-use is not installed. Install it in your agent environment if "
        "you want to wire this adapter into browser-use. The standalone "
        "ChromeBridgeSession loop below does not require browser-use."
    )
else:
    BROWSER_USE_IMPORT_ERROR = ""


class SimpleBridgeAgent:
    """Tiny standalone loop showing how an agent can execute bridge actions.

    A real LLM planner would produce these steps dynamically. This class keeps
    the execution layer explicit so it can be reused or adapted without relying
    on undocumented browser-use internals.
    """

    def __init__(self, session: ChromeBridgeSession) -> None:
        self.session = session

    def run(self, steps: Iterable[dict]) -> None:
        for step in steps:
            action = step["action"]
            if action == "navigate":
                self.session.navigate(step["url"])
            elif action == "wait_for_selector":
                self.session.wait_for_selector(
                    step["selector"],
                    timeout_ms=step.get("timeout_ms", 10000),
                    tab_id=step.get("tab_id"),
                )
            elif action == "click":
                self.session.click(step["selector"], tab_id=step.get("tab_id"))
            elif action == "type":
                self.session.type_text(
                    step["selector"], step["text"], tab_id=step.get("tab_id")
                )
            elif action == "fill":
                self.session.fill(step["selector"], step["text"], tab_id=step.get("tab_id"))
            elif action == "handoff":
                self.session.wait_for_handoff(
                    step["message"],
                    mode=step.get("mode", "manual"),
                    arg=step.get("arg"),
                    timeout_ms=step.get("timeout_ms", 120000),
                    tab_id=step.get("tab_id"),
                )
            elif action == "read":
                text = self.session.extract_text(
                    tab_id=step.get("tab_id"), max_chars=step.get("max_chars", 20000)
                )
                print(text)
            else:
                raise ValueError("Unknown action: %s" % action)


def run_standalone_example() -> None:
    """Demonstrate real-profile login handoff without browser-use APIs."""
    steps = [
        {"action": "navigate", "url": "https://example.com/login"},
        {
            "action": "handoff",
            "message": "Please sign in, complete any 2FA, then return here.",
            "mode": "url",
            "arg": "/dashboard",
            "timeout_ms": 180000,
        },
        {"action": "wait_for_selector", "selector": "main", "timeout_ms": 30000},
        {"action": "read", "max_chars": 4000},
    ]
    with ChromeBridgeSession() as session:
        SimpleBridgeAgent(session).run(steps)


def build_browser_use_adapter(session: ChromeBridgeSession) -> Optional[object]:
    """Placeholder for a browser-use integration boundary.

    TODO: Replace this with browser-use's documented custom-browser/action API
    once the target browser-use version and integration surface are chosen. This
    repository should not fabricate private browser-use class names or method
    contracts. The object returned here is intentionally absent until that API is
    pinned by the consumer application.
    """
    if browser_use is None:
        raise RuntimeError(BROWSER_USE_IMPORT_ERROR)
    raise NotImplementedError(
        "browser-use wiring is intentionally left as a TODO until a documented "
        "custom execution API is selected. Use ChromeBridgeSession directly for "
        "the transport-safe action layer."
    )


if __name__ == "__main__":
    try:
        run_standalone_example()
    except ChromeBridgeError as exc:
        raise SystemExit("Chrome Bridge error: %s" % exc)
