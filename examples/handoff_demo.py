#!/usr/bin/env python3
"""Narrated Chrome Bridge handoff demo.

This stdlib-only script is designed for screen recording. It intentionally prints
only redacted summaries: no cookie values, no raw page text, and no page HTML.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_URL = "https://github.com/login"
DEFAULT_WAIT_FOR = "github.com"
DEFAULT_PORT = 9223
DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_SCREENSHOT = Path("/tmp/handoff_demo.png")
HANDOFF_MESSAGE = "Please complete login/2FA - the agent will resume automatically"


class BridgeError(Exception):
    """Bridge request failed with a host-provided or transport error."""


class BridgeClient:
    def __init__(self, port: int, token_file: Path):
        self.port = port
        self.token = self._read_token(token_file)

    @staticmethod
    def _read_token(token_file: Path) -> str:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BridgeError(f"could not read bridge token from {token_file}: {exc}") from exc
        if not token:
            raise BridgeError(f"bridge token file is empty: {token_file}")
        return token

    def request(
        self,
        action: str,
        payload: dict | None = None,
        *,
        tab_id: int | None = None,
        read_timeout_ms: int | None = None,
    ) -> dict:
        command = {"token": self.token, "action": action, "payload": payload or {}}
        if tab_id is not None:
            command["tabId"] = tab_id

        read_timeout_seconds = 15.0
        if read_timeout_ms is not None:
            read_timeout_seconds = max(15.0, read_timeout_ms / 1000.0 + 30.0)

        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=15.0) as sock:
                sock.settimeout(read_timeout_seconds)
                sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
                raw = self._read_line(sock)
        except socket.timeout as exc:
            raise BridgeError("timed out waiting for the bridge response") from exc
        except OSError as exc:
            raise BridgeError(f"could not connect to bridge on 127.0.0.1:{self.port}: {exc}") from exc

        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(f"invalid JSON response from bridge: {exc}") from exc

        if response.get("success") is not True:
            raise BridgeError(bridge_error_message(response))
        result = response.get("result")
        if isinstance(result, dict) and result.get("success") is False:
            raise BridgeError(bridge_error_message(response))
        return response

    @staticmethod
    def _read_line(sock: socket.socket) -> bytes:
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
        if not buffer.strip():
            raise BridgeError("received empty response from bridge")
        return buffer.split(b"\n", 1)[0]


def bridge_error_message(response: dict) -> str:
    result = response.get("result")
    candidates = []
    if isinstance(result, dict):
        candidates.extend([result.get("err"), result.get("error"), result.get("message"), result.get("reason")])
    candidates.extend([response.get("error"), response.get("message")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return json.dumps(response, sort_keys=True)


def step(number: int, title: str) -> None:
    print(f"\n=== Step {number}: {title} ===", flush=True)


def pause(seconds: float = 1.25) -> None:
    time.sleep(seconds)


def result_payload(response: dict) -> dict:
    result = response.get("result")
    return result if isinstance(result, dict) else response


def domain_from_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split("/", 1)[0] or DEFAULT_WAIT_FOR


def compact_session_status(response: dict, domain: str) -> tuple[bool, int]:
    result = result_payload(response)
    entries = result.get("sessions") or result.get("statuses") or result.get("domains") or result.get("results")
    status = None
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("domain") == domain:
                status = entry
                break
        if status is None and entries and isinstance(entries[0], dict):
            status = entries[0]
    elif isinstance(entries, dict):
        value = entries.get(domain)
        status = value if isinstance(value, dict) else entries
    elif domain in result and isinstance(result[domain], dict):
        status = result[domain]
    else:
        status = result

    logged_in = bool(status.get("loggedIn")) if isinstance(status, dict) else False
    cookie_count = 0
    if isinstance(status, dict):
        raw_count = status.get("cookieCount")
        if isinstance(raw_count, int):
            cookie_count = raw_count
        else:
            cookies = status.get("cookies") or status.get("cookieNames")
            if isinstance(cookies, list):
                cookie_count = len(cookies)
    return logged_in, cookie_count


def extract_tab_id(response: dict) -> int:
    result = result_payload(response)
    candidates = [result.get("tabId"), result.get("id")]
    tab = result.get("tab")
    if isinstance(tab, dict):
        candidates.extend([tab.get("id"), tab.get("tabId")])
    tabs = result.get("tabs")
    if isinstance(tabs, list) and tabs and isinstance(tabs[0], dict):
        candidates.extend([tabs[0].get("id"), tabs[0].get("tabId")])
    for candidate in candidates:
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    raise BridgeError("navigate response did not include a tab id")


def text_length(response: dict) -> int:
    result = result_payload(response)
    text = result.get("text") if isinstance(result, dict) else None
    return len(text) if isinstance(text, str) else 0


def save_screenshot(response: dict, output_path: Path) -> int:
    result = result_payload(response)
    data_url = result.get("dataUrl") if isinstance(result, dict) else None
    prefix = "data:image/png;base64,"
    if not isinstance(data_url, str) or not data_url.startswith(prefix):
        raise BridgeError("screenshot response did not include a PNG dataUrl")
    data = base64.b64decode(data_url[len(prefix) :])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return len(data)


def parse_args(argv: list[str]) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Run a narrated Chrome Bridge human-handoff demo without printing secrets."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"login URL to open (default: {DEFAULT_URL})")
    parser.add_argument(
        "--wait-for",
        default=DEFAULT_WAIT_FOR,
        help=f"post-login URL substring for waitForHandoff mode=url (default: {DEFAULT_WAIT_FOR})",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bridge TCP port (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=repo_root / "bridge_token.txt",
        help="path to bridge_token.txt (default: ./bridge_token.txt)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help=f"handoff timeout in milliseconds (default: {DEFAULT_TIMEOUT_MS})",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    domain = domain_from_url(args.url)

    try:
        client = BridgeClient(args.port, args.token_file.expanduser())

        step(1, f"Check real-profile session for {domain}")
        status = client.request("sessionStatus", {"domains": [domain]})
        logged_in, cookie_count = compact_session_status(status, domain)
        print(f"Redacted session summary: loggedIn={logged_in}, cookieCount={cookie_count}", flush=True)
        pause()

        step(2, f"Open {args.url}")
        navigate = client.request("navigate", {"url": args.url})
        tab_id = extract_tab_id(navigate)
        print(f"Chrome tab ready: tabId={tab_id}", flush=True)
        if args.wait_for in args.url:
            print(
                "Recording warning: --wait-for already appears in --url; "
                "use a post-login-only URL substring for a visible pause.",
                flush=True,
            )
            pause()
        pause()

        step(3, "Hand control to the human for login or 2FA")
        print(HANDOFF_MESSAGE, flush=True)
        client.request(
            "waitForHandoff",
            {
                "message": HANDOFF_MESSAGE,
                "until": {"mode": "url", "urlSubstring": args.wait_for},
                "timeoutMs": args.timeout_ms,
                "tabId": tab_id,
            },
            read_timeout_ms=args.timeout_ms,
        )
        print("Handoff complete. Agent resumed automatically.", flush=True)
        pause()

        step(4, "Confirm resumed page without exposing content")
        text = client.request("extractText", {"tabId": tab_id, "maxChars": 2000})
        screenshot = client.request("screenshot", {"tabId": tab_id, "format": "png", "quiet": True})
        screenshot_bytes = save_screenshot(screenshot, DEFAULT_SCREENSHOT)
        print(
            f"Redacted confirmation: extractedTextChars={text_length(text)}, "
            f"screenshot={DEFAULT_SCREENSHOT} ({screenshot_bytes} bytes)",
            flush=True,
        )
        pause()

        step(5, "Demo complete")
        print("Demo complete", flush=True)
        return 0
    except BridgeError as exc:
        print(f"Bridge error: {exc}", file=sys.stderr)
        print("Hint: if this is a policy denial, run `python3 test_client.py policy doctor`.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
