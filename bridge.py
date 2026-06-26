#!/usr/bin/env python3
import sys
import os
import struct
import json
import logging
import socket
import threading
import uuid

# Resolve paths relative to this script so the install is location-independent.
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

# Configure local logging
logging.basicConfig(
    filename=os.environ.get('BRIDGE_LOG_FILE', os.path.join(SCRIPT_DIR, 'bridge_debug.log')),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

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
    try:
        # Read a complete newline-delimited JSON request (TCP may split/coalesce)
        client_socket.settimeout(30)
        buffer = b""
        while b"\n" not in buffer:
            chunk = client_socket.recv(65536)
            if not chunk:
                break
            buffer += chunk
        if not buffer.strip():
            client_socket.close()
            return
        line = buffer.split(b"\n", 1)[0]
        cmd = json.loads(line.decode('utf-8'))

        # Reject any request missing or mismatching the shared token.
        if not AUTH_TOKEN or cmd.get("token") != AUTH_TOKEN:
            logging.warning("Rejected unauthenticated/invalid-token request.")
            client_socket.sendall(
                (json.dumps({"success": False, "error": "unauthorized"}) + "\n").encode('utf-8'))
            client_socket.close()
            return
        cmd.pop("token", None)  # never forward the secret to the extension

        req_id = str(uuid.uuid4())
        cmd["id"] = req_id

        with requests_lock:
            pending_requests[req_id] = client_socket

        # Send to extension
        write_message(cmd)
    except Exception as e:
        logging.error(f"Error handling socket client: {e}", exc_info=True)
        try:
            client_socket.close()
        except:
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
                client_sock = None
                with requests_lock:
                    if msg_id in pending_requests:
                        client_sock = pending_requests.pop(msg_id)
                if client_sock:
                    try:
                        client_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                        client_sock.close()
                        logging.info(f"Routed response for request ID {msg_id} back to socket client.")
                    except Exception as e:
                        logging.error(f"Error sending response to socket client: {e}", exc_info=True)
                else:
                    logging.info(f"Received message with ID {msg_id} but no pending socket client was found.")
            else:
                logging.info(f"Received message from Chrome with no ID: {msg}")
        except Exception as e:
            logging.error(f"Error in main loop: {e}", exc_info=True)
            break

if __name__ == '__main__':
    main()
