#!/usr/bin/env python3
import base64
import json
import os
import re
import socket
import sys
import time
from bridge_wake import bridge_extension_id, token_file_path, wake_bridge_extension

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def load_token():
    token_file = token_file_path(SCRIPT_DIR)
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
    wake_attempted = False

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
                if not wake_attempted:
                    wake_bridge_extension(SCRIPT_DIR)
                    wake_attempted = True
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


def save_screenshot(tab_id, output_path, quiet=False):
    payload = {"tabId": tab_id, "format": "png"}
    if quiet:
        payload["quiet"] = True
    exit_code, response, stderr = send_command_data("screenshot", payload)
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

def _policy_paths():
    # Ask the host for the authoritative policy/audit file paths. The host is the
    # only component that knows BRIDGE_POLICY_FILE as the running host saw it, so
    # never assume a repo-local path here.
    exit_code, response, stderr = send_command_data("policyInfo")
    if exit_code != 0 or not response:
        print(stderr or "Error: could not reach host for policyInfo", file=sys.stderr)
        return None
    return result_payload(response)


def _load_policy_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        print(f"Error: policy file at {path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def _write_policy_file(path, policy):
    # Persist with mode 600: the policy governs which origins automation may
    # touch, so it must not be world-readable.
    encoded = json.dumps(policy, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        os.write(fd, encoded.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
def _restrict_policy_file_perms(path):
    if not os.path.exists(path):
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
def _policy_section(policy, client, explicit):
    # Return (container, key) for the list-bearing section governing ``client``.
    # An explicitly-named client always edits clients.<name> (created if absent)
    # so naming a new client never silently broadens the shared default policy.
    # The host-reported inherited client edits clients.<name> only when that
    # layer already exists, else default.
    if explicit and client:
        clients = policy.setdefault("clients", {})
        return clients, client
    if client and isinstance(policy.get("clients"), dict) and isinstance(policy["clients"].get(client), dict):
        return policy["clients"], client
    policy.setdefault("default", {})
    return policy, "default"


# Built-in fail-closed defaults, mirrored from the host (bridge.py DEFAULT_POLICY
# / host-rs default_policy). Used only to seed a newly-created list so an "allow"
# never silently drops inherited grants -- the host remains the source of truth.
_BUILTIN_DEFAULT_LISTS = {
    "allowedActions": ["ping", "policyCheck", "policyInfo", "lease", "release", "leaseStatus"],
    "deniedActions": [],
    "allowedOrigins": [],
    "deniedOrigins": [
        "file://*", "chrome://*", "chrome-extension://*",
        "*://localhost", "*://localhost:*",
        "*://127.0.0.1", "*://127.0.0.1:*",
        "*://0.0.0.0", "*://0.0.0.0:*",
        "*://*.local", "*://*.local:*",
        "*://[[]::1[]]", "*://[[]::1[]]:*",
    ],
}


def _effective_inherited_list(policy, container, key, list_key):
    # The list the host would resolve for this section BEFORE our edit, following
    # its merge order: built-in default -> default.<list> -> clients.<name>.<list>.
    # Editing default inherits only the built-in; editing clients.<name> inherits
    # the default layer (its own list when present, else built-in).
    base = list(_BUILTIN_DEFAULT_LISTS.get(list_key, []))
    if key != "default":
        default_list = (policy.get("default") or {}).get(list_key)
        if isinstance(default_list, list):
            base = list(default_list)
    return base


def _policy_add_to_list(policy, client, list_key, value, explicit):
    container, key = _policy_section(policy, client, explicit)
    section = container.setdefault(key, {})
    if list_key not in section:
        # Seed a new list from the inherited effective list so appending one
        # grant does not replace (and thus revoke) everything inherited.
        section[list_key] = _effective_inherited_list(policy, container, key, list_key)
    lst = section[list_key]
    if value in lst:
        return False
    lst.append(value)
    return True


def cmd_policy(args):
    sub = args[2] if len(args) > 2 else ""
    if sub == "info":
        return send_command("policyInfo")
    info = _policy_paths()
    if info is None:
        return 1
    policy_file = info.get("policyFile")
    audit_file = info.get("auditLogFile")
    client = info.get("client")
    if sub == "show":
        policy = _load_policy_file(policy_file)
        if policy is None:
            print(json.dumps({"policyFile": policy_file, "exists": False,
                              "note": "No policy file; built-in fail-closed default is active."}, indent=2))
            return 0
        print(json.dumps({"policyFile": policy_file, "exists": True, "policy": policy}, indent=2))
        return 0
    if sub == "doctor":
        return _policy_doctor(audit_file, policy_file)
    if sub == "allow-action":
        require_args(args, 4, "Usage: python3 test_client.py policy allow-action <action> [client]")
        explicit = len(args) > 4
        target_client = args[4] if explicit else client
        policy = _load_policy_file(policy_file) or {}
        changed = _policy_add_to_list(policy, target_client, "allowedActions", args[3], explicit)
        if changed:
            _write_policy_file(policy_file, policy)
        else:
            _restrict_policy_file_perms(policy_file)
        print(json.dumps({"success": True, "changed": changed, "action": args[3],
                          "policyFile": policy_file}, indent=2))
        return 0
    if sub == "allow-origin":
        require_args(args, 4, "Usage: python3 test_client.py policy allow-origin <pattern> [client]")
        explicit = len(args) > 4
        target_client = args[4] if explicit else client
        policy = _load_policy_file(policy_file) or {}
        changed = _policy_add_to_list(policy, target_client, "allowedOrigins", args[3], explicit)
        if changed:
            _write_policy_file(policy_file, policy)
        else:
            _restrict_policy_file_perms(policy_file)
        print(json.dumps({"success": True, "changed": changed, "origin": args[3],
                          "policyFile": policy_file}, indent=2))
        return 0
    print("Usage: python3 test_client.py policy <info|show|doctor|allow-action|allow-origin> ...", file=sys.stderr)
    return 64


def _policy_doctor(audit_file, policy_file):
    # Read recent deny entries from the audit log and propose the precise grant
    # for each distinct (action, target) so the user can self-service. Reads only
    # paths/metadata the host already disclosed; never forwards anything.
    denials = []
    try:
        with open(audit_file) as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(json.dumps({"policyFile": policy_file, "denials": [],
                          "note": "No audit log yet; nothing to diagnose."}, indent=2))
        return 0
    seen = set()
    for line in reversed(lines[-500:]):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("decision") != "deny":
            continue
        reason = ev.get("reason") or ""
        action = ev.get("action") or ""
        targets = ev.get("targets") or []
        # A denied batch step is audited as "batch step N: <inner reason>" with
        # the outer action "batch"; strip the wrapper so the inner reason gets the
        # same fix hint, and surface the step index. Mirrors host policy_denial.
        batch_step = None
        bm = re.match(r"^batch step (\d+): (.*)$", reason)
        if bm:
            batch_step = int(bm.group(1))
            reason = bm.group(2)
        key = (action, reason, tuple(targets), batch_step)
        if key in seen:
            continue
        seen.add(key)
        # Deny-lists win over allow-lists in the host, so the fix differs by the
        # gate that fired: "not allowed" means the item is missing from an allow
        # list (grant it); "denied" means a deny-list pattern matched (the user
        # must remove/narrow it -- a grant would not help).
        suggestion = None
        am = re.match(r"^action (\S+) (?:not allowed|denied)$", reason)
        if am and reason.endswith("not allowed"):
            suggestion = {"cli": f"policy allow-action {am.group(1)}"}
        elif am and reason.endswith("denied"):
            suggestion = {"manual": f"Remove or narrow the deniedActions pattern matching '{am.group(1)}' in {policy_file}"}
        elif reason == "target not allowed" and targets:
            suggestion = {"cli": f"policy allow-origin '{targets[0]}'"}
        elif reason == "target denied" and targets:
            suggestion = {"manual": f"Remove or narrow the deniedOrigins pattern matching '{targets[0]}' in {policy_file}"}
        denials.append({"action": action, "reason": reason, "targets": targets,
                        "batchStep": batch_step, "suggestion": suggestion})
    print(json.dumps({"policyFile": policy_file, "auditLogFile": audit_file,
                      "denials": denials}, indent=2))
    return 0


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
    print("  python3 test_client.py policy info")
    print("  python3 test_client.py policy show")
    print("  python3 test_client.py policy doctor")
    print("  python3 test_client.py policy allow-action <action> [client]")
    print("  python3 test_client.py policy allow-origin <pattern> [client]")

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
        payload = {"url": args[2]}
        if len(args) > 3 and args[3] == "--background":
            payload["active"] = False
        sys.exit(send_command("navigate", payload))
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
        require_args(args, 4, "Usage: python3 test_client.py screenshot <tabId> <outputPath> [--quiet]")
        sys.exit(save_screenshot(parse_int(args[2], "tabId"), args[3], len(args) > 4 and args[4] == "--quiet"))
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
    elif action == "policy":
        sys.exit(cmd_policy(args))
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(64)


if __name__ == '__main__':
    main()
