use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use socket2::{Domain, Protocol, Socket, Type};

/// Directory of the current executable; base for default token/log paths.
fn host_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Mutex-guarded file logger. Never writes to stdout.
struct Logger {
    file: Mutex<std::fs::File>,
}

impl Logger {
    fn new(path: &PathBuf) -> io::Result<Logger> {
        let file = OpenOptions::new().append(true).create(true).open(path)?;
        Ok(Logger {
            file: Mutex::new(file),
        })
    }

    fn log(&self, level: &str, msg: &str) {
        let line = format!(
            "{} - {} - {}",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S,%3f"),
            level,
            msg
        );
        if let Ok(mut f) = self.file.lock() {
            let _ = writeln!(f, "{}", line);
            let _ = f.flush();
        }
    }
}

fn log_info(logger: &Arc<Logger>, msg: &str) {
    logger.log("INFO", msg);
}

fn log_warn(logger: &Arc<Logger>, msg: &str) {
    logger.log("WARNING", msg);
}

fn log_error(logger: &Arc<Logger>, msg: &str) {
    logger.log("ERROR", msg);
}

fn log_path(host_dir: &Path) -> PathBuf {
    match std::env::var("BRIDGE_LOG_FILE") {
        Ok(p) => PathBuf::from(p),
        Err(_) => host_dir.join("bridge_debug.log"),
    }
}

/// Path of the legacy single-token file (BRIDGE_TOKEN_FILE or <host_dir>/bridge_token.txt).
fn token_file_path(host_dir: &Path) -> PathBuf {
    match std::env::var("BRIDGE_TOKEN_FILE") {
        Ok(p) => PathBuf::from(p),
        Err(_) => host_dir.join("bridge_token.txt"),
    }
}

/// Path of the named-token file (BRIDGE_TOKENS_FILE or <host_dir>/bridge_tokens.txt).
fn tokens_file_path(host_dir: &Path) -> PathBuf {
    match std::env::var("BRIDGE_TOKENS_FILE") {
        Ok(p) => PathBuf::from(p),
        Err(_) => host_dir.join("bridge_tokens.txt"),
    }
}

