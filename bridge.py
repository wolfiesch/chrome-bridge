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
def load_token():
    token_file = os.environ.get(
        'BRIDGE_TOKEN_FILE', os.path.join(SCRIPT_DIR, 'bridge_token.txt'))
    try:
        with open(token_file) as f:
            return f.read().strip()
    except Exception as e:
        logging.error(f"Could not read token file {token_file}: {e}")
        return None

AUTH_TOKEN = load_token()

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

            # Reject any request missing or mismatching the shared token.
            if not AUTH_TOKEN or cmd.get("token") != AUTH_TOKEN:
                logging.warning("Rejected unauthenticated/invalid-token request.")
                client_socket.sendall(
                    (json.dumps({"success": False, "error": "unauthorized"}) + "\n").encode('utf-8'))
                return
            cmd.pop("token", None)  # never forward the secret to the extension

            req_id = str(uuid.uuid4())
            cmd["id"] = req_id
            response_queue = queue.Queue(maxsize=1)
            with requests_lock:
                pending_requests[req_id] = response_queue

            # Send to extension, then block this connection until its response.
            write_message(cmd)
            try:
                response = response_queue.get(timeout=SOCKET_IDLE_TIMEOUT)
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
        return
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
