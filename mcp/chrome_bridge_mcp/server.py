"""FastMCP server exposing the Chrome native-messaging bridge.

P2 contract: every tool is a plain module-level function (directly callable in
tests and scripts) and the server is assembled on demand by ``build_server``,
which scopes the exposed surface from ``readonly``/``allow_sensitive`` flags
(args or the ``BRIDGE_MCP_READONLY`` / ``BRIDGE_MCP_ALLOW_SENSITIVE`` env vars)
and applies ``readOnly``/``destructive`` annotations. Every tab-scoped tool
takes an optional ``tab_id``; when omitted the active tab is used.
"""
import json
import os
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from .transport import BridgeError, call, resolve_tab_id

_PNG_PREFIX = "data:image/png;base64,"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def _truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _truncate(s, limit):
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... [truncated {len(s) - limit} chars]"


def _expand_existing_files(paths):
    """Resolve and validate local upload paths, mirroring
    ``test_client.expand_existing_files`` but raising ``BridgeError`` instead of
    exiting, so missing files fail before Chrome is contacted."""
    expanded = []
    for path in paths:
        abs_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(abs_path):
            raise BridgeError(f"Upload file not found: {abs_path}")
        expanded.append(abs_path)
    return expanded


def browser_list_tabs() -> str:
    """List all open browser tabs (id, url, title, active, status)."""
    return _text(call("getTabs"))


def browser_navigate(url: str) -> str:
    """Open a URL in a new tab and return the new tab id."""
    return _text(call("navigate", {"url": url}))


def browser_snapshot(tab_id: Optional[int] = None) -> str:
    """Accessibility snapshot of the page: the structured view of what is on it.

    Prefer this over a screenshot for deciding what to click or read.
    """
    tid = resolve_tab_id(tab_id)
    return _text(call("observe", {"tabId": tid}))


def browser_extract_text(tab_id: Optional[int] = None, max_chars: int = 20000) -> str:
    """Extract visible text from the page, truncated to ``max_chars``."""
    tid = resolve_tab_id(tab_id)
    return _text(call("extractText", {"tabId": tid, "maxChars": max_chars}))


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


def browser_click(selector: str, tab_id: Optional[int] = None) -> str:
    """Click the first element matching a CSS selector."""
    tid = resolve_tab_id(tab_id)
    return _text(call("click", {"tabId": tid, "selector": selector}))


def browser_type(selector: str, text: str, tab_id: Optional[int] = None) -> str:
    """Focus an element and insert text (does not clear existing value)."""
    tid = resolve_tab_id(tab_id)
    return _text(call("type", {"tabId": tid, "selector": selector, "text": text}))


def browser_fill(selector: str, text: str, tab_id: Optional[int] = None) -> str:
    """Clear an element, then insert text."""
    tid = resolve_tab_id(tab_id)
    return _text(call("fill", {"tabId": tid, "selector": selector, "text": text}))


def browser_hover(selector: str, tab_id: Optional[int] = None) -> str:
    """Hover the pointer over the first element matching a CSS selector."""
    tid = resolve_tab_id(tab_id)
    return _text(call("hover", {"tabId": tid, "selector": selector}))


def browser_scroll(
    delta_x: float,
    delta_y: float,
    selector: Optional[str] = None,
    tab_id: Optional[int] = None,
) -> str:
    """Scroll by ``delta_x``/``delta_y``; scope to ``selector`` when given."""
    tid = resolve_tab_id(tab_id)
    return _text(call("scroll", {
        "tabId": tid,
        "deltaX": delta_x,
        "deltaY": delta_y,
        "selector": selector,
    }))


def browser_press(key: str, tab_id: Optional[int] = None) -> str:
    """Press a key (or key combination spec) on the page."""
    tid = resolve_tab_id(tab_id)
    return _text(call("press", {"tabId": tid, "key": key}))


def browser_drag(
    from_selector: str,
    to_selector: str,
    tab_id: Optional[int] = None,
) -> str:
    """Drag from one element to another by CSS selector."""
    tid = resolve_tab_id(tab_id)
    return _text(call("drag", {
        "tabId": tid,
        "fromSelector": from_selector,
        "toSelector": to_selector,
    }))


def browser_select(selector: str, value: str, tab_id: Optional[int] = None) -> str:
    """Select an option ``value`` in a ``<select>`` element."""
    tid = resolve_tab_id(tab_id)
    return _text(call("select", {"tabId": tid, "selector": selector, "value": value}))


def browser_upload_file(
    selector: str,
    files: list,
    tab_id: Optional[int] = None,
) -> str:
    """Set files on a file ``<input>``; local paths are validated first."""
    expanded = _expand_existing_files(files)
    tid = resolve_tab_id(tab_id)
    return _text(call("uploadFile", {"tabId": tid, "selector": selector, "files": expanded}))