/// Last-modified time of a path, or None when the file is missing/unreadable.
fn file_mtime(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// Read BRIDGE_TOKEN_FILE env or <host_dir>/bridge_token.txt, trimmed.
fn load_token(host_dir: &Path, logger: &Arc<Logger>) -> Option<String> {
    let token_file = token_file_path(host_dir);
    match std::fs::read_to_string(&token_file) {
        Ok(s) => Some(s.trim().to_string()),
        Err(e) => {
            log_error(
                logger,
                &format!("Could not read token file {}: {}", token_file.display(), e),
            );
            None
        }
    }
}

/// Build a token -> client-name registry.
///
/// The legacy single token (BRIDGE_TOKEN_FILE, default <host_dir>/bridge_token.txt)
/// is registered under the name `default`. If BRIDGE_TOKENS_FILE (default
/// <host_dir>/bridge_tokens.txt) exists, each non-empty, non-`#` line is parsed
/// as `name:token` (split on the first ':') and added to the registry.
fn load_tokens(host_dir: &Path, logger: &Arc<Logger>) -> HashMap<String, String> {
    let mut tokens: HashMap<String, String> = HashMap::new();

    if let Some(legacy) = load_token(host_dir, logger) {
        if !legacy.is_empty() {
            tokens.insert(legacy, "default".to_string());
        }
    }

    let tokens_file = tokens_file_path(host_dir);
    if tokens_file.exists() {
        match std::fs::read_to_string(&tokens_file) {
            Ok(contents) => {
                for line in contents.lines() {
                    let trimmed = line.trim();
                    if trimmed.is_empty() || trimmed.starts_with('#') {
                        continue;
                    }
                    match trimmed.split_once(':') {
                        Some((name, tok)) => {
                            let name = name.trim();
                            let tok = tok.trim();
                            if !name.is_empty() && !tok.is_empty() {
                                tokens.insert(tok.to_string(), name.to_string());
                            }
                        }
                        None => {
                            log_warn(
                                logger,
                                &format!("Ignoring malformed token line (expected name:token): {}", trimmed),
                            );
                        }
                    }
                }
            }
            Err(e) => {
                log_error(
                    logger,
                    &format!("Could not read tokens file {}: {}", tokens_file.display(), e),
                );
            }
        }
    }

    tokens
}

/// Framed stdout writer: native-endian u32 length prefix + JSON bytes.
fn write_message(stdout: &Arc<Mutex<io::Stdout>>, logger: &Arc<Logger>, message: &Value) {
    let encoded = serde_json::to_vec(message).unwrap_or_else(|_| b"{}".to_vec());
    let id = message.get("id");
    let action = message.get("action");
    log_info(
        logger,
        &format!(
            "Forwarding to extension: id={} action={} ({} bytes)",
            value_field(id),
            value_field(action),
            encoded.len()
        ),
    );
    if let Ok(mut out) = stdout.lock() {
        let _ = out.write_all(&(encoded.len() as u32).to_ne_bytes());
        let _ = out.write_all(&encoded);
        let _ = out.flush();
    }
}

/// Render an optional JSON field roughly like Python's str(message.get(k)).
fn value_field(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// Per-request channel registry: in-flight request id -> Sender the handler
/// thread blocks on for the extension's response. The stdin reader routes
/// responses here.
type Pending = Arc<Mutex<HashMap<String, Sender<Value>>>>;

/// token -> client name registry plus the recorded mtimes of the two token
/// files, shared across connections and reloadable under a write lock.
struct TokenRegistry {
    map: HashMap<String, String>,
    token_file_mtime: Option<SystemTime>,
    tokens_file_mtime: Option<SystemTime>,
}

type Tokens = Arc<RwLock<TokenRegistry>>;

/// Build the registry: load the map and record both files' current mtimes.
fn build_registry(host_dir: &Path, logger: &Arc<Logger>) -> TokenRegistry {
    TokenRegistry {
        map: load_tokens(host_dir, logger),
        token_file_mtime: file_mtime(&token_file_path(host_dir)),
        tokens_file_mtime: file_mtime(&tokens_file_path(host_dir)),
    }
}

/// Resolve a request token to a client name. On a miss, reload the registry if
/// either token file's mtime advanced (or an absent file became present) and
/// re-lookup; only a still-absent token is unresolved.
fn resolve_client(
    tokens: &Tokens,
    host_dir: &Path,
    logger: &Arc<Logger>,
    token: &str,
) -> Option<String> {
    if let Ok(reg) = tokens.read() {
        if let Some(name) = reg.map.get(token) {
            return Some(name.clone());
        }
    }

    let cur_token = file_mtime(&token_file_path(host_dir));
    let cur_tokens = file_mtime(&tokens_file_path(host_dir));

    if let Ok(mut reg) = tokens.write() {
        if cur_token != reg.token_file_mtime || cur_tokens != reg.tokens_file_mtime {
            reg.map = load_tokens(host_dir, logger);
            reg.token_file_mtime = cur_token;
            reg.tokens_file_mtime = cur_tokens;
        }
        return reg.map.get(token).cloned();
    }

    None
}

/// Cooperative single-holder lease over the shared Chrome profile.
struct Lease {
    owner: Option<String>,
    expires_at: Option<u128>,
}

type LeaseState = Arc<Mutex<Lease>>;

/// Current wall-clock time in epoch milliseconds.
fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Idle read timeout for a persistent connection (BRIDGE_SOCKET_IDLE_TIMEOUT, default 300s).
fn socket_idle_timeout() -> Duration {
    let secs = std::env::var("BRIDGE_SOCKET_IDLE_TIMEOUT")
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|s| *s > 0.0)
        .unwrap_or(300.0);
    Duration::from_secs_f64(secs)
}

/// Resolve the live lease owner, clearing the lease in place if its TTL expired.
fn live_owner(lease: &mut Lease, now: u128) -> Option<String> {
    match lease.expires_at {
        Some(exp) if now < exp => lease.owner.clone(),
        _ => {
            lease.owner = None;
            lease.expires_at = None;
            None
        }
    }
}

/// Handle lease/release/leaseStatus host-side. Returns None if `action` is not
/// a lease verb (caller should forward to the extension instead).
fn handle_lease_action(action: &str, payload: Option<&Value>, client: &str, lease: &LeaseState) -> Option<Value> {
    let now = now_ms();
    match action {
        "lease" => {
            let ttl = payload
                .and_then(|p| p.get("ttlMs"))
                .and_then(|v| v.as_u64())
                .unwrap_or(300_000) as u128;
            let resp = if let Ok(mut g) = lease.lock() {
                match live_owner(&mut g, now) {
                    Some(o) if o != client => {
                        json!({"success": false, "error": format!("leased by {}", o)})
                    }
                    _ => {
                        let expires = now + ttl;
                        g.owner = Some(client.to_string());
                        g.expires_at = Some(expires);
                        json!({"success": true, "result": {
                            "owner": client,
                            "expiresAt": expires as u64,
                            "ttlMs": ttl as u64
                        }})
                    }
                }
            } else {
                json!({"success": false, "error": "lease state unavailable"})
            };
            Some(resp)
        }
        "release" => {
            let resp = if let Ok(mut g) = lease.lock() {
                match live_owner(&mut g, now) {
                    Some(o) if o != client => {
                        json!({"success": false, "error": "not lease owner"})
                    }
                    Some(_) => {
                        g.owner = None;
                        g.expires_at = None;
                        json!({"success": true, "result": {"released": true}})
                    }
                    None => json!({"success": true, "result": {"released": false}}),
                }
            } else {
                json!({"success": false, "error": "lease state unavailable"})
            };
            Some(resp)
        }
        "leaseStatus" => {
            let resp = if let Ok(mut g) = lease.lock() {
                let owner = live_owner(&mut g, now);
                json!({"success": true, "result": {
                    "owner": owner,
                    "expiresAt": g.expires_at.map(|e| e as u64),
                    "now": now as u64
                }})
            } else {
                json!({"success": false, "error": "lease state unavailable"})
            };
            Some(resp)
        }
        _ => None,
    }
}

/// Enforcement gate for non-lease actions: if another client holds a live lease,
/// block with `leased by <owner>`. Returns Some(blocked_response) when blocked.
fn lease_gate(client: &str, lease: &LeaseState) -> Option<Value> {
    let now = now_ms();
    if let Ok(mut g) = lease.lock() {
        if let Some(o) = live_owner(&mut g, now) {
            if o != client {
                return Some(json!({"success": false, "error": format!("leased by {}", o)}));
            }
        }
    }
    None
}

/// Write a single newline-delimited JSON response line to the client socket.
fn write_line(stream: &mut TcpStream, value: &Value) -> io::Result<()> {
    let mut out = serde_json::to_vec(value).unwrap_or_default();
    out.push(b'\n');
    stream.write_all(&out)?;
    stream.flush()
}

fn handle_socket_client(
    mut stream: TcpStream,
    host_dir: &Path,
    tokens: &Tokens,
    pending: &Pending,
    lease: &LeaseState,
    stdout: &Arc<Mutex<io::Stdout>>,
    logger: &Arc<Logger>,
) {
    let idle = socket_idle_timeout();
    let _ = stream.set_read_timeout(Some(idle));

    // Serve many newline-delimited requests on one connection. The residual
    // buffer carries bytes past the consumed line across iterations (TCP may
    // split/coalesce frames).
    let mut buffer: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 65536];

    loop {
        // Read until we have at least one complete line.
        while !buffer.contains(&b'\n') {
            match stream.read(&mut chunk) {
                Ok(0) => return, // client closed
                Ok(n) => buffer.extend_from_slice(&chunk[..n]),
                Err(_) => return, // idle timeout or other IO error: drop connection
            }
        }

        let nl = match buffer.iter().position(|&b| b == b'\n') {
            Some(i) => i,
            None => continue,
        };
        let line: Vec<u8> = buffer.drain(..=nl).take(nl).collect();

        if line.iter().all(|b| b.is_ascii_whitespace()) {
            continue; // tolerate blank keep-alive lines
        }

        let mut cmd: Value = match serde_json::from_slice(&line) {
            Ok(v) => v,
            Err(e) => {
                log_error(logger, &format!("Error handling socket client: {}", e));
                return;
            }
        };

        // Reject any request whose token is missing or not in the registry.
        // On a miss, resolve_client mtime-checks the token files and reloads
        // before giving up, so newly added tokens resolve without a restart.
        let client_name = cmd
            .get("token")
            .and_then(|t| t.as_str())
            .and_then(|t| resolve_client(tokens, host_dir, logger, t));
        let client_name = match client_name {
            Some(name) => name,
            None => {
                log_warn(logger, "Rejected unauthenticated/invalid-token request.");
                let _ = write_line(&mut stream, &json!({"success": false, "error": "unauthorized"}));
                return;
            }
        };

        // Never forward the secret to the extension.
        if let Value::Object(map) = &mut cmd {
            map.remove("token");
        }

        let action = cmd.get("action").and_then(|a| a.as_str()).unwrap_or("").to_string();
        let payload = cmd.get("payload").cloned();

        // Lease verbs are answered host-side with no extension round-trip.
        if let Some(resp) = handle_lease_action(&action, payload.as_ref(), &client_name, lease) {
            if write_line(&mut stream, &resp).is_err() {
                return;
            }
            continue;
        }

        // A live lease held by another client blocks every other action.
        if let Some(blocked) = lease_gate(&client_name, lease) {
            if write_line(&mut stream, &blocked).is_err() {
                return;
            }
            continue;
        }

        let req_id = uuid::Uuid::new_v4().to_string();
        if let Value::Object(map) = &mut cmd {
            map.insert("id".to_string(), Value::String(req_id.clone()));
        }

        let (tx, rx) = std::sync::mpsc::channel::<Value>();
        if let Ok(mut p) = pending.lock() {
            p.insert(req_id.clone(), tx);
        }

        // Send to extension, then block this connection until its response.
        write_message(stdout, logger, &cmd);
        match rx.recv_timeout(idle) {
            Ok(response) => {
                if write_line(&mut stream, &response).is_err() {
                    return;
                }
            }
            Err(_) => {
                log_error(
                    logger,
                    &format!("Timed out waiting for extension response to {}.", req_id),
                );
                if let Ok(mut p) = pending.lock() {
                    p.remove(&req_id);
                }
                let _ = write_line(
                    &mut stream,
                    &json!({"success": false, "error": "extension response timeout"}),
                );
                return;
            }
        }
    }
}

