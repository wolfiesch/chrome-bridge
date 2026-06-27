#!/usr/bin/env python3
import base64
import json
import os
import socket
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def load_token():
    token_file = os.environ.get(
        'BRIDGE_TOKEN_FILE', os.path.join(SCRIPT_DIR, 'bridge_token.txt'))
    try:
        with open(token_file) as f:
            return f.read().strip()
    except Exception:
        return None


def parse_int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        print(f"Invalid {name}: {value}", file=sys.stderr)
        sys.exit(2)


def parse_float(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        print(f"Invalid {name}: {value}", file=sys.stderr)
        sys.exit(2)


def parse_timeout(args, index, default=10000):
    if len(args) > index:
        return parse_int(args[index], "timeoutMs")
    return default


def expand_existing_files(paths):
    expanded = []
    for path in paths:
        abs_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(abs_path):
            print(f"Upload file not found: {abs_path}", file=sys.stderr)
            sys.exit(2)
        expanded.append(abs_path)
    return expanded


def expand_output_path(path):
    return os.path.abspath(os.path.expanduser(path))


def send_command_data(action, payload=None, read_timeout_ms=None, confirmation_token=None):
    if payload is None:
        payload = {}

    # Any action carrying a payload ``timeoutMs`` (waits, human handoff) may run
    # longer than the default 15s socket read; derive the read timeout from it
    # unless the caller passed one explicitly, mirroring the host-side per-request
    # timeout so no layer times out before the extension legitimately finishes.
    if read_timeout_ms is None and isinstance(payload, dict):
        pt = payload.get("timeoutMs")
        if isinstance(pt, (int, float)) and pt > 0:
            read_timeout_ms = pt

    token = load_token()
    if not token:
        return 2, None, "Error: could not read bridge token. Is bridge_token.txt present?"

    port = int(os.environ.get('BRIDGE_PORT', 9223))
    retry_seconds = float(os.environ.get('BRIDGE_CONNECT_TIMEOUT_SECONDS', 45))
    deadline = time.monotonic() + retry_seconds
    sock = None

    try:
        while True:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(15)
                sock.connect(('127.0.0.1', port))
                # Connect uses a short timeout; the post-connect read can be much
                # longer (e.g. human-handoff waits), with headroom over the
                # extension-side deadline so the wire never times out first.
                if read_timeout_ms is not None:
                    sock.settimeout(max(15, read_timeout_ms / 1000 + 10))
                break
            except ConnectionRefusedError:
                try:
                    sock.close()
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

        cmd = {
            "action": action,
            "payload": payload,
            "token": token
        }
        if isinstance(confirmation_token, str) and confirmation_token:
            cmd["confirmationToken"] = confirmation_token
        sock.sendall((json.dumps(cmd) + "\n").encode('utf-8'))

        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
        if buffer.strip():
            response = json.loads(buffer.split(b"\n", 1)[0].decode('utf-8'))
            exit_code = 0
            if response.get("success") is not True:
                exit_code = 1
            result = response.get("result")
            if isinstance(result, dict) and result.get("success") is False:
                exit_code = 1
            return exit_code, response, ""
        return 1, None, "Received empty response from bridge."
    except socket.timeout:
        return 124, None, (
            "Error: timed out waiting for the extension to respond. "
            "Is the extension's service worker active? Open chrome://extensions, "
            f"click 'service worker' to wake it, then check {os.path.join(SCRIPT_DIR, 'bridge_debug.log')}."
        )
    except ConnectionRefusedError:
        return 111, None, "Error: Connection refused. Is Google Chrome running with the loaded extension?"
    except Exception as e:
        return 1, None, f"Error communicating with bridge: {e}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def send_command(action, payload=None, read_timeout_ms=None, confirmation_token=None):
    exit_code, response, stderr = send_command_data(action, payload, read_timeout_ms, confirmation_token)
    if response is not None:
        print(json.dumps(response, indent=2))
    if stderr:
        print(stderr, file=sys.stderr)
    return exit_code


def result_payload(response):
    if not response:
        return None
    result = response.get("result")
    return result if isinstance(result, dict) else response


def save_screenshot(tab_id, output_path):
    exit_code, response, stderr = send_command_data("screenshot", {"tabId": tab_id, "format": "png"})
    if exit_code != 0:
        if response is not None:
            print(json.dumps(response, indent=2))
        if stderr:
            print(stderr, file=sys.stderr)
        return exit_code
    result = result_payload(response)
    data_url = result.get("dataUrl", "") if result else ""
    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        print("Error: screenshot response did not include PNG dataUrl", file=sys.stderr)
        return 1
    data = base64.b64decode(data_url[len(prefix):])
    path = expand_output_path(output_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(json.dumps({"success": True, "path": path, "mimeType": "image/png", "bytes": len(data)}, indent=2))
    return 0


def save_html(tab_id, output_path):
    exit_code, response, stderr = send_command_data("getHTML", {"tabId": tab_id})
    if exit_code != 0:
        if response is not None:
            print(json.dumps(response, indent=2))
        if stderr:
            print(stderr, file=sys.stderr)
        return exit_code
    result = result_payload(response)
    html = result.get("html") if result else None
    if not isinstance(html, str):
        print("Error: getHTML response did not include html", file=sys.stderr)
        return 1
    encoded = html.encode("utf-8")
    path = expand_output_path(output_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(encoded)
    print(json.dumps({"success": True, "path": path, "bytes": len(encoded)}, indent=2))
    return 0


def save_storage_state(tab_id, output_path):
    exit_code, response, stderr = send_command_data("storageState", {"tabId": tab_id})
    if exit_code != 0:
        if response is not None:
            print(json.dumps(response, indent=2))
        if stderr:
            print(stderr, file=sys.stderr)
        return exit_code
    result = result_payload(response)
    if not isinstance(result, dict):
        print("Error: storageState response was empty or invalid", file=sys.stderr)
        return 1
    encoded = json.dumps(result, indent=2).encode("utf-8")
    path = expand_output_path(output_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(encoded)
    origin = result.get("origin")
    cookie_count = len(result.get("cookies", []))
    ls_origins = [origin] if (origin and result.get("localStorage")) else []
    ss_origins = [origin] if (origin and result.get("sessionStorage")) else []
    out = {
        "success": True,
        "path": path,
        "bytes": len(encoded),
        "cookieCount": cookie_count,
        "localStorageOrigins": ls_origins,
        "sessionStorageOrigins": ss_origins
    }
    print(json.dumps(out, indent=2))
    return 0


def require_args(argv, count, usage):
    if len(argv) < count:
        print(usage, file=sys.stderr)
        sys.exit(1)


def print_usage():
    print("Usage:")
    print("  python3 test_client.py ping")
    print("  python3 test_client.py navigate <url>")
    print("  python3 test_client.py getTabs")
    print("  python3 test_client.py getCookies <domain>")
    print("  python3 test_client.py executeScript <tabId> <code>")
    print("  python3 test_client.py executeScriptCDP <tabId> <code>")
    print("  python3 test_client.py click <tabId> <selector>")
    print("  python3 test_client.py type <tabId> <selector> <text>")
    print("  python3 test_client.py observe <tabId>")
    print("  python3 test_client.py activateTab <tabId>")
    print("  python3 test_client.py closeTab <tabId>")
    print("  python3 test_client.py reload <tabId>")
    print("  python3 test_client.py goBack <tabId>")
    print("  python3 test_client.py goForward <tabId>")
    print("  python3 test_client.py waitForLoad <tabId> [timeoutMs]")
    print("  python3 test_client.py waitForSelector <tabId> <selector> [timeoutMs]")
    print("  python3 test_client.py waitForText <tabId> <text> [timeoutMs]")
    print("  python3 test_client.py waitForUrl <tabId> <substring> [timeoutMs]")
    print("  python3 test_client.py getCurrentState <tabId>")
    print("  python3 test_client.py screenshot <tabId> <outputPath>")
    print("  python3 test_client.py extractText <tabId> [maxChars]")
    print("  python3 test_client.py getHTML <tabId> <outputPath>")
    print("  python3 test_client.py hover <tabId> <selector>")
    print("  python3 test_client.py scroll <tabId> <deltaX> <deltaY> [selector]")
    print("  python3 test_client.py press <tabId> <keySpec>")
    print("  python3 test_client.py drag <tabId> <fromSelector> <toSelector>")
    print("  python3 test_client.py fill <tabId> <selector> <text>")
    print("  python3 test_client.py select <tabId> <selector> <value>")
    print("  python3 test_client.py uploadFile <tabId> <selector> <path...>")
    print("    selectors: CSS, css=<selector>, label=<text>, text=<text>, role=<role>[name=<text>],")
    print("               <host> >>> <shadow-selector>, frame=<iframe-selector> >> <target-selector>")
    print("  python3 test_client.py setViewport <tabId> <width> <height> [deviceScaleFactor]")
    print("  python3 test_client.py setCpuThrottling <tabId> <rate>")
    print("  python3 test_client.py setNetworkConditions <tabId> <offline:0|1> [latencyMs] [downBps] [upBps]")
    print("  python3 test_client.py clearNetworkConditions <tabId>")
    print("  python3 test_client.py setColorScheme <tabId> light|dark|no-preference")
    print("  python3 test_client.py setUserAgent <tabId> <userAgent...>")
    print("  python3 test_client.py startMonitoring <tabId>")
    print("  python3 test_client.py stopMonitoring <tabId>")
    print("  python3 test_client.py consoleMessages <tabId>")
    print("  python3 test_client.py networkRequests <tabId>")
    print("  python3 test_client.py handleDialog <tabId> accept|dismiss [promptText]")
    print("  python3 test_client.py downloadUrl <url> [filename]")
    print("  python3 test_client.py storageState <tabId> <outputPath>")
    print("  python3 test_client.py setGeolocation <tabId> <latitude> <longitude> [accuracy]")
    print("  python3 test_client.py clearGeolocation <tabId>")
    print("  python3 test_client.py startInterception <tabId> <urlPattern> continue|abort|fulfill [status] [body]")
    print("  python3 test_client.py stopInterception <tabId>")
    print("  python3 test_client.py interceptedRequests <tabId>")
    print("  python3 test_client.py performanceMetrics <tabId>")
    print("  python3 test_client.py sessionStatus <domain> [<domain> ...]")
    print("  python3 test_client.py waitForHandoff <message> [mode] [selectorOrUrlOrText] [timeoutMs] [tabId]")
    print("  python3 test_client.py policyCheck <action> [payloadJson]")
    print("  python3 test_client.py confirm <action> <confirmationToken> <payloadJson>")

def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    action = sys.argv[1]
    args = sys.argv

    if action == "ping":
        sys.exit(send_command("ping"))
    elif action == "navigate":
        require_args(args, 3, "Missing URL.")
        sys.exit(send_command("navigate", {"url": args[2]}))
    elif action == "getTabs":
        sys.exit(send_command("getTabs"))
    elif action == "getCookies":
        require_args(args, 3, "Missing domain.")
        sys.exit(send_command("getCookies", {"domain": args[2]}))
    elif action == "executeScript":
        require_args(args, 4, "Usage: python3 test_client.py executeScript <tabId> <code>")
        sys.exit(send_command("executeScript", {"tabId": parse_int(args[2], "tabId"), "code": args[3]}))
    elif action == "executeScriptCDP":
        require_args(args, 4, "Usage: python3 test_client.py executeScriptCDP <tabId> <code>")
        sys.exit(send_command("executeScriptCDP", {"tabId": parse_int(args[2], "tabId"), "code": args[3]}))
    elif action == "click":
        require_args(args, 4, "Usage: python3 test_client.py click <tabId> <selector>")
        sys.exit(send_command("click", {"tabId": parse_int(args[2], "tabId"), "selector": args[3]}))
    elif action == "type":
        require_args(args, 5, "Usage: python3 test_client.py type <tabId> <selector> <text>")
        sys.exit(send_command("type", {"tabId": parse_int(args[2], "tabId"), "selector": args[3], "text": args[4]}))
    elif action == "observe":
        require_args(args, 3, "Usage: python3 test_client.py observe <tabId>")
        sys.exit(send_command("observe", {"tabId": parse_int(args[2], "tabId")}))
    elif action in {"activateTab", "closeTab", "reload", "goBack", "goForward", "getCurrentState", "startMonitoring", "stopMonitoring", "consoleMessages", "networkRequests"}:
        require_args(args, 3, f"Usage: python3 test_client.py {action} <tabId>")
        sys.exit(send_command(action, {"tabId": parse_int(args[2], "tabId")}))
    elif action == "waitForLoad":
        require_args(args, 3, "Usage: python3 test_client.py waitForLoad <tabId> [timeoutMs]")
        sys.exit(send_command("waitForLoad", {"tabId": parse_int(args[2], "tabId"), "timeoutMs": parse_timeout(args, 3)}))
    elif action == "waitForSelector":
        require_args(args, 4, "Usage: python3 test_client.py waitForSelector <tabId> <selector> [timeoutMs]")
        sys.exit(send_command("waitForSelector", {"tabId": parse_int(args[2], "tabId"), "selector": args[3], "timeoutMs": parse_timeout(args, 4)}))
    elif action == "waitForText":
        require_args(args, 4, "Usage: python3 test_client.py waitForText <tabId> <text> [timeoutMs]")
        sys.exit(send_command("waitForText", {"tabId": parse_int(args[2], "tabId"), "text": args[3], "timeoutMs": parse_timeout(args, 4)}))
    elif action == "waitForUrl":
        require_args(args, 4, "Usage: python3 test_client.py waitForUrl <tabId> <substring> [timeoutMs]")
        sys.exit(send_command("waitForUrl", {"tabId": parse_int(args[2], "tabId"), "substring": args[3], "timeoutMs": parse_timeout(args, 4)}))
    elif action == "screenshot":
        require_args(args, 4, "Usage: python3 test_client.py screenshot <tabId> <outputPath>")
        sys.exit(save_screenshot(parse_int(args[2], "tabId"), args[3]))
    elif action == "extractText":
        require_args(args, 3, "Usage: python3 test_client.py extractText <tabId> [maxChars]")
        max_chars = parse_int(args[3], "maxChars") if len(args) > 3 else 20000
        sys.exit(send_command("extractText", {"tabId": parse_int(args[2], "tabId"), "maxChars": max_chars}))
    elif action == "getHTML":
        require_args(args, 4, "Usage: python3 test_client.py getHTML <tabId> <outputPath>")
        sys.exit(save_html(parse_int(args[2], "tabId"), args[3]))
    elif action == "hover":
        require_args(args, 4, "Usage: python3 test_client.py hover <tabId> <selector>")
        sys.exit(send_command("hover", {"tabId": parse_int(args[2], "tabId"), "selector": args[3]}))
    elif action == "scroll":
        require_args(args, 5, "Usage: python3 test_client.py scroll <tabId> <deltaX> <deltaY> [selector]")
        sys.exit(send_command("scroll", {
            "tabId": parse_int(args[2], "tabId"),
            "deltaX": parse_float(args[3], "deltaX"),
            "deltaY": parse_float(args[4], "deltaY"),
            "selector": args[5] if len(args) > 5 else None,
        }))
    elif action == "press":
        require_args(args, 4, "Usage: python3 test_client.py press <tabId> <keySpec>")
        sys.exit(send_command("press", {"tabId": parse_int(args[2], "tabId"), "key": args[3]}))
    elif action == "drag":
        require_args(args, 5, "Usage: python3 test_client.py drag <tabId> <fromSelector> <toSelector>")
        sys.exit(send_command("drag", {"tabId": parse_int(args[2], "tabId"), "fromSelector": args[3], "toSelector": args[4]}))
    elif action == "fill":
        require_args(args, 5, "Usage: python3 test_client.py fill <tabId> <selector> <text>")
        sys.exit(send_command("fill", {"tabId": parse_int(args[2], "tabId"), "selector": args[3], "text": args[4]}))
    elif action == "select":
        require_args(args, 5, "Usage: python3 test_client.py select <tabId> <selector> <value>")
        sys.exit(send_command("select", {"tabId": parse_int(args[2], "tabId"), "selector": args[3], "value": args[4]}))
    elif action == "uploadFile":
        require_args(args, 5, "Usage: python3 test_client.py uploadFile <tabId> <selector> <path...>")
        sys.exit(send_command("uploadFile", {"tabId": parse_int(args[2], "tabId"), "selector": args[3], "files": expand_existing_files(args[4:])}))
    elif action == "setViewport":
        require_args(args, 5, "Usage: python3 test_client.py setViewport <tabId> <width> <height> [deviceScaleFactor]")
        scale = parse_float(args[5], "deviceScaleFactor") if len(args) > 5 else 1
        sys.exit(send_command("setViewport", {
            "tabId": parse_int(args[2], "tabId"),
            "width": parse_int(args[3], "width"),
            "height": parse_int(args[4], "height"),
            "deviceScaleFactor": scale,
        }))
    elif action == "handleDialog":
        require_args(args, 4, "Usage: python3 test_client.py handleDialog <tabId> accept|dismiss [promptText]")
        if args[3] not in {"accept", "dismiss"}:
            print("Dialog action must be accept or dismiss", file=sys.stderr)
            sys.exit(2)
        sys.exit(send_command("handleDialog", {
            "tabId": parse_int(args[2], "tabId"),
            "accept": args[3] == "accept",
            "promptText": " ".join(args[4:]) if len(args) > 4 else None,
        }))
    elif action == "downloadUrl":
        require_args(args, 3, "Usage: python3 test_client.py downloadUrl <url> [filename]")
        payload = {"url": args[2]}
        if len(args) > 3:
            payload["filename"] = args[3]
        sys.exit(send_command("downloadUrl", payload))
    elif action == "storageState":
        require_args(args, 4, "Usage: python3 test_client.py storageState <tabId> <outputPath>")
        sys.exit(save_storage_state(parse_int(args[2], "tabId"), args[3]))
    elif action == "setGeolocation":
        require_args(args, 5, "Usage: python3 test_client.py setGeolocation <tabId> <latitude> <longitude> [accuracy]")
        accuracy = parse_float(args[5], "accuracy") if len(args) > 5 else None
        sys.exit(send_command("setGeolocation", {
            "tabId": parse_int(args[2], "tabId"),
            "latitude": parse_float(args[3], "latitude"),
            "longitude": parse_float(args[4], "longitude"),
            "accuracy": accuracy
        }))
    elif action == "clearGeolocation":
        require_args(args, 3, "Usage: python3 test_client.py clearGeolocation <tabId>")
        sys.exit(send_command("clearGeolocation", {"tabId": parse_int(args[2], "tabId")}))
    elif action == "startInterception":
        require_args(args, 5, "Usage: python3 test_client.py startInterception <tabId> <urlPattern> continue|abort|fulfill [status] [body]")
        tab_id = parse_int(args[2], "tabId")
        url_pattern = args[3]
        mode = args[4]
        if mode not in {"continue", "abort", "fulfill"}:
            print("Interception mode must be continue, abort, or fulfill", file=sys.stderr)
            sys.exit(2)
        status = None
        body = None
        if len(args) > 5:
            status = parse_int(args[5], "status")
        if len(args) > 6:
            body = " ".join(args[6:])
        sys.exit(send_command("startInterception", {
            "tabId": tab_id,
            "urlPattern": url_pattern,
            "mode": mode,
            "status": status,
            "body": body
        }))
    elif action in {"stopInterception", "interceptedRequests", "performanceMetrics"}:
        require_args(args, 3, f"Usage: python3 test_client.py {action} <tabId>")
        sys.exit(send_command(action, {"tabId": parse_int(args[2], "tabId")}))
    elif action == "batch":
        require_args(args, 3, "Usage: python3 test_client.py batch <stepsJson> [tabId]")
        try:
            steps = json.loads(args[2])
        except Exception as exc:
            print(f"Invalid steps JSON: {exc}", file=sys.stderr)
            sys.exit(2)
        payload = {"steps": steps}
        if len(args) > 3:
            payload["tabId"] = parse_int(args[3], "tabId")
        sys.exit(send_command("batch", payload))
    elif action == "confirm":
        require_args(args, 5, "Usage: python3 test_client.py confirm <action> <confirmationToken> <payloadJson>")
        try:
            payload = json.loads(args[4])
        except Exception as exc:
            print(f"Invalid payload JSON: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(payload, dict):
            print("payloadJson must be a JSON object", file=sys.stderr)
            sys.exit(2)
        sys.exit(send_command(args[2], payload, confirmation_token=args[3]))
    elif action == "policyCheck":
        require_args(args, 3, "Usage: python3 test_client.py policyCheck <action> [payloadJson]")
        target_payload = {}
        if len(args) > 3:
            try:
                target_payload = json.loads(args[3])
            except Exception as exc:
                print(f"Invalid payload JSON: {exc}", file=sys.stderr)
                sys.exit(2)
        sys.exit(send_command("policyCheck", {"action": args[2], "payload": target_payload}))
    elif action == "sessionStatus":
        require_args(args, 3, "Usage: python3 test_client.py sessionStatus <domain> [<domain> ...]")
        sys.exit(send_command("sessionStatus", {"domains": list(args[2:])}))
    elif action == "waitForHandoff":
        require_args(args, 3, "Usage: python3 test_client.py waitForHandoff <message> [mode] [selectorOrUrlOrText] [timeoutMs] [tabId]")
        message = args[2]
        mode = args[3] if len(args) > 3 else "manual"
        target = args[4] if len(args) > 4 else None
        timeoutMs = parse_int(args[5], "timeoutMs") if len(args) > 5 else 120000
        until = {"mode": mode}
        if mode == "selector" and target is not None:
            until["selector"] = target
        elif mode == "url" and target is not None:
            until["urlSubstring"] = target
        elif mode == "text" and target is not None:
            until["text"] = target
        payload = {"message": message, "until": until, "timeoutMs": timeoutMs}
        if len(args) > 6:
            payload["tabId"] = parse_int(args[6], "tabId")
        sys.exit(send_command("waitForHandoff", payload, read_timeout_ms=timeoutMs))
    elif action == "setCpuThrottling":
        require_args(args, 4, "Usage: python3 test_client.py setCpuThrottling <tabId> <rate>")
        sys.exit(send_command("setCpuThrottling", {
            "tabId": parse_int(args[2], "tabId"),
            "rate": parse_float(args[3], "rate"),
        }))
    elif action == "setNetworkConditions":
        require_args(args, 4, "Usage: python3 test_client.py setNetworkConditions <tabId> <offline:0|1> [latencyMs] [downBps] [upBps]")
        offline = args[3] in {"1", "true", "True"}
        latency = parse_float(args[4], "latency") if len(args) > 4 else 0
        down = parse_int(args[5], "downloadThroughput") if len(args) > 5 else -1
        up = parse_int(args[6], "uploadThroughput") if len(args) > 6 else -1
        sys.exit(send_command("setNetworkConditions", {
            "tabId": parse_int(args[2], "tabId"),
            "offline": offline,
            "latency": latency,
            "downloadThroughput": down,
            "uploadThroughput": up,
        }))
    elif action == "clearNetworkConditions":
        require_args(args, 3, "Usage: python3 test_client.py clearNetworkConditions <tabId>")
        sys.exit(send_command("clearNetworkConditions", {"tabId": parse_int(args[2], "tabId")}))
    elif action == "setColorScheme":
        require_args(args, 4, "Usage: python3 test_client.py setColorScheme <tabId> light|dark|no-preference")
        if args[3] not in {"light", "dark", "no-preference"}:
            print("Color scheme must be light, dark, or no-preference", file=sys.stderr)
            sys.exit(2)
        sys.exit(send_command("setColorScheme", {
            "tabId": parse_int(args[2], "tabId"),
            "scheme": args[3],
        }))
    elif action == "setUserAgent":
        require_args(args, 4, "Usage: python3 test_client.py setUserAgent <tabId> <userAgent...>")
        ua = " ".join(args[3:])
        sys.exit(send_command("setUserAgent", {
            "tabId": parse_int(args[2], "tabId"),
            "userAgent": ua,
        }))
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(64)


if __name__ == '__main__':
    main()
