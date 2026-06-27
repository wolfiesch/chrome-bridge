"""Bridge transport for the MCP server.

Reuses ``send_command_data`` from the repo-root ``test_client.py`` verbatim so
the MCP surface and the CLI share one wire implementation (connect-retry, token
load, newline framing, socket timeout). ``test_client`` guards its CLI behind
``if __name__ == '__main__'``, so importing it is side-effect-free.
"""
import importlib.util
import os
import sys
import threading

# Repo root is the parent of the ``mcp/`` package directory.
_REPO_ROOT = os.environ.get(
    "BRIDGE_REPO_ROOT",
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
)


def _load_test_client():
    path = os.path.join(_REPO_ROOT, "test_client.py")
    if not os.path.exists(path):
        raise RuntimeError(
            f"Cannot locate test_client.py at {path}. Set BRIDGE_REPO_ROOT to the "
            "chrome-native-bridge checkout."
        )
    spec = importlib.util.spec_from_file_location("chrome_bridge_test_client", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("chrome_bridge_test_client", module)
    spec.loader.exec_module(module)
    return module


_client = _load_test_client()
# One bridge consumer at a time: serialize outbound TCP calls.
_call_lock = threading.Lock()


class BridgeError(Exception):
    """Raised when the bridge transport or the extension reports a failure."""


def call(action, payload=None, read_timeout_ms=None, confirmation_token=None):
    """Send one action to the bridge and return its ``result`` payload.

    Raises ``BridgeError`` with an actionable message on transport failures,
    auth rejection, or an unsuccessful extension result. ``read_timeout_ms``
    extends the post-connect socket read deadline for long waits (e.g. human
    handoff); the wire timeout is kept above it so transport never fires first.
    """
    with _call_lock:
        exit_code, response, stderr = _client.send_command_data(
            action, payload or {}, read_timeout_ms=read_timeout_ms,
            confirmation_token=confirmation_token)

    if response is None:
        raise BridgeError(stderr or "No response from bridge.")

    if response.get("success") is not True:
        err = response.get("error") or stderr or "Bridge reported failure."
        if err == "unauthorized":
            err = ("unauthorized: bridge token mismatch. Ensure the MCP server "
                   "reads the same bridge_token.txt as the running host "
                   "(check BRIDGE_TOKEN_FILE / BRIDGE_REPO_ROOT).")
        raise BridgeError(err)

    result = response.get("result")
    if isinstance(result, dict) and result.get("success") is False:
        raise BridgeError(result.get("err") or "Extension action failed.")
    return result


def resolve_tab_id(tab_id):
    """Return ``tab_id`` if given, else the active tab's id.

    Falls back to the first tab when no tab is marked active.
    """
    if tab_id is not None:
        return tab_id
    tabs = call("getTabs")
    if not isinstance(tabs, list) or not tabs:
        raise BridgeError("No open tabs to resolve an active tab from.")
    for tab in tabs:
        if tab.get("active"):
            return tab.get("id")
    return tabs[0].get("id")