fn socket_server_loop(
    host_dir: PathBuf,
    tokens: Tokens,
    pending: Pending,
    lease: LeaseState,
    stdout: Arc<Mutex<io::Stdout>>,
    logger: Arc<Logger>,
) {
    let port: u16 = std::env::var("BRIDGE_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(9223);

    // SO_REUSEADDR before bind, matching Python (SOL_SOCKET/SO_REUSEADDR=1). Avoids
    // transient bind failures against a TIME_WAIT port during rapid host replacement.
    let addr: std::net::SocketAddr = (std::net::Ipv4Addr::LOCALHOST, port).into();
    let bind_result = (|| -> io::Result<TcpListener> {
        let socket = Socket::new(Domain::IPV4, Type::STREAM, Some(Protocol::TCP))?;
        socket.set_reuse_address(true)?;
        socket.bind(&addr.into())?;
        socket.listen(128)?;
        Ok(socket.into())
    })();
    let listener = match bind_result {
        Ok(l) => l,
        Err(e) => {
            log_error(
                &logger,
                &format!(
                    "FATAL: could not bind 127.0.0.1:{} ({}). Another bridge host is \
                     likely already running. Disable the duplicate Chrome extension so only \
                     one host owns this port. This host will not accept CLI commands.",
                    port, e
                ),
            );
            std::process::exit(1);
        }
    };

    log_info(
        &logger,
        &format!("TCP socket server listening on 127.0.0.1:{}", port),
    );

    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let addr = stream
                    .peer_addr()
                    .map(|a| a.to_string())
                    .unwrap_or_else(|_| "<unknown>".to_string());
                log_info(&logger, &format!("Accepted connection from {}", addr));
                let host_dir = host_dir.clone();
                let tokens = Arc::clone(&tokens);
                let pending = Arc::clone(&pending);
                let lease = Arc::clone(&lease);
                let stdout = Arc::clone(&stdout);
                let logger = Arc::clone(&logger);
                std::thread::spawn(move || {
                    handle_socket_client(stream, &host_dir, &tokens, &pending, &lease, &stdout, &logger);
                });
            }
            Err(e) => {
                log_error(&logger, &format!("Error in socket server accept: {}", e));
            }
        }
    }
}

