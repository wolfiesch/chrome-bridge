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

            # Lease control actions are answered host-side, never forwarded.
            if action in ('lease', 'release', 'leaseStatus'):
                resp = handle_lease_action(action, cmd.get("payload") or {}, name)
                client_socket.sendall((json.dumps(resp) + "\n").encode('utf-8'))
                continue

            # Enforcement gate: a live lease held by another client blocks others.
            with lease_lock:
                owner, _ = _lease_status_locked()
            if owner is not None and owner != name:
                client_socket.sendall(
                    (json.dumps({"success": False, "error": f"leased by {owner}"}) + "\n").encode('utf-8'))
                continue

            req_id = str(uuid.uuid4())
            cmd["id"] = req_id
            response_queue = queue.Queue(maxsize=1)
            with requests_lock:
                pending_requests[req_id] = response_queue

            # Send to extension, then block this connection until its response.
            # Most actions resolve well within SOCKET_IDLE_TIMEOUT, but waits and
            # human-handoff carry a payload timeoutMs that can exceed it; cover
            # that window (plus headroom) so the host does not time out before
            # the extension legitimately finishes.
            resp_timeout = SOCKET_IDLE_TIMEOUT
            payload = cmd.get("payload")
            if isinstance(payload, dict):
                req_timeout_ms = payload.get("timeoutMs")
                if isinstance(req_timeout_ms, (int, float)) and req_timeout_ms > 0:
                    resp_timeout = max(SOCKET_IDLE_TIMEOUT, req_timeout_ms / 1000 + 30)
            write_message(cmd)
            try:
                response = response_queue.get(timeout=resp_timeout)
            except queue.Empty:
                logging.error(f"Timed out waiting for extension response to {req_id}.")
                with requests_lock:
                    pending_requests.pop(req_id, None)
                client_socket.sendall(
                    (json.dumps({"success": False, "error": "extension response timeout"}) + "\n").encode('utf-8'))
                return
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
