"""Stdlib Chrome Bridge client for browser-use-style agent loops.

This module intentionally talks to the local Chrome Bridge TCP host directly
instead of going through the MCP server. It holds the raw bridge token, so the
host policy file remains the enforcement boundary.
"""

from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


JsonDict = Dict[str, Any]


class ChromeBridgeError(RuntimeError):
    """Raised when the bridge transport or host reports an error."""


class ChromeBridgeSession:
    """Persistent newline-delimited JSON client for Chrome Bridge.

    Requests match ``test_client.py`` framing:
    ``{"token": token, "action": action, "payload": payload, "tabId": tab_id?}``
    sent as one UTF-8 JSON line to ``127.0.0.1:BRIDGE_PORT``. Responses are one
    JSON line with ``success`` and ``result`` fields.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        token_file: Optional[str] = None,
        connect_timeout: float = 15.0,
        read_timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = int(port if port is not None else os.environ.get("BRIDGE_PORT", "9223"))
        self.token_file = Path(
            token_file
            or os.environ.get("BRIDGE_TOKEN_FILE", "bridge_token.txt")
        ).expanduser()
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._token = self._load_token()
        self._socket: Optional[socket.socket] = None
        self._buffer = b""

    def __enter__(self) -> "ChromeBridgeSession":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        """Open the TCP socket if it is not already open."""
        if self._socket is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.connect_timeout)
            sock.connect((self.host, self.port))
            sock.settimeout(self.read_timeout)
        except OSError:
            sock.close()
            raise
        self._socket = sock

    def close(self) -> None:
        """Close the persistent TCP socket."""
        if self._socket is None:
            return
        try:
            self._socket.close()
        finally:
            self._socket = None
            self._buffer = b""

    def navigate(self, url: str) -> Any:
        return self.request("navigate", {"url": url})

    def get_tabs(self) -> List[JsonDict]:
        result = self.request("getTabs")
        if not isinstance(result, list):
            raise ChromeBridgeError("getTabs response was not a list")
        return result

    def get_state(self, tab_id: Optional[int] = None) -> Any:
        resolved_tab_id = self._resolve_tab_id(tab_id)
        try:
            return self.request("observe", {"tabId": resolved_tab_id})
        except ChromeBridgeError:
            return self.request("getCurrentState", {"tabId": resolved_tab_id})

    def click(self, selector: str, tab_id: Optional[int] = None) -> Any:
        return self.request("click", {"tabId": self._resolve_tab_id(tab_id), "selector": selector})

    def type_text(self, selector: str, text: str, tab_id: Optional[int] = None) -> Any:
        return self.request(
            "type",
            {"tabId": self._resolve_tab_id(tab_id), "selector": selector, "text": text},
        )

    def fill(self, selector: str, text: str, tab_id: Optional[int] = None) -> Any:
        return self.request(
            "fill",
            {"tabId": self._resolve_tab_id(tab_id), "selector": selector, "text": text},
        )

    def extract_text(self, tab_id: Optional[int] = None, max_chars: int = 20000) -> str:
        result = self.request(
            "extractText",
            {"tabId": self._resolve_tab_id(tab_id), "maxChars": max_chars},
        )
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            text = result.get("text")
            if isinstance(text, str):
                return text
        raise ChromeBridgeError("extractText response did not include text")

    def screenshot(self, path: str, tab_id: Optional[int] = None) -> str:
        result = self.request(
            "screenshot",
            {"tabId": self._resolve_tab_id(tab_id), "format": "png"},
        )
        data_url = result.get("dataUrl") if isinstance(result, dict) else None
        prefix = "data:image/png;base64,"
        if not isinstance(data_url, str) or not data_url.startswith(prefix):
            raise ChromeBridgeError("screenshot response did not include PNG dataUrl")
        output_path = Path(path).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(data_url[len(prefix):]))
        return str(output_path)

    def wait_for_selector(
        self,
        selector: str,
        timeout_ms: int = 10000,
        tab_id: Optional[int] = None,
    ) -> Any:
        return self.request(
            "waitForSelector",
            {
                "tabId": self._resolve_tab_id(tab_id),
                "selector": selector,
                "timeoutMs": timeout_ms,
            },
            read_timeout_ms=timeout_ms,
        )

    def wait_for_handoff(
        self,
        message: str,
        mode: str = "manual",
        arg: Optional[str] = None,
        timeout_ms: int = 120000,
        tab_id: Optional[int] = None,
    ) -> Any:
        until: JsonDict = {"mode": mode}
        if mode == "selector" and arg is not None:
            until["selector"] = arg
        elif mode == "url" and arg is not None:
            until["urlSubstring"] = arg
        elif mode == "text" and arg is not None:
            until["text"] = arg
        payload: JsonDict = {"message": message, "until": until, "timeoutMs": timeout_ms}
        payload["tabId"] = self._resolve_tab_id(tab_id)
        return self.request("waitForHandoff", payload, read_timeout_ms=timeout_ms)

    def session_status(self, *domains: str) -> Any:
        if not domains:
            raise ChromeBridgeError("session_status requires at least one domain")
        return self.request("sessionStatus", {"domains": list(domains)})

    def request(
        self,
        action: str,
        payload: Optional[JsonDict] = None,
        tab_id: Optional[int] = None,
        read_timeout_ms: Optional[int] = None,
    ) -> Any:
        if payload is None:
            payload = {}
        command: JsonDict = {"token": self._token, "action": action, "payload": payload}
        if tab_id is not None:
            command["tabId"] = tab_id
        response = self._send_json_line(command, read_timeout_ms=read_timeout_ms)
        if response.get("success") is not True:
            raise ChromeBridgeError(self._response_error(response))
        result = response.get("result")
        if isinstance(result, dict) and result.get("success") is False:
            raise ChromeBridgeError(self._response_error(response))
        return result

    def _send_json_line(self, command: JsonDict, read_timeout_ms: Optional[int] = None) -> JsonDict:
        self.connect()
        assert self._socket is not None
        old_timeout = self._socket.gettimeout()
        if read_timeout_ms is not None:
            self._socket.settimeout(max(self.read_timeout, read_timeout_ms / 1000 + 10))
        try:
            self._socket.sendall((json.dumps(command) + "\n").encode("utf-8"))
            line = self._read_line()
        except OSError as exc:
            self.close()
            raise ChromeBridgeError("Error communicating with bridge: %s" % exc) from exc
        finally:
            if self._socket is not None and read_timeout_ms is not None:
                self._socket.settimeout(old_timeout)
        if not line:
            raise ChromeBridgeError("Received empty response from bridge.")
        try:
            response = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise ChromeBridgeError("Bridge returned invalid JSON: %s" % exc) from exc
        if not isinstance(response, dict):
            raise ChromeBridgeError("Bridge response was not a JSON object")
        return response

    def _read_line(self) -> bytes:
        assert self._socket is not None
        while b"\n" not in self._buffer:
            chunk = self._socket.recv(65536)
            if not chunk:
                line, self._buffer = self._buffer, b""
                return line.strip()
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.strip()

    def _resolve_tab_id(self, tab_id: Optional[int]) -> int:
        if tab_id is not None:
            return tab_id
        tabs = self.get_tabs()
        active = self._first_tab(tabs, active=True) or self._first_tab(tabs, active=False)
        if active is None:
            raise ChromeBridgeError("No tabs available")
        raw_tab_id = active.get("id")
        if not isinstance(raw_tab_id, int):
            raise ChromeBridgeError("Active tab did not include an integer id")
        return raw_tab_id

    @staticmethod
    def _first_tab(tabs: Iterable[JsonDict], active: bool) -> Optional[JsonDict]:
        for tab in tabs:
            if isinstance(tab, dict) and (tab.get("active") is True) == active:
                return tab
        return None

    def _load_token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ChromeBridgeError(
                "could not read bridge token from %s" % self.token_file
            ) from exc
        if not token:
            raise ChromeBridgeError("bridge token file was empty: %s" % self.token_file)
        return token

    @staticmethod
    def _response_error(response: JsonDict) -> str:
        for candidate in (response.get("error"), response.get("message")):
            if isinstance(candidate, str) and candidate:
                return candidate
        result = response.get("result")
        if isinstance(result, dict):
            for key in ("error", "message", "reason"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    return value
        return json.dumps(response, sort_keys=True)