fn main() {
    let host_dir = host_dir();
    let logger = Arc::new(
        Logger::new(&log_path(&host_dir)).unwrap_or_else(|e| {
            eprintln!("could not open log file: {}", e);
            std::process::exit(1);
        }),
    );

    log_info(&logger, "Native Messaging Host started.");

    let tokens: Tokens = Arc::new(RwLock::new(build_registry(&host_dir, &logger)));
    let stdout: Arc<Mutex<io::Stdout>> = Arc::new(Mutex::new(io::stdout()));
    let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
    let lease: LeaseState = Arc::new(Mutex::new(Lease {
        owner: None,
        expires_at: None,
    }));

    {
        let host_dir = host_dir.clone();
        let tokens = Arc::clone(&tokens);
        let pending = Arc::clone(&pending);
        let lease = Arc::clone(&lease);
        let stdout = Arc::clone(&stdout);
        let logger = Arc::clone(&logger);
        std::thread::spawn(move || {
            socket_server_loop(host_dir, tokens, pending, lease, stdout, logger);
        });
    }

    let stdin = io::stdin();
    let mut handle = stdin.lock();

    loop {
        // Read 4-byte native-endian length prefix.
        let mut len_buf = [0u8; 4];
        match read_exact_or_eof(&mut handle, &mut len_buf) {
            Ok(true) => {}
            Ok(false) => {
                log_info(&logger, "Extension disconnected (empty read).");
                std::process::exit(0);
            }
            Err(e) => {
                log_error(&logger, &format!("Error in main loop: {}", e));
                break;
            }
        }

        let message_length = u32::from_ne_bytes(len_buf) as usize;
        let mut body = vec![0u8; message_length];
        if let Err(e) = handle.read_exact(&mut body) {
            log_error(&logger, &format!("Error in main loop: {}", e));
            break;
        }

        log_info(
            &logger,
            &format!("Read message from extension ({} bytes)", message_length),
        );

        let msg: Value = match serde_json::from_slice(&body) {
            Ok(v) => v,
            Err(e) => {
                log_error(&logger, &format!("Error in main loop: {}", e));
                break;
            }
        };

        let msg_id = msg.get("id").and_then(|v| v.as_str()).map(|s| s.to_string());

        match msg_id {
            Some(id) => {
                let sender = pending.lock().ok().and_then(|mut p| p.remove(&id));
                match sender {
                    Some(tx) => match tx.send(msg) {
                        Ok(()) => {
                            log_info(
                                &logger,
                                &format!(
                                    "Routed response for request ID {} to its socket handler.",
                                    id
                                ),
                            );
                        }
                        Err(e) => {
                            log_error(
                                &logger,
                                &format!("Error sending response to socket handler: {}", e),
                            );
                        }
                    },
                    None => {
                        log_info(
                            &logger,
                            &format!(
                                "Received message with ID {} but no pending request was found.",
                                id
                            ),
                        );
                    }
                }
            }
            None => {
                log_info(
                    &logger,
                    &format!("Received message from Chrome with no ID: {}", msg),
                );
            }
        }
    }
}

/// Read exactly buf.len() bytes. Returns Ok(false) on clean EOF before any byte,
/// Ok(true) on success, Err on partial/other IO error.
fn read_exact_or_eof<R: Read>(reader: &mut R, buf: &mut [u8]) -> io::Result<bool> {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..]) {
            Ok(0) => {
                if filled == 0 {
                    return Ok(false);
                }
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "unexpected eof reading length prefix",
                ));
            }
            Ok(n) => filled += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(true)
}