def browser_get_cookies(domain: str) -> str:
    """Return cookies for ``domain`` (sensitive)."""
    return _text(call("getCookies", {"domain": domain}))


def browser_get_html(tab_id: Optional[int] = None, max_chars: int = 200000) -> str:
    """Return the page's serialized HTML, truncated to ``max_chars``."""
    tid = resolve_tab_id(tab_id)
    result = call("getHTML", {"tabId": tid})
    html = result.get("html") if isinstance(result, dict) else result
    if not isinstance(html, str):
        raise BridgeError("getHTML response did not include html.")
    return _truncate(html, max_chars)


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


def browser_action(action: str, payload: Optional[dict] = None) -> str:
    """Escape hatch: send any raw bridge action with its payload.

    Covers the full action surface (interception, geolocation, monitoring,
    console/network logs, downloadUrl, storageState, executeScript, setViewport,
    handleDialog, batch, etc.). Returns the raw result as JSON text.
    """
    return _text(call(action, payload or {}))


def browser_lease(ttl_ms: int = 300000) -> str:
    """Acquire exclusive cooperative control of the shared real-Chrome profile.

    Cooperative multi-agent leasing: while you hold the lease, other clients
    are blocked with 'leased by <owner>' until you release it or the lease
    expires after ``ttl_ms`` milliseconds (TTL).
    """
    return _text(call("lease", {"ttlMs": ttl_ms}))


def browser_release() -> str:
    """Release the cooperative lease on the shared real-Chrome profile.

    Frees the exclusive control acquired via ``browser_lease`` so other clients
    are no longer blocked with 'leased by <owner>'.
    """
    return _text(call("release", {}))


def browser_lease_status() -> str:
    """Report the current cooperative lease on the shared real-Chrome profile.

    Shows who (if anyone) holds exclusive control; other clients are blocked
    with 'leased by <owner>' until release or TTL expiry.
    """
    return _text(call("leaseStatus", {}))


# (func, mutating, sensitive) for every tool in the surface.
_TOOLS = [
    (browser_list_tabs, False, False),
    (browser_snapshot, False, False),
    (browser_extract_text, False, False),
    (browser_screenshot, False, False),
    (browser_get_html, False, False),
    (browser_wait_for, False, False),
    (browser_get_cookies, False, True),
    (browser_navigate, True, False),
    (browser_click, True, False),
    (browser_type, True, False),
    (browser_fill, True, False),
    (browser_hover, True, False),
    (browser_scroll, True, False),
    (browser_press, True, False),
    (browser_drag, True, False),
    (browser_select, True, False),
    (browser_upload_file, True, False),
    (browser_tab_control, True, False),
    (browser_action, True, True),
    (browser_lease, True, False),
    (browser_release, True, False),
    (browser_lease_status, False, False),
]


def build_server(readonly=None, allow_sensitive=None) -> FastMCP:
    """Assemble a ``FastMCP`` server scoped by ``readonly``/``allow_sensitive``.

    Each flag falls back to its env var (``BRIDGE_MCP_READONLY`` /
    ``BRIDGE_MCP_ALLOW_SENSITIVE``) parsed with ``_truthy``. Mutating tools are
    dropped in read-only mode; sensitive tools require ``allow_sensitive``.
    """
    if readonly is None:
        readonly = _truthy(os.environ.get("BRIDGE_MCP_READONLY", ""))
    if allow_sensitive is None:
        allow_sensitive = _truthy(os.environ.get("BRIDGE_MCP_ALLOW_SENSITIVE", ""))

    m = FastMCP("chrome-bridge")

    for func, mutating, sensitive in _TOOLS:
        if readonly and mutating:
            continue
        if sensitive and not allow_sensitive:
            continue
        m.tool(annotations=ToolAnnotations(
            readOnlyHint=not mutating, destructiveHint=mutating
        ))(func)

    @m.resource("browser://tabs")
    def tabs_resource() -> str:
        """Live list of open browser tabs."""
        return _text(call("getTabs"))

    @m.resource("browser://tab/{id}/state")
    def tab_state_resource(id: int) -> str:
        """Current state of a single tab."""
        return _text(call("getCurrentState", {"tabId": int(id)}))

    return m


def main() -> None:
    transport = os.environ.get("BRIDGE_MCP_TRANSPORT", "stdio")
    if transport == "http":
        host = os.environ.get("BRIDGE_MCP_HTTP_HOST", "127.0.0.1")
        port = os.environ.get("BRIDGE_MCP_HTTP_PORT", "8723")
        m = build_server()
        try:
            m.settings.host = host
            m.settings.port = int(port)
        except AttributeError:
            pass
        m.run(transport="streamable-http")
    else:
        build_server().run()


if __name__ == "__main__":
    main()
