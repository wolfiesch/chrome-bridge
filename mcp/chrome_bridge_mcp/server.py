"""FastMCP server exposing the Chrome native-messaging bridge.

P1 walking skeleton: a curated set of high-frequency tools plus a
``browser_action`` escape hatch covering the full bridge action surface. Every
tab-scoped tool takes an optional ``tab_id``; when omitted the active tab is
used.
"""
import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from .transport import BridgeError, call, resolve_tab_id

mcp = FastMCP("chrome-bridge")

_PNG_PREFIX = "data:image/png;base64,"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


@mcp.tool()
def browser_list_tabs() -> str:
    """List all open browser tabs (id, url, title, active, status)."""
    return _text(call("getTabs"))


@mcp.tool()
def browser_navigate(url: str) -> str:
    """Open a URL in a new tab and return the new tab id."""
    return _text(call("navigate", {"url": url}))


@mcp.tool()
def browser_snapshot(tab_id: Optional[int] = None) -> str:
    """Accessibility snapshot of the page: the structured view of what is on it.

    Prefer this over a screenshot for deciding what to click or read.
    """
    tid = resolve_tab_id(tab_id)
    return _text(call("observe", {"tabId": tid}))


@mcp.tool()
def browser_extract_text(tab_id: Optional[int] = None, max_chars: int = 20000) -> str:
    """Extract visible text from the page, truncated to ``max_chars``."""
    tid = resolve_tab_id(tab_id)
    return _text(call("extractText", {"tabId": tid, "maxChars": max_chars}))


@mcp.tool()
def browser_screenshot(tab_id: Optional[int] = None) -> ImageContent:
    """Capture a PNG screenshot of the visible tab, returned inline as an image."""
    tid = resolve_tab_id(tab_id)
    result = call("screenshot", {"tabId": tid, "format": "png"})
    data_url = result.get("dataUrl", "") if isinstance(result, dict) else ""
    if not data_url.startswith(_PNG_PREFIX):
        raise BridgeError("Screenshot response did not include a PNG data URL.")
    return ImageContent(
        type="image", data=data_url[len(_PNG_PREFIX):], mimeType="image/png"
    )


@mcp.tool()
def browser_click(selector: str, tab_id: Optional[int] = None) -> str:
    """Click the first element matching a CSS selector."""
    tid = resolve_tab_id(tab_id)
    return _text(call("click", {"tabId": tid, "selector": selector}))


@mcp.tool()
def browser_type(selector: str, text: str, tab_id: Optional[int] = None) -> str:
    """Focus an element and insert text (does not clear existing value)."""
    tid = resolve_tab_id(tab_id)
    return _text(call("type", {"tabId": tid, "selector": selector, "text": text}))


@mcp.tool()
def browser_fill(selector: str, text: str, tab_id: Optional[int] = None) -> str:
    """Clear an element, then insert text."""
    tid = resolve_tab_id(tab_id)
    return _text(call("fill", {"tabId": tid, "selector": selector, "text": text}))


@mcp.tool()
def browser_wait_for(
    mode: str,
    tab_id: Optional[int] = None,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url_substring: Optional[str] = None,
    timeout_ms: int = 10000,
) -> str:
    """Wait for a page condition.

    ``mode`` is one of ``load``, ``selector``, ``text``, ``url``. Provide
    ``selector`` for ``selector`` mode, ``text`` for ``text`` mode, and
    ``url_substring`` for ``url`` mode.
    """
    tid = resolve_tab_id(tab_id)
    if mode == "load":
        return _text(call("waitForLoad", {"tabId": tid, "timeoutMs": timeout_ms}))
    if mode == "selector":
        if not selector:
            raise BridgeError("wait_for mode 'selector' requires a selector.")
        return _text(call("waitForSelector", {"tabId": tid, "selector": selector, "timeoutMs": timeout_ms}))
    if mode == "text":
        if not text:
            raise BridgeError("wait_for mode 'text' requires text.")
        return _text(call("waitForText", {"tabId": tid, "text": text, "timeoutMs": timeout_ms}))
    if mode == "url":
        if not url_substring:
            raise BridgeError("wait_for mode 'url' requires url_substring.")
        return _text(call("waitForUrl", {"tabId": tid, "substring": url_substring, "timeoutMs": timeout_ms}))
    raise BridgeError(f"Unknown wait_for mode: {mode!r} (use load|selector|text|url).")


@mcp.tool()
def browser_tab_control(op: str, tab_id: Optional[int] = None) -> str:
    """Tab lifecycle control.

    ``op`` is one of ``activate``, ``close``, ``reload``, ``back``, ``forward``.
    """
    tid = resolve_tab_id(tab_id)
    actions = {
        "activate": "activateTab",
        "close": "closeTab",
        "reload": "reload",
        "back": "goBack",
        "forward": "goForward",
    }
    action = actions.get(op)
    if action is None:
        raise BridgeError(f"Unknown tab op: {op!r} (use activate|close|reload|back|forward).")
    return _text(call(action, {"tabId": tid}))


@mcp.tool()
def browser_action(action: str, payload: Optional[dict] = None) -> str:
    """Escape hatch: send any raw bridge action with its payload.

    Covers the full action surface (interception, geolocation, monitoring,
    console/network logs, downloadUrl, storageState, executeScript, setViewport,
    handleDialog, batch, etc.). Returns the raw result as JSON text.
    """
    return _text(call(action, payload or {}))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
