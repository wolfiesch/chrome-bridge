use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};

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

/// Read BRIDGE_TOKEN_FILE env or <host_dir>/bridge_token.txt, trimmed.
fn load_token(host_dir: &Path, logger: &Arc<Logger>) -> Option<String> {
    let token_file = match std::env::var("BRIDGE_TOKEN_FILE") {
        Ok(p) => PathBuf::from(p),
        Err(_) => host_dir.join("bridge_token.txt"),
    };
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

type Pending = Arc<Mutex<HashMap<String, TcpStream>>>;

fn handle_socket_client(
    mut stream: TcpStream,
    token: &Arc<Option<String>>,
    pending: &Pending,
    stdout: &Arc<Mutex<io::Stdout>>,
    logger: &Arc<Logger>,
) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));

    // Read a complete newline-delimited JSON request (TCP may split/coalesce).
    let mut buffer: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 65536];
    while !buffer.contains(&b'\n') {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => buffer.extend_from_slice(&chunk[..n]),
            Err(_) => break,
        }
    }

    if buffer.iter().all(|b| b.is_ascii_whitespace()) {
        drop(stream);
        return;
    }

    let line: Vec<u8> = buffer
        .iter()
        .copied()
        .take_while(|&b| b != b'\n')
        .collect();

    let mut cmd: Value = match serde_json::from_slice(&line) {
        Ok(v) => v,
        Err(e) => {
            log_error(logger, &format!("Error handling socket client: {}", e));
            drop(stream);
            return;
        }
    };

    // Reject any request missing or mismatching the shared token.
    let authorized = match token.as_ref() {
        Some(expected) => cmd
            .get("token")
            .and_then(|t| t.as_str())
            .map(|t| t == expected)
            .unwrap_or(false),
        None => false,
    };

    if !authorized {
        log_warn(logger, "Rejected unauthenticated/invalid-token request.");
        let resp = json!({"success": false, "error": "unauthorized"});
        let mut out = serde_json::to_vec(&resp).unwrap_or_default();
        out.push(b'\n');
        let _ = stream.write_all(&out);
        let _ = stream.flush();
        drop(stream);
        return;
    }

    // Never forward the secret to the extension.
    if let Value::Object(map) = &mut cmd {
        map.remove("token");
    }

    let req_id = uuid::Uuid::new_v4().to_string();
    if let Value::Object(map) = &mut cmd {
        map.insert("id".to_string(), Value::String(req_id.clone()));
    }

    if let Ok(mut p) = pending.lock() {
        p.insert(req_id, stream);
    }

    write_message(stdout, logger, &cmd);
}

fn socket_server_loop(
    token: Arc<Option<String>>,
    pending: Pending,
    stdout: Arc<Mutex<io::Stdout>>,
    logger: Arc<Logger>,
) {
    let port: u16 = std::env::var("BRIDGE_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(9223);

    // Plain TcpListener::bind; SO_REUSEADDR is best-effort (OS default).
    let listener = match TcpListener::bind(("127.0.0.1", port)) {
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
            return;
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
                let token = Arc::clone(&token);
                let pending = Arc::clone(&pending);
                let stdout = Arc::clone(&stdout);
                let logger = Arc::clone(&logger);
                std::thread::spawn(move || {
                    handle_socket_client(stream, &token, &pending, &stdout, &logger);
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

    let token: Arc<Option<String>> = Arc::new(load_token(&host_dir, &logger));
    let stdout: Arc<Mutex<io::Stdout>> = Arc::new(Mutex::new(io::stdout()));
    let pending: Pending = Arc::new(Mutex::new(HashMap::new()));

    {
        let token = Arc::clone(&token);
        let pending = Arc::clone(&pending);
        let stdout = Arc::clone(&stdout);
        let logger = Arc::clone(&logger);
        std::thread::spawn(move || {
            socket_server_loop(token, pending, stdout, logger);
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
                let client = pending.lock().ok().and_then(|mut p| p.remove(&id));
                match client {
                    Some(mut stream) => {
                        let mut out = serde_json::to_vec(&msg).unwrap_or_default();
                        out.push(b'\n');
                        match stream.write_all(&out).and_then(|_| stream.flush()) {
                            Ok(()) => {
                                drop(stream);
                                log_info(
                                    &logger,
                                    &format!(
                                        "Routed response for request ID {} back to socket client.",
                                        id
                                    ),
                                );
                            }
                            Err(e) => {
                                log_error(
                                    &logger,
                                    &format!("Error sending response to socket client: {}", e),
                                );
                            }
                        }
                    }
                    None => {
                        log_info(
                            &logger,
                            &format!(
                                "Received message with ID {} but no pending socket client was found.",
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
