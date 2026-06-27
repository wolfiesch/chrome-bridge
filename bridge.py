#!/usr/bin/env python3
import sys
import os
import struct
import json
import logging
import socket
import threading
import uuid
import queue
import time
import re
import fnmatch
from urllib.parse import urlparse

# Resolve paths relative to this script so the install is location-independent.
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

# Configure local logging
logging.basicConfig(
    filename=os.environ.get('BRIDGE_LOG_FILE', os.path.join(SCRIPT_DIR, 'bridge_debug.log')),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Maps in-flight request id -> queue.Queue the handler thread blocks on for
# the extension's response. The reader thread (main) routes responses here.
pending_requests = {}
requests_lock = threading.Lock()
stdout_lock = threading.Lock()

# Shared-secret auth: every TCP request must include this token. The file is
# created with 0600 perms next to this script; override path via BRIDGE_TOKEN_FILE.
TOKEN_FILE = os.environ.get(
    'BRIDGE_TOKEN_FILE', os.path.join(SCRIPT_DIR, 'bridge_token.txt'))
TOKENS_FILE = os.environ.get(
    'BRIDGE_TOKENS_FILE', os.path.join(SCRIPT_DIR, 'bridge_tokens.txt'))

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception as e:
        logging.error(f"Could not read token file {TOKEN_FILE}: {e}")
        return None

def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None

# Per-client token registry. The legacy single token (bridge_token.txt) is the
# `default` client; an optional name:token file adds named clients on top.
def load_token_registry():
    registry = {}
    legacy = load_token()
    if legacy:
        registry[legacy] = 'default'
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    name, sep, token = line.partition(':')
                    if not sep:
                        continue
                    name, token = name.strip(), token.strip()
                    if name and token:
                        registry[token] = name
        except Exception as e:
            logging.error(f"Could not read tokens file {TOKENS_FILE}: {e}")
    # Record the mtimes observed for both token-file paths (missing -> None).
    mtimes = {TOKEN_FILE: _file_mtime(TOKEN_FILE),
              TOKENS_FILE: _file_mtime(TOKENS_FILE)}
    return registry, mtimes

# Guards both the registry dict and the recorded mtimes; reloads happen under it.
_registry_lock = threading.Lock()
TOKEN_REGISTRY, _registry_mtimes = load_token_registry()

def resolve_client(token):
    # Resolve a token to its client name. On a miss, reload the registry only if
    # a token file's mtime changed (including absent->present) since last load.
    global TOKEN_REGISTRY, _registry_mtimes
    with _registry_lock:
        name = TOKEN_REGISTRY.get(token)
        if name is not None:
            return name
        changed = any(_file_mtime(path) != recorded
                      for path, recorded in _registry_mtimes.items())
        if changed:
            TOKEN_REGISTRY, _registry_mtimes = load_token_registry()
            name = TOKEN_REGISTRY.get(token)
        return name

# Cooperative leasing: at most one client holds an exclusive lease at a time.
# A live lease blocks other clients' non-lease actions with "leased by <owner>".
lease_lock = threading.Lock()
lease_state = {'owner': None, 'expires_at': None}

def now_ms():
    return int(time.time() * 1000)

# Keep-alive: a single TCP connection may carry many newline-delimited
# requests. Idle connections are closed after this many seconds so a
# persistent client can reconnect transparently.
SOCKET_IDLE_TIMEOUT = float(os.environ.get('BRIDGE_SOCKET_IDLE_TIMEOUT', 300))

def read_message():
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        logging.info("Extension disconnected (empty read).")
        sys.exit(0)
    message_length = struct.unpack('@I', raw_length)[0]
    message_data = sys.stdin.buffer.read(message_length).decode('utf-8')
    # Do not log payload bodies: responses can contain cookies/DOM secrets.
    logging.info(f"Read message from extension ({message_length} bytes)")
    return json.loads(message_data)

def write_message(message):
    encoded_message = json.dumps(message).encode('utf-8')
    logging.info(
        f"Forwarding to extension: id={message.get('id')} "
        f"action={message.get('action')} ({len(encoded_message)} bytes)")
    with stdout_lock:
        sys.stdout.buffer.write(struct.pack('@I', len(encoded_message)))
        sys.stdout.buffer.write(encoded_message)
        sys.stdout.buffer.flush()

def _lease_status_locked():
    # Caller holds lease_lock. Returns the live lease snapshot, clearing it if expired.
    owner = lease_state['owner']
    expires_at = lease_state['expires_at']
    if owner is not None and expires_at is not None and now_ms() >= expires_at:
        lease_state['owner'] = None
        lease_state['expires_at'] = None
        owner = None
        expires_at = None
    return owner, expires_at

def handle_lease_action(action, payload, name):
    # Compute the host-side response for lease/release/leaseStatus. Returns a
    # dict to send straight back to the client (never forwarded to the extension).
    with lease_lock:
        owner, expires_at = _lease_status_locked()
        if action == 'lease':
            if owner is not None and owner != name:
                return {"success": False, "error": f"leased by {owner}"}
            try:
                ttl_ms = int(payload.get('ttlMs', 300000))
            except (TypeError, ValueError):
                ttl_ms = 300000
            expires = now_ms() + ttl_ms
            lease_state['owner'] = name
            lease_state['expires_at'] = expires
            return {"success": True, "result": {"owner": name, "expiresAt": expires, "ttlMs": ttl_ms}}
        if action == 'release':
            if owner is not None and owner != name:
                return {"success": False, "error": "not lease owner"}
            if owner is None:
                return {"success": True, "result": {"released": False}}
            lease_state['owner'] = None
            lease_state['expires_at'] = None
            return {"success": True, "result": {"released": True}}
        # leaseStatus: non-mutating snapshot (expired leases already cleared).
        return {"success": True, "result": {"owner": owner, "expiresAt": expires_at, "now": now_ms()}}

# --- Host-enforced guardrails: policy, audit, redaction ---------------------
# Policy is enforced in the host request path so every local client (Python or
# Rust host, raw TCP/CLI, MCP) is governed by the same rules before any action
# is forwarded to the extension.

POLICY_FILE = os.environ.get('BRIDGE_POLICY_FILE', os.path.join(SCRIPT_DIR, 'bridge_policy.json'))
AUDIT_LOG_FILE = os.environ.get('BRIDGE_AUDIT_LOG_FILE', os.path.join(SCRIPT_DIR, 'bridge_audit.jsonl'))

# Action classifications. These are advisory tags for policy authors and for
# the default redaction set; deny/allow/confirmation are driven by the policy
# file, not these sets.
SENSITIVE_ACTIONS = {
    'getCookies', 'storageState', 'executeScript', 'executeScriptCDP',
    'startInterception', 'downloadUrl',
}
MUTATING_ACTIONS = {
    'navigate', 'click', 'type', 'fill', 'hover', 'scroll', 'press', 'drag',
    'select', 'uploadFile', 'activateTab', 'closeTab', 'reload', 'goBack',
    'goForward', 'setViewport', 'setGeolocation', 'clearGeolocation',
    'startInterception', 'stopInterception', 'startMonitoring', 'stopMonitoring',
    'handleDialog', 'downloadUrl', 'batch',
}
DESTRUCTIVE_ACTIONS = {
    'executeScript', 'executeScriptCDP', 'startInterception', 'downloadUrl',
    'getCookies', 'storageState',
}

# Origin-exempt actions: their policy target is NOT the live tab origin, so the
# host must not do a tab-origin lookup for them. navigate/downloadUrl/getCookies
# carry their own target in the payload; the rest are tab-independent or
# host-side. EVERY other forwarded action is treated as tab-scoped and is
# origin-checked against the live tab (fail-safe: a new tab action is protected
# by default rather than silently exempt).
ORIGIN_EXEMPT_ACTIONS = {
    'ping', 'getTabs', 'navigate', 'downloadUrl', 'getCookies', 'sessionStatus',
    'batch', 'lease', 'release', 'leaseStatus', 'policyCheck',
}

# Actions a socket client may never invoke directly: they are reserved for
# host-internal use (e.g. the tab-origin policy lookup) and are rejected with
# "unknown action" so the reserved surface is not externally reachable.
RESERVED_ACTIONS = {'__tabOrigin'}

# Built-in permissive default. The policy file's "default"/"clients" overlay it.
DEFAULT_POLICY = {
    "default": {
        "allowedActions": ["*"],
        "deniedActions": [],
        "allowedOrigins": ["*"],
        "deniedOrigins": [],
        "requireConfirmation": [],
        "redactPatterns": [],
        "redact": True,
        "audit": True,
    },
    "clients": {},
}

_policy_lock = threading.Lock()
_policy_cache = DEFAULT_POLICY
_policy_mtime = None


def load_policy():
    # Read and parse the policy file. Missing/malformed file falls back to the
    # permissive default and logs one error, preserving current behavior.
    try:
        with open(POLICY_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("policy root must be an object")
        return data
    except FileNotFoundError:
        return DEFAULT_POLICY
    except Exception as e:
        logging.error(f"Could not load policy file {POLICY_FILE}: {e}")
        return DEFAULT_POLICY


def current_policy():
    # Cached-with-mtime, matching token reload behavior: reload when the policy
    # file's mtime changes (including absent -> present) so changes take effect
    # without a host restart.
    global _policy_cache, _policy_mtime
    with _policy_lock:
        mtime = _file_mtime(POLICY_FILE)
        if mtime != _policy_mtime:
            _policy_cache = load_policy()
            _policy_mtime = mtime
        return _policy_cache


_POLICY_LIST_KEYS = (
    'allowedActions', 'deniedActions', 'allowedOrigins', 'deniedOrigins',
    'requireConfirmation', 'redactPatterns',
)
_POLICY_BOOL_KEYS = ('redact', 'audit')


def policy_for_client(policy, name):
    # Merge: built-in default -> policy["default"] -> policy["clients"][name].
    # List overrides replace the inherited list; bool overrides replace the
    # inherited value; unknown keys are ignored.
    merged = dict(DEFAULT_POLICY["default"])
    for layer in (policy.get("default"), (policy.get("clients") or {}).get(name)):
        if not isinstance(layer, dict):
            continue
        for key in _POLICY_LIST_KEYS:
            if isinstance(layer.get(key), list):
                merged[key] = list(layer[key])
        for key in _POLICY_BOOL_KEYS:
            if isinstance(layer.get(key), bool):
                merged[key] = layer[key]
    return merged


def normalize_url_targets(raw_url):
    # Lowercase scheme/host, preserve explicit port, strip path/query/fragment.
    # Returns [scheme://host[:port], *://host[:port]] or [] for invalid URLs.
    try:
        parsed = urlparse(raw_url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return []
    if not scheme or not host:
        return []
    if port is not None:
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return [f"{scheme}://{netloc}", f"*://{netloc}"]


def targets_from_payload(action, payload):
    # Ordered list of normalized policy targets derived from a request payload.
    if not isinstance(payload, dict):
        return []
    if action == 'navigate' or action == 'downloadUrl':
        url = payload.get('url')
        return normalize_url_targets(url) if isinstance(url, str) else []
    if action == 'getCookies':
        domain = payload.get('domain')
        if isinstance(domain, str) and domain:
            if domain.startswith('.'):
                domain = domain[1:]
            return [f"*://{domain.lower()}"]
        return []
    if action == 'batch':
        targets = []
        steps = payload.get('steps')
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    targets.extend(targets_from_payload(
                        step.get('action'), step.get('payload') or {}))
        return targets
    return []


def action_matches(patterns, action):
    if not isinstance(patterns, list):
        return False
    return any(fnmatch.fnmatchcase(action, p) for p in patterns if isinstance(p, str))


def target_matches(patterns, targets):
    if not isinstance(patterns, list):
        return False
    for target in targets:
        for p in patterns:
            if isinstance(p, str) and fnmatch.fnmatchcase(target, p):
                return True
    return False


def origin_targets(origin):
    # Convert a tab origin ("https://host[:port]") into policy target strings
    # [scheme://host[:port], *://host[:port]] using the same normalizer as URLs.
    if not isinstance(origin, str) or not origin:
        return []
    return normalize_url_targets(origin)


def policy_constrains_origins(policy, name):
    # True when the client's site policy is non-trivial, i.e. it could allow or
    # deny based on a tab's origin. Lets the host skip the tab-origin lookup
    # round-trip when policy is origin-permissive (deniedOrigins empty and
    # allowedOrigins is exactly ["*"]).
    cp = policy_for_client(policy, name)
    denied = cp.get('deniedOrigins') or []
    allowed = cp.get('allowedOrigins')
    if denied:
        return True
    if allowed != ["*"]:
        return True
    return False


def _step_payloads(payload):
    # Yield (action, payload) for a batch's steps, applying the extension's
    # runBatch defaulting: a top-level batch tabId fills in steps that omit one,
    # so origin policy cannot be bypassed by hoisting tabId to the batch payload.
    default_tab = (payload or {}).get('tabId')
    steps = (payload or {}).get('steps')
    if not isinstance(steps, list):
        return
    for step in steps:
        step = step if isinstance(step, dict) else {}
        s_payload = dict(step.get('payload') or {})
        if s_payload.get('tabId') is None and default_tab is not None:
            s_payload['tabId'] = default_tab
        yield (step.get('action') or ''), s_payload


def tab_ids_needed(action, payload):
    # The set of tabId keys (int or None for the active tab) whose live origin
    # the host must resolve to apply site policy to a tab-scoped request. Returns
    # an empty set for origin-exempt actions. ``None`` means "resolve the active
    # tab". Recurses into batch steps with runBatch tabId defaulting.
    payload = payload if isinstance(payload, dict) else {}
    if action == 'batch':
        needed = set()
        for s_action, s_payload in _step_payloads(payload):
            needed |= tab_ids_needed(s_action, s_payload)
        return needed
    if action in ORIGIN_EXEMPT_ACTIONS:
        return set()
    return {payload.get('tabId')}


def evaluate_policy(policy, name, action, payload, origins=None):
    # Returns (allowed, reason, confirmation_required, redact_enabled,
    # audit_enabled, targets). Precedence: denied action -> allowed action ->
    # denied target -> allowed target -> confirmation requirement.
    # ``origins`` maps a tabId (int, or None for the active tab) to that tab's
    # live origin string; for tab-scoped actions the matching origin is folded
    # into the site-policy targets so policy applies even with no URL in payload.
    cp = policy_for_client(policy, name)
    redact_enabled = cp.get('redact', True)
    audit_enabled = cp.get('audit', True)
    origins = origins or {}
    targets = targets_from_payload(action, payload)
    if action not in ORIGIN_EXEMPT_ACTIONS:
        tab_origin = origins.get((payload or {}).get('tabId'))
        if tab_origin:
            targets = targets + origin_targets(tab_origin)

    # Reserved host-internal actions are never client-invokable, including as a
    # batch step (runBatch would otherwise dispatch them). Deny centrally here.
    if action in RESERVED_ACTIONS:
        return (False, f"action {action} denied", False, redact_enabled, audit_enabled, targets)

    # Apply action-level policy to the action itself first.
    if action_matches(cp.get('deniedActions'), action):
        return (False, f"action {action} denied", False, redact_enabled, audit_enabled, targets)
    allowed_actions = cp.get('allowedActions')
    if not action_matches(allowed_actions, action):
        return (False, f"action {action} not allowed", False, redact_enabled, audit_enabled, targets)
    confirm = action_matches(cp.get('requireConfirmation'), action)

    # For batch, only inspect steps once the batch action itself is allowed and
    # does not require confirmation.
    if action == 'batch':
        if confirm:
            return (True, None, True, redact_enabled, audit_enabled, targets)
        step_confirm = False
        for i, (s_action, s_payload) in enumerate(_step_payloads(payload)):
            s_allowed, s_reason, s_confirm, _, _, _ = evaluate_policy(
                policy, name, s_action, s_payload, origins=origins)
            if not s_allowed:
                return (False, f"batch step {i}: {s_reason}", False,
                        redact_enabled, audit_enabled, targets)
            step_confirm = step_confirm or s_confirm
        return (True, None, step_confirm, redact_enabled, audit_enabled, targets)

    # Target (site) policy for non-batch actions.
    denied_origins = cp.get('deniedOrigins')
    allowed_origins = cp.get('allowedOrigins')
    if targets and target_matches(denied_origins, targets):
        return (False, "target denied", False, redact_enabled, audit_enabled, targets)
    if targets and not target_matches(allowed_origins, targets):
        return (False, "target not allowed", False, redact_enabled, audit_enabled, targets)
    return (True, None, confirm, redact_enabled, audit_enabled, targets)


def write_audit_event(event):
    # Append one JSON line. Never writes payload/response bodies. A write
    # failure is logged but never blocks browser automation.
    try:
        with open(AUDIT_LOG_FILE, 'a') as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        logging.error(f"Could not write audit event to {AUDIT_LOG_FILE}: {e}")


def _audit(audit_enabled, client, action, targets, decision, reason, request_id):
    if not audit_enabled:
        return
    write_audit_event({
        "ts": now_ms(),
        "client": client,
        "action": action,
        "targets": targets,
        "decision": decision,
        "reason": reason,
        "requestId": request_id,
    })


_REDACT_KEY_SUBSTRINGS = ('token', 'secret', 'password', 'cookie', 'session', 'csrf', 'auth')


def _redact_storage_value(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and any(s in k.lower() for s in _REDACT_KEY_SUBSTRINGS):
                out[k] = "<redacted>"
            else:
                out[k] = _redact_storage_value(v)
        return out
    if isinstance(value, list):
        return [_redact_storage_value(v) for v in value]
    return value

# Response fields that carry page-derived content and so are subject to
# policy ``redactPatterns`` masking before reaching the client.
_CONTENT_REDACT_FIELDS = ('html', 'text', 'val', 'value', 'result')


def _compile_patterns(patterns):
    # Compile policy redactPatterns into regexes, skipping invalid ones (logged
    # once). Patterns are matched case-sensitively; authors use inline flags
    # (e.g. (?i)) for case-insensitivity.
    compiled = []
    if not isinstance(patterns, list):
        return compiled
    for p in patterns:
        if not isinstance(p, str) or not p:
            continue
        try:
            compiled.append(re.compile(p))
        except re.error as e:
            logging.error(f"Invalid redactPattern {p!r}: {e}")
    return compiled


def _mask_text(text, compiled):
    for rx in compiled:
        text = rx.sub("<redacted>", text)
    return text


def _redact_content_value(value, compiled):
    if isinstance(value, str):
        return _mask_text(value, compiled)
    if isinstance(value, dict):
        return {k: _redact_content_value(v, compiled) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_content_value(v, compiled) for v in value]
    return value


def redact_response(action, response, redact_enabled, patterns=None):
    # Redact sensitive response values before returning them to socket clients.
    # Operates on a returned copy; never mutates audit/queue/routing structures.
    if not redact_enabled or not isinstance(response, dict):
        return response
    if action == 'getCookies':
        result = response.get('result')
        cookies = None
        container = None
        if isinstance(result, dict) and isinstance(result.get('cookies'), list):
            cookies, container = result.get('cookies'), 'result'
        elif isinstance(result, list):
            cookies, container = result, 'result-list'
        elif isinstance(response.get('cookies'), list):
            cookies, container = response.get('cookies'), 'response'
        if cookies is None:
            return response
        redacted = []
        for c in cookies:
            if isinstance(c, dict):
                c = dict(c)
                if 'value' in c:
                    c['value'] = "<redacted>"
            redacted.append(c)
        out = dict(response)
        if container == 'result':
            new_result = dict(result)
            new_result['cookies'] = redacted
            out['result'] = new_result
        elif container == 'result-list':
            out['result'] = redacted
        else:
            out['cookies'] = redacted
        return out
    if action == 'storageState':
        out = dict(response)
        if 'result' in out:
            out['result'] = _redact_storage_value(out['result'])
        return out
    # Content-bearing actions: mask policy redactPatterns in page-derived text.
    if action in ('getHTML', 'extractText', 'executeScript', 'executeScriptCDP'):
        compiled = _compile_patterns(patterns)
        if not compiled:
            return response
        out = dict(response)
        for field in _CONTENT_REDACT_FIELDS:
            if field in out:
                out[field] = _redact_content_value(out[field], compiled)
        return out
    return response

def forward_to_extension(cmd, resp_timeout, on_registered=None):
    # Send one command to the extension and block until its response or timeout.
    # Returns (req_id, response_dict | None). ``on_registered(req_id)`` runs
    # after the request id is registered but before write_message, so callers
    # can emit the "allow" audit event with the generated id before the action
    # is actually forwarded. Used for normal forwards and host-internal lookups.
    req_id = str(uuid.uuid4())
    cmd["id"] = req_id
    response_queue = queue.Queue(maxsize=1)
    with requests_lock:
        pending_requests[req_id] = response_queue
    if on_registered is not None:
        on_registered(req_id)
    write_message(cmd)
    try:
        return req_id, response_queue.get(timeout=resp_timeout)
    except queue.Empty:
        with requests_lock:
            pending_requests.pop(req_id, None)
        return req_id, None


def resolve_origins(tab_ids, resp_timeout):
    # Resolve each needed tabId (int, or None for the active tab) to its live
    # origin via the reserved __tabOrigin extension action. Returns a dict
    # {tabId: origin_string_or_None}. We prefer the full tab ``url`` over the
    # extension's ``origin`` because JS URL.origin strips explicit default ports
    # (https://x:443 -> https://x); normalize_url_targets() preserves them, so a
    # port-scoped origin policy stays effective for tab-scoped actions. A
    # failed/timed-out lookup maps to None, which is fail-closed under a
    # non-trivial allowedOrigins policy.
    origins = {}
    for tab_id in tab_ids:
        payload = {} if tab_id is None else {"tabId": tab_id}
        _, resp = forward_to_extension({"action": "__tabOrigin", "payload": payload}, resp_timeout)
        origin = None
        if isinstance(resp, dict) and resp.get("success"):
            result = resp.get("result")
            if isinstance(result, dict):
                origin = result.get("url") or result.get("origin")
        origins[tab_id] = origin
    return origins

def handle_socket_client(client_socket):
    # Serve many newline-delimited requests on one connection. Each request is
    # forwarded to the extension and its response awaited via a per-request
    # queue before the next request is read, preserving request/response order.
    buffer = b""
    try:
        client_socket.settimeout(SOCKET_IDLE_TIMEOUT)
        while True:
            # Read until we have at least one complete line (TCP may split/coalesce).
            while b"\n" not in buffer:
                try:
                    chunk = client_socket.recv(65536)
                except socket.timeout:
                    return  # idle too long; drop the connection
                if not chunk:
                    return  # client closed
                buffer += chunk

            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue  # tolerate blank keep-alive lines
            cmd = json.loads(line.decode('utf-8'))

            # Resolve the client by its token; unknown/missing token is rejected.
            name = resolve_client(cmd.get("token"))
            if name is None:
                logging.warning("Rejected unauthenticated/invalid-token request.")
                client_socket.sendall(
                    (json.dumps({"success": False, "error": "unauthorized"}) + "\n").encode('utf-8'))
                return
            cmd.pop("token", None)  # never forward the secret to the extension

            action = cmd.get("action")

            # Reserved host-internal actions (e.g. __tabOrigin) are never
            # reachable from socket clients; reject them as unknown so the
            # internal surface cannot be driven or probed externally.
            if action in RESERVED_ACTIONS:
                logging.warning(f"Rejected reserved action from client: {action}")
                policy = current_policy()
                audit_enabled = policy_for_client(policy, name).get('audit', True)
                _audit(audit_enabled, name, action, [], "deny", "unknown action", None)
                client_socket.sendall(
                    (json.dumps({"success": False, "error": f"unknown action: {action}"}) + "\n").encode('utf-8'))
                continue

            # Lease control actions are answered host-side, never forwarded.
            if action in ('lease', 'release', 'leaseStatus'):
                resp = handle_lease_action(action, cmd.get("payload") or {}, name)
                policy = current_policy()
                audit_enabled = policy_for_client(policy, name).get('audit', True)
                decision = "lease_allow" if resp.get("success") else "lease_deny"
                _audit(audit_enabled, name, action, [], decision, resp.get("error"), None)
                client_socket.sendall((json.dumps(resp) + "\n").encode('utf-8'))
                continue

            policy = current_policy()

            # policyCheck is host-side: report what the policy would decide for a
            # target action/payload without forwarding it to the extension.
            if action == 'policyCheck':
                pc_payload = cmd.get("payload") or {}
                target_action = pc_payload.get("action") or ""
                target_payload = pc_payload.get("payload") or {}
                allowed, reason, confirm, redact_enabled, audit_enabled, targets = evaluate_policy(
                    policy, name, target_action, target_payload)
                # Without forwarding, the host cannot see the live tab origin, so
                # for an origin-constrained policy a tab-scoped action's verdict
                # is provisional: the real request will additionally be checked
                # against the tab origin. Report that so callers don't trust an
                # "allowed" that origin policy may still deny.
                origin_dependent = bool(
                    tab_ids_needed(target_action, target_payload)
                    and policy_constrains_origins(policy, name))
                resp = {"success": True, "result": {
                    "allowed": allowed,
                    "reason": reason,
                    "confirmationRequired": confirm,
                    "redact": redact_enabled,
                    "audit": audit_enabled,
                    "originDependent": origin_dependent,
                }}
                _audit(audit_enabled, name, "policyCheck", targets, "allow", None, None)
                client_socket.sendall((json.dumps(resp) + "\n").encode('utf-8'))
                continue

            payload = cmd.get("payload") or {}
            audit_enabled = policy_for_client(policy, name).get('audit', True)

            # Host-enforced policy, phase 1: action-level and payload-target
            # checks that need no extension round-trip. These run before the
            # lease gate, preserving prior precedence (policy denial wins over a
            # lease held by another client for payload-determined targets).
            allowed, reason, confirm, redact_enabled, audit_enabled, targets = evaluate_policy(
                policy, name, action, payload)
            if not allowed:
                _audit(audit_enabled, name, action, targets, "deny", reason, None)
                client_socket.sendall(
                    (json.dumps({"success": False, "error": f"policy denied: {reason}"}) + "\n").encode('utf-8'))
                continue

            resp_timeout = SOCKET_IDLE_TIMEOUT
            if isinstance(payload, dict):
                req_timeout_ms = payload.get("timeoutMs")
                if isinstance(req_timeout_ms, (int, float)) and req_timeout_ms > 0:
                    resp_timeout = max(SOCKET_IDLE_TIMEOUT, req_timeout_ms / 1000 + 30)

            # Phase 2: tab-origin policy for tab-scoped actions. The live origin
            # comes from a host-internal __tabOrigin lookup, so the lease gate
            # runs first (a non-owner must not trigger any extension round-trip),
            # then origin-aware re-evaluation runs before the confirmation check
            # so a denied origin wins over a confirmation requirement.
            needed = tab_ids_needed(action, payload) if policy_constrains_origins(policy, name) else set()
            if needed:
                with lease_lock:
                    owner, _ = _lease_status_locked()
                if owner is not None and owner != name:
                    _audit(audit_enabled, name, action, targets, "lease_deny", f"leased by {owner}", None)
                    client_socket.sendall(
                        (json.dumps({"success": False, "error": f"leased by {owner}"}) + "\n").encode('utf-8'))
                    continue
                origins = resolve_origins(needed, resp_timeout)
                # Fail closed when any needed tab resolves to no usable origin
                # target: lookup failure, no such tab, or an opaque origin (the
                # string "null"/"" -> origin_targets() == []). Under an
                # origin-constraining policy such a request must not proceed,
                # since an allow-list can never match an absent target.
                if any(not origin_targets(origins.get(t)) for t in needed):
                    targets = targets + ["<unresolved-origin>"]
                    _audit(audit_enabled, name, action, targets, "deny", "tab origin unresolved", None)
                    client_socket.sendall(
                        (json.dumps({"success": False, "error": "policy denied: tab origin unresolved"}) + "\n").encode('utf-8'))
                    continue
                allowed, reason, confirm, redact_enabled, audit_enabled, targets = evaluate_policy(
                    policy, name, action, payload, origins=origins)
                if not allowed:
                    _audit(audit_enabled, name, action, targets, "deny", reason, None)
                    client_socket.sendall(
                        (json.dumps({"success": False, "error": f"policy denied: {reason}"}) + "\n").encode('utf-8'))
                    continue

            if confirm:
                _audit(audit_enabled, name, action, targets, "confirmation_required", None, None)
                client_socket.sendall((json.dumps({
                    "success": False, "error": "confirmation required",
                    "confirmationRequired": True, "action": action}) + "\n").encode('utf-8'))
                continue

            # Enforcement gate: a live lease held by another client blocks others.
            with lease_lock:
                owner, _ = _lease_status_locked()
            if owner is not None and owner != name:
                _audit(audit_enabled, name, action, targets, "lease_deny", f"leased by {owner}", None)
                client_socket.sendall(
                    (json.dumps({"success": False, "error": f"leased by {owner}"}) + "\n").encode('utf-8'))
                continue

            # Send to extension, then block this connection until its response.
            # Most actions resolve well within SOCKET_IDLE_TIMEOUT, but waits and
            # human-handoff carry a payload timeoutMs that can exceed it; cover
            # that window (plus headroom) so the host does not time out before
            # the extension legitimately finishes.
            # Audit "allow" with the generated id before the action is forwarded.
            req_id, response = forward_to_extension(
                cmd, resp_timeout,
                on_registered=lambda rid: _audit(audit_enabled, name, action, targets, "allow", None, rid))
            if response is None:
                logging.error(f"Timed out waiting for extension response to {req_id}.")
                _audit(audit_enabled, name, action, targets, "extension_error", "extension response timeout", req_id)
                client_socket.sendall(
                    (json.dumps({"success": False, "error": "extension response timeout"}) + "\n").encode('utf-8'))
                return
            ext_decision = "extension_success" if response.get("success") else "extension_error"
            _audit(audit_enabled, name, action, targets, ext_decision, response.get("error"), req_id)
            redact_patterns = policy_for_client(policy, name).get('redactPatterns')
            response = redact_response(action, response, redact_enabled, redact_patterns)
            client_socket.sendall((json.dumps(response) + "\n").encode('utf-8'))
    except Exception as e:
        logging.error(f"Error handling socket client: {e}", exc_info=True)
    finally:
        try:
            client_socket.close()
        except Exception:
            pass

def socket_server_loop():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    port = int(os.environ.get('BRIDGE_PORT', 9223))
    try:
        server.bind(('127.0.0.1', port))
    except OSError as e:
        # Almost always means a second copy of this host is already running
        # (e.g. both the old /tmp extension and the new stable one are enabled,
        # racing to bind the same port). Make it loud instead of a silent death.
        logging.error(
            f"FATAL: could not bind 127.0.0.1:{port} ({e}). Another bridge host is "
            f"likely already running. Disable the duplicate Chrome extension so only "
            f"one host owns this port. This host will not accept CLI commands.")
        os._exit(1)
    server.listen(5)
    logging.info(f"TCP socket server listening on 127.0.0.1:{port}")
    while True:
        try:
            client_sock, addr = server.accept()
            logging.info(f"Accepted connection from {addr}")
            t = threading.Thread(target=handle_socket_client, args=(client_sock,), daemon=True)
            t.start()
        except Exception as e:
            logging.error(f"Error in socket server accept: {e}", exc_info=True)

def main():
    logging.info("Native Messaging Host started.")
    # Start the local TCP listener thread
    t = threading.Thread(target=socket_server_loop, daemon=True)
    t.start()
    
    while True:
        try:
            msg = read_message()
            # If the extension sent a response to a command we initiated
            msg_id = msg.get("id")
            if msg_id:
                response_queue = None
                with requests_lock:
                    response_queue = pending_requests.pop(msg_id, None)
                if response_queue is not None:
                    response_queue.put(msg)
                    logging.info(f"Routed response for request ID {msg_id} to its socket handler.")
                else:
                    logging.info(f"Received message with ID {msg_id} but no pending request was found.")
            else:
                logging.info(f"Received message from Chrome with no ID: {msg}")
        except Exception as e:
            logging.error(f"Error in main loop: {e}", exc_info=True)
            break

if __name__ == '__main__':
    main()
